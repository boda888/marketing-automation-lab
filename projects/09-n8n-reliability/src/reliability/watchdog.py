"""Watchdog for schedules that stop without an error: a dead man's switch per workflow.

n8n's error workflow fires only when an execution fails. A schedule that never fires at
all — workflow deactivated, trigger lost after a restart or an import, a worker that died
mid-run — produces no failed execution and therefore no alert. Here each scheduled
workflow pings at the end of a run (optionally also at the start) and a separate check
raises an alert when a ping is overdue, the way cron monitors such as Healthchecks define
it (*period* = expected time between pings, *grace* = extra time before alerting):

    configure(name, period, grace)
    ping(name, "start")   -> optional; a run that started and did not finish within
                             grace is ``down`` (hung or crashed mid-run)
    ping(name, "ok")      -> the run finished
    check()               -> state per watch + the transitions worth an alert:
                             ``down`` once when it goes down, again every ``realert``
                             while it stays down, ``recovered`` once on the next ok ping

States: ``new`` (no ok ping yet, inside period + grace), ``up``, ``late`` (past period,
inside grace: no alert yet), ``down``, ``paused``. Unlike Healthchecks' ``new``, a watch
that never got an ok ping goes ``down`` after period + grace counted from when it was
configured: a schedule that never ran once is exactly the silent failure to catch.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

WatchState = Literal["new", "up", "late", "down", "paused"]
Signal = Literal["start", "ok"]
Transition = Literal["down", "still_down", "recovered"]

DEFAULT_REALERT_SECONDS = 6 * 3600.0

_SCHEMA = """
CREATE TABLE IF NOT EXISTS watches (
    name TEXT PRIMARY KEY,
    period REAL NOT NULL,
    grace REAL NOT NULL,
    configured_at REAL NOT NULL,
    last_start_at REAL,
    last_ok_at REAL,
    last_execution TEXT,
    paused INTEGER NOT NULL DEFAULT 0,
    down_since REAL,
    alerted_at REAL
)
"""
_COLS = (
    "name, period, grace, configured_at, last_start_at, last_ok_at, last_execution, paused, "
    "down_since, alerted_at"
)


class UnknownWatch(Exception):
    """A ping for a name that was never configured: fail loud, a typo is not a new watch."""


@dataclass(frozen=True)
class WatchStatus:
    name: str
    state: WatchState
    reason: str
    period_seconds: float
    grace_seconds: float
    last_ok_at: float | None
    last_start_at: float | None
    last_execution: str | None
    overdue_seconds: float  # how far past period + grace (0 unless down)
    down_since: float | None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class WatchAlert:
    name: str
    transition: Transition
    text: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _fmt(seconds: float) -> str:
    seconds = int(max(seconds, 0))
    if seconds >= 3600:
        return f"{seconds // 3600}h{seconds % 3600 // 60:02d}m"
    if seconds >= 60:
        return f"{seconds // 60}m"
    return f"{seconds}s"


def _state(row: tuple[Any, ...], now: float) -> tuple[WatchState, str, float]:
    _, period, grace, configured, started, ok, _, paused, _, _ = row
    if paused:
        return "paused", "paused by a person", 0.0
    if started is not None and (ok is None or started > ok) and now - started > grace:
        return "down", f"started {_fmt(now - started)} ago and never finished", now - started
    if ok is None:
        due = configured + period + grace
        if now > due:
            return (
                "down",
                f"no finished run since it was configured {_fmt(now - configured)} ago",
                now - due,
            )
        return "new", "waiting for the first run", 0.0
    age = now - ok
    if age <= period:
        return "up", f"last run finished {_fmt(age)} ago", 0.0
    if age <= period + grace:
        return "late", f"last run finished {_fmt(age)} ago (period {_fmt(period)})", 0.0
    return (
        "down",
        f"last run finished {_fmt(age)} ago (period {_fmt(period)})",
        age - period - grace,
    )


class Watchdog:
    """Watches on one SQLite file; ``check()`` is safe to run from several schedulers."""

    def __init__(
        self,
        path: str | Path = ":memory:",
        *,
        realert_seconds: float = DEFAULT_REALERT_SECONDS,
        busy_timeout: float = 10.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if realert_seconds <= 0:
            raise ValueError("realert_seconds must be > 0")
        self.path = str(path)
        self.realert_seconds = realert_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._db = sqlite3.connect(
            self.path, timeout=busy_timeout, isolation_level=None, check_same_thread=False
        )
        if self.path != ":memory:":
            self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute(_SCHEMA)

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> Watchdog:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _rows(self, name: str | None = None) -> list[tuple[Any, ...]]:
        if name is None:
            return self._db.execute(f"SELECT {_COLS} FROM watches ORDER BY name").fetchall()
        return self._db.execute(f"SELECT {_COLS} FROM watches WHERE name = ?", (name,)).fetchall()

    def configure(self, name: str, *, period_seconds: float, grace_seconds: float) -> WatchStatus:
        """Create or re-time a watch; pings already received are kept."""
        if not name or len(name) > 200:
            raise ValueError("name must be 1-200 characters")
        if period_seconds <= 0 or grace_seconds < 0:
            raise ValueError("period_seconds must be > 0 and grace_seconds >= 0")
        now = self._clock()
        with self._lock:
            self._db.execute(
                "INSERT INTO watches (name, period, grace, configured_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET period = excluded.period, "
                "grace = excluded.grace",
                (name, period_seconds, grace_seconds, now),
            )
            (row,) = self._rows(name)
        return self._status(row, now)

    def ping(
        self, name: str, signal: Signal = "ok", *, execution: str | None = None
    ) -> WatchStatus:
        if signal not in ("start", "ok"):
            raise ValueError("signal must be 'start' or 'ok'")
        now = self._clock()
        column = "last_start_at" if signal == "start" else "last_ok_at"
        with self._lock:
            cur = self._db.execute(
                f"UPDATE watches SET {column} = ?, last_execution = ? WHERE name = ?",
                (now, execution, name),
            )
            if cur.rowcount == 0:
                raise UnknownWatch(name)
            (row,) = self._rows(name)
        return self._status(row, now)

    def pause(self, name: str, paused: bool = True) -> WatchStatus:
        """Pause for a known problem; un-pausing clears the down episode."""
        now = self._clock()
        with self._lock:
            cur = self._db.execute(
                "UPDATE watches SET paused = ?, down_since = NULL, alerted_at = NULL "
                "WHERE name = ?",
                (int(paused), name),
            )
            if cur.rowcount == 0:
                raise UnknownWatch(name)
            (row,) = self._rows(name)
        return self._status(row, now)

    def _status(self, row: tuple[Any, ...], now: float) -> WatchStatus:
        state, reason, overdue = _state(row, now)
        return WatchStatus(
            name=row[0],
            state=state,
            reason=reason,
            period_seconds=row[1],
            grace_seconds=row[2],
            last_ok_at=row[5],
            last_start_at=row[4],
            last_execution=row[6],
            overdue_seconds=round(overdue, 3),
            down_since=row[8],
        )

    def statuses(self) -> list[WatchStatus]:
        now = self._clock()
        with self._lock:
            rows = self._rows()
        return [self._status(r, now) for r in rows]

    def check(self) -> tuple[list[WatchStatus], list[WatchAlert]]:
        """States now, plus the alerts this check owes; records what it alerted."""
        now = self._clock()
        alerts: list[WatchAlert] = []
        statuses: list[WatchStatus] = []
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                for row in self._rows():
                    status = self._status(row, now)
                    down_since, alerted_at = row[8], row[9]
                    name = status.name
                    if status.state == "down":
                        if down_since is None:
                            down_since, alerted_at = now, now
                            alerts.append(
                                WatchAlert(
                                    name, "down", f"schedule '{name}' is down: {status.reason}"
                                )
                            )
                        elif alerted_at is None or now - alerted_at >= self.realert_seconds:
                            alerted_at = now
                            alerts.append(
                                WatchAlert(
                                    name,
                                    "still_down",
                                    f"schedule '{name}' still down for {_fmt(now - down_since)}: "
                                    f"{status.reason}",
                                )
                            )
                    elif down_since is not None and status.state in ("up", "late"):
                        alerts.append(
                            WatchAlert(
                                name,
                                "recovered",
                                f"schedule '{name}' recovered after {_fmt(now - down_since)} down: "
                                f"{status.reason}",
                            )
                        )
                        down_since = alerted_at = None
                    self._db.execute(
                        "UPDATE watches SET down_since = ?, alerted_at = ? WHERE name = ?",
                        (down_since, alerted_at, name),
                    )
                    statuses.append(WatchStatus(**{**status.as_dict(), "down_since": down_since}))
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
        return statuses, alerts
