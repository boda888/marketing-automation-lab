"""Rate budget per vendor account scope, shared by every execution and queue-mode worker.

n8n paces per execution: HTTP Request Batching spaces the items of one node, but two
workflows on the same Instantly workspace, or five queue-mode workers running the same
workflow, each think they own the whole limit (research R9 2026-11-12). Most vendors count
the **account**, not the key: Instantly the workspace across keys and API versions,
Smartlead pools admin keys, Attio the workspace, Pipedrive the company (R9 2026-11-22).

One budget per scope (``instantly:ws-north``, ``apollo:team-1``), stored in one SQLite
file, so every process that opens the same file shares it:

    configure(scope, windows)          several windows, all must pass (Apollo min + h + day)
    acquire(scope, cost=1) -> Grant    non-blocking: granted, or the wait in seconds
    penalize(scope, retry_after)       a 429: nobody calls the scope until it has passed

Each window is a sliding log: at most ``limit`` cost units in any ``seconds``-long interval.
That is at least as strict as the vendor's fixed windows (every fixed window is one of those
intervals), so a fixed-window vendor never sees more than its limit from this budget.
``cost`` is charged per call, for vendors that bill endpoints differently (Pipedrive tokens).

``acquire`` never sleeps and never books a future slot: it either records the call now or
answers how long to wait, and the caller (an n8n Wait node) asks again. There is no queue,
so callers are not served in arrival order. A ``Retry-After`` from the vendor overrides the
windows, because the vendor's count includes callers this budget never saw.

Cross-process safety: every acquire runs in ``BEGIN IMMEDIATE``, which takes SQLite's write
lock before reading the log, so two processes cannot both see the last free slot.
"""

from __future__ import annotations

import json
import math
import sqlite3
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS rate_scopes (
        scope TEXT PRIMARY KEY,
        windows TEXT NOT NULL,
        blocked_until REAL NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS rate_events (
        id INTEGER PRIMARY KEY,
        scope TEXT NOT NULL,
        at REAL NOT NULL,
        cost INTEGER NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS rate_events_scope_at ON rate_events (scope, at)",
)


class UnknownScope(Exception):
    """acquire / penalize / usage on a scope that was never configured (fail closed)."""


class CostTooHigh(ValueError):
    """A cost no window can ever grant: waiting would never help."""


@dataclass(frozen=True)
class Window:
    seconds: float
    limit: int

    def __post_init__(self) -> None:
        if self.seconds <= 0 or self.limit < 1:
            raise ValueError("a window needs seconds > 0 and limit >= 1")

    @property
    def label(self) -> str:
        return f"{self.seconds:g}s"


@dataclass(frozen=True)
class Preset:
    """Published limits for one vendor account scope, with how sure the lab is of them."""

    windows: tuple[Window, ...]
    scope: str
    confidence: str
    source: str


# From the R9 2026-11-22 table. Every number is a web-search summary of the vendor's page;
# none of those pages could be opened from the run environment. Check your plan's dashboard.
PRESETS: dict[str, Preset] = {
    "instantly": Preset(
        (Window(1, 100), Window(60, 6000)),
        "workspace: all keys, API v1 + v2",
        "medium (search summary)",
        "https://developer.instantly.ai/getting-started/rate-limit",
    ),
    "smartlead_admin_pool": Preset(
        (Window(60, 200),),
        "all admin keys of the account together",
        "low (search summary; sources disagree)",
        "https://helpcenter.smartlead.ai/en/articles/68-how-to-get-started-with-the-smartlead-api",
    ),
    "smartlead_client_key": Preset(
        (Window(60, 60),),
        "one client key",
        "low (search summary; sources disagree)",
        "https://helpcenter.smartlead.ai/en/articles/68-how-to-get-started-with-the-smartlead-api",
    ),
    "apollo_basic": Preset(
        (Window(60, 200), Window(3600, 400), Window(86400, 2000)),
        "team, per endpoint (Basic / Organization)",
        "medium (search summary)",
        "https://docs.apollo.io/reference/rate-limits",
    ),
    "apollo_professional": Preset(
        (Window(60, 200), Window(3600, 600), Window(86400, 6000)),
        "team, per endpoint (Professional)",
        "medium (search summary)",
        "https://docs.apollo.io/reference/rate-limits",
    ),
    "apollo_free": Preset(
        (Window(60, 50), Window(3600, 200), Window(86400, 600)),
        "team, per endpoint (Free)",
        "medium (search summary)",
        "https://docs.apollo.io/reference/rate-limits",
    ),
    "hubspot_app": Preset(
        (Window(10, 100),),
        "one app per portal (daily cap by tier not modelled)",
        "medium (SDK README, R7 2026-10-29)",
        "https://github.com/HubSpot/hubspot-api-nodejs",
    ),
    "attio_reads": Preset(
        (Window(1, 100),),
        "workspace",
        "medium (search summary, R7 2026-10-29)",
        "https://docs.attio.com/rest-api/guides/rate-limiting",
    ),
    "attio_writes": Preset(
        (Window(1, 25),),
        "workspace",
        "medium (search summary, R7 2026-10-29)",
        "https://docs.attio.com/rest-api/guides/rate-limiting",
    ),
    "heyreach": Preset(
        (Window(60, 300),),
        "unknown (third-party pages)",
        "low (search summary, R5 2026-11-06)",
        "R5 note 2026-11-06",
    ),
}


@dataclass(frozen=True)
class WindowUsage:
    seconds: float
    limit: int
    used: int

    @property
    def remaining(self) -> int:
        return max(self.limit - self.used, 0)


@dataclass(frozen=True)
class Grant:
    granted: bool
    wait_seconds: float  # 0 when granted
    blocked_by: str | None  # "retry_after", a window label like "3600s", or None
    usage: tuple[WindowUsage, ...]  # after this call (granted) or as found (refused)

    @property
    def wait_ms(self) -> int:
        return math.ceil(self.wait_seconds * 1000)


def _check_scope(scope: str) -> None:
    if not scope.strip():
        raise ValueError("scope must be non-blank")


def _windows(raw: Iterable[Window | tuple[float, int]]) -> tuple[Window, ...]:
    out = tuple(w if isinstance(w, Window) else Window(float(w[0]), int(w[1])) for w in raw)
    if not out:
        raise ValueError("a scope needs at least one window")
    if len({w.seconds for w in out}) != len(out):
        raise ValueError("two windows with the same length")
    return tuple(sorted(out, key=lambda w: w.seconds))


def _wait_for(window: Window, events: list[tuple[float, int]], cost: int, now: float) -> float:
    """Seconds until ``cost`` more fits in ``window``; ``events`` are (at, cost), oldest first."""
    inside = [(at, c) for at, c in events if at > now - window.seconds]
    excess = sum(c for _, c in inside) + cost - window.limit
    if excess <= 0:
        return 0.0
    freed = 0
    for at, c in inside:
        freed += c
        if freed >= excess:
            return at + window.seconds - now
    raise AssertionError("cost <= limit, so expiring every event always frees enough")


class RateBudget:
    """Multi-window budget per scope on one SQLite file (``":memory:"`` for tests)."""

    def __init__(
        self,
        path: str | Path = ":memory:",
        *,
        busy_timeout: float = 10.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.path = str(path)
        self._clock = clock
        self._lock = threading.Lock()
        self._db = sqlite3.connect(
            self.path, timeout=busy_timeout, isolation_level=None, check_same_thread=False
        )
        if self.path != ":memory:":
            self._db.execute("PRAGMA journal_mode=WAL")
        for stmt in _SCHEMA:
            self._db.execute(stmt)

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> RateBudget:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def configure(
        self, scope: str, windows: Iterable[Window | tuple[float, int]] | str
    ) -> tuple[Window, ...]:
        """Set the scope's windows (a list, or a ``PRESETS`` name). Keeps its call log."""
        _check_scope(scope)
        if isinstance(windows, str):
            if windows not in PRESETS:
                raise ValueError(f"unknown preset {windows!r}")
            parsed = PRESETS[windows].windows
        else:
            parsed = _windows(windows)
        encoded = json.dumps([[w.seconds, w.limit] for w in parsed])
        with self._lock:
            self._db.execute(
                "INSERT INTO rate_scopes (scope, windows) VALUES (?, ?) "
                "ON CONFLICT (scope) DO UPDATE SET windows = excluded.windows",
                (scope, encoded),
            )
        return parsed

    def _load(self, scope: str) -> tuple[tuple[Window, ...], float]:
        row = self._db.execute(
            "SELECT windows, blocked_until FROM rate_scopes WHERE scope = ?", (scope,)
        ).fetchone()
        if row is None:
            raise UnknownScope(f"scope {scope!r} has no budget; configure it first")
        return tuple(Window(s, n) for s, n in json.loads(row[0])), row[1]

    def _events(self, scope: str, since: float) -> list[tuple[float, int]]:
        return self._db.execute(
            "SELECT at, cost FROM rate_events WHERE scope = ? AND at > ? ORDER BY at, id",
            (scope, since),
        ).fetchall()

    @staticmethod
    def _usage(
        windows: tuple[Window, ...], events: list[tuple[float, int]], now: float
    ) -> tuple[WindowUsage, ...]:
        return tuple(
            WindowUsage(w.seconds, w.limit, sum(c for at, c in events if at > now - w.seconds))
            for w in windows
        )

    def windows(self, scope: str) -> tuple[Window, ...]:
        with self._lock:
            return self._load(scope)[0]

    def acquire(self, scope: str, cost: int = 1) -> Grant:
        """Record one call of ``cost`` now if every window and any Retry-After allow it."""
        _check_scope(scope)
        if cost < 1:
            raise ValueError("cost must be >= 1")
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                grant = self._acquire_locked(scope, cost)
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            self._db.execute("COMMIT")
        return grant

    def _acquire_locked(self, scope: str, cost: int) -> Grant:
        windows, blocked_until = self._load(scope)
        too_small = [w for w in windows if cost > w.limit]
        if too_small:
            raise CostTooHigh(
                f"cost {cost} exceeds the {too_small[0].label} limit {too_small[0].limit}"
            )
        now = self._clock()
        longest = windows[-1].seconds
        self._db.execute(
            "DELETE FROM rate_events WHERE scope = ? AND at <= ?", (scope, now - longest)
        )
        events = self._events(scope, now - longest)
        waits: list[tuple[float, str]] = []
        if blocked_until > now:
            waits.append((blocked_until - now, "retry_after"))
        waits += [(_wait_for(w, events, cost, now), w.label) for w in windows]
        wait, by = max(waits, key=lambda x: x[0])
        if wait > 0:
            return Grant(False, wait, by, self._usage(windows, events, now))
        self._db.execute(
            "INSERT INTO rate_events (scope, at, cost) VALUES (?, ?, ?)", (scope, now, cost)
        )
        return Grant(True, 0.0, None, self._usage(windows, [*events, (now, cost)], now))

    def penalize(self, scope: str, retry_after_seconds: float | None = None) -> float:
        """The vendor answered 429: block the scope; returns ``blocked_until`` (epoch s).

        Without a ``Retry-After`` the shortest window's length is used: it is the soonest
        the vendor's count can have moved. A shorter block never shortens an existing one.
        """
        _check_scope(scope)
        if retry_after_seconds is not None and retry_after_seconds < 0:
            raise ValueError("retry_after_seconds must be >= 0")
        with self._lock:
            windows, blocked_until = self._load(scope)
            seconds = windows[0].seconds if retry_after_seconds is None else retry_after_seconds
            until = max(blocked_until, self._clock() + seconds)
            self._db.execute(
                "UPDATE rate_scopes SET blocked_until = ? WHERE scope = ?", (until, scope)
            )
        return until

    def usage(self, scope: str) -> dict[str, object]:
        """Current use per window and any Retry-After block, for a dashboard or alert."""
        _check_scope(scope)
        now = self._clock()
        with self._lock:
            windows, blocked_until = self._load(scope)
            events = self._events(scope, now - windows[-1].seconds)
        return {
            "scope": scope,
            "blocked_until": blocked_until if blocked_until > now else None,
            "windows": [
                {"seconds": u.seconds, "limit": u.limit, "used": u.used, "remaining": u.remaining}
                for u in self._usage(windows, events, now)
            ],
        }
