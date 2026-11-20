"""Dead-letter store for items that did not land, with a replay that refuses unsafe rows.

n8n's error workflow carries no items, so the lead that failed is named nowhere a person
looks. Each per-item stop from ``/v1/decide`` (or a node's error output) becomes one row
here, keyed by ``(scope, key)``:

    intake(row)   -> one open row per (scope, key); a repeat failure bumps ``count`` and
                     overwrites outcome / reason instead of adding a second row
    replay(ids)   -> hands back payloads only for rows that are safe to send again:
                     ``exhausted`` always, ``deferred`` once ``retry_at`` has passed;
                     ``unknown`` (may have landed) and ``failed`` (vendor rejected it) are
                     refused until someone reads the vendor back / fixes the data
    resolve(id, landed=)  -> a read-back: landed -> ``resolved``; not landed -> the row
                     becomes safe to resend (``unknown`` -> ``exhausted``)
    discard(id)   -> closed without sending (bad data, unsubscribed, duplicate)

``safe_to_resend`` is always recomputed from the outcome here, never trusted from the
caller. A replayed row stays attached to its key: if the replay fails again, the next
intake reopens the same row (``replays`` counts how often), and ``max_replays`` stops a
poison item from cycling forever. Like an SQS redrive, a replay starts a fresh attempt
sequence (attempt 1) for the item; ``first_seen_at`` keeps the original failure time.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

DlqOutcome = Literal["failed", "exhausted", "deferred", "unknown"]
DlqState = Literal["open", "replayed", "resolved", "discarded"]
ReplayStatus = Literal[
    "replay", "not_due", "unsafe_to_resend", "replay_limit", "not_open", "not_found"
]

OUTCOMES: frozenset[str] = frozenset({"failed", "exhausted", "deferred", "unknown"})
SAFE_OUTCOMES: frozenset[str] = frozenset({"exhausted", "deferred"})
MAX_PAYLOAD_BYTES = 64 * 1024

_SCHEMA = """
CREATE TABLE IF NOT EXISTS dlq_rows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope TEXT NOT NULL,
    key TEXT NOT NULL,
    outcome TEXT NOT NULL,
    reason TEXT NOT NULL,
    attempts INTEGER NOT NULL,
    last_status INTEGER,
    retry_at REAL,
    payload TEXT,
    source TEXT,
    state TEXT NOT NULL CHECK (state IN ('open', 'replayed', 'resolved', 'discarded')),
    count INTEGER NOT NULL,
    replays INTEGER NOT NULL DEFAULT 0,
    first_seen_at REAL NOT NULL,
    last_seen_at REAL NOT NULL,
    closed_at REAL,
    note TEXT
)
"""
_INDEX = "CREATE INDEX IF NOT EXISTS dlq_scope_key ON dlq_rows (scope, key, state)"
_ACTIVE = ("open", "replayed")


class RowNotFound(Exception):
    """resolve / discard on an id that does not exist."""


class RowClosed(Exception):
    """resolve / discard on a row that is already resolved or discarded."""


@dataclass(frozen=True)
class DlqRow:
    id: int
    scope: str
    key: str
    outcome: DlqOutcome
    reason: str
    attempts: int
    last_status: int | None
    retry_at: float | None
    payload: Any
    source: dict[str, Any]
    state: DlqState
    count: int
    replays: int
    first_seen_at: float
    last_seen_at: float
    closed_at: float | None
    note: str | None

    @property
    def safe_to_resend(self) -> bool:
        return self.outcome in SAFE_OUTCOMES

    def as_dict(self) -> dict[str, Any]:
        out = {name: getattr(self, name) for name in self.__dataclass_fields__}
        out["safe_to_resend"] = self.safe_to_resend
        return out


@dataclass(frozen=True)
class ReplayItem:
    id: int
    status: ReplayStatus
    scope: str | None = None
    key: str | None = None
    payload: Any = None
    retry_at: float | None = None
    reason: str | None = None

    @property
    def go(self) -> bool:
        return self.status == "replay"


def _row(r: tuple[Any, ...]) -> DlqRow:
    (id_, scope, key, outcome, reason, attempts, last_status, retry_at, payload, source) = r[:10]
    (state, count, replays, first, last, closed, note) = r[10:]
    return DlqRow(
        id=id_,
        scope=scope,
        key=key,
        outcome=outcome,
        reason=reason,
        attempts=attempts,
        last_status=last_status,
        retry_at=retry_at,
        payload=json.loads(payload) if payload is not None else None,
        source=json.loads(source) if source else {},
        state=state,
        count=count,
        replays=replays,
        first_seen_at=first,
        last_seen_at=last,
        closed_at=closed,
        note=note,
    )


_COLS = (
    "id, scope, key, outcome, reason, attempts, last_status, retry_at, payload, source, "
    "state, count, replays, first_seen_at, last_seen_at, closed_at, note"
)


class DlqStore:
    """Dead-letter rows on one SQLite file (``":memory:"`` for tests)."""

    def __init__(
        self,
        path: str | Path = ":memory:",
        *,
        max_replays: int = 3,
        busy_timeout: float = 10.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if max_replays < 1:
            raise ValueError("max_replays must be >= 1")
        self.path = str(path)
        self.max_replays = max_replays
        self._clock = clock
        self._lock = threading.Lock()
        self._db = sqlite3.connect(
            self.path, timeout=busy_timeout, isolation_level=None, check_same_thread=False
        )
        if self.path != ":memory:":
            self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute(_SCHEMA)
        self._db.execute(_INDEX)

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> DlqStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _get(self, row_id: int) -> DlqRow | None:
        r = self._db.execute(f"SELECT {_COLS} FROM dlq_rows WHERE id = ?", (row_id,)).fetchone()
        return _row(r) if r else None

    def get(self, row_id: int) -> DlqRow | None:
        with self._lock:
            return self._get(row_id)

    def intake(
        self,
        row: Mapping[str, Any],
        *,
        scope: str,
        payload: Any = None,
        source: Mapping[str, Any] | None = None,
    ) -> DlqRow:
        """Store one stop (a ``dlq_row`` from ``decide``) for ``(scope, key)``.

        An active (open or replayed) row for the same scope + key is reopened and updated,
        so retries of one lead never pile up as separate rows. ``ok`` is rejected: what
        landed does not belong here.
        """
        key = str(row.get("key") or "").strip()
        scope = scope.strip()
        if not scope or not key:
            raise ValueError("scope and key must be non-blank")
        outcome = row.get("outcome")
        if outcome not in OUTCOMES:
            raise ValueError(f"outcome must be one of {sorted(OUTCOMES)}, got {outcome!r}")
        reason = str(row.get("reason") or outcome)
        attempts = int(row.get("attempts") or 1)
        last_status = row.get("last_status")
        retry_at = row.get("retry_at")
        if outcome == "deferred" and retry_at is None:
            raise ValueError("a deferred row needs retry_at")
        body = json.dumps(payload, sort_keys=True) if payload is not None else None
        if body is not None and len(body.encode()) > MAX_PAYLOAD_BYTES:
            raise ValueError(f"payload larger than {MAX_PAYLOAD_BYTES} bytes")
        src = json.dumps(dict(source or {}), sort_keys=True)
        now = self._clock()
        with self._lock:
            active = self._db.execute(
                "SELECT id, payload FROM dlq_rows WHERE scope = ? AND key = ? "
                "AND state IN (?, ?) ORDER BY id DESC LIMIT 1",
                (scope, key, *_ACTIVE),
            ).fetchone()
            if active is None:
                cur = self._db.execute(
                    "INSERT INTO dlq_rows (scope, key, outcome, reason, attempts, last_status, "
                    "retry_at, payload, source, state, count, first_seen_at, last_seen_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', 1, ?, ?)",
                    (
                        scope,
                        key,
                        outcome,
                        reason,
                        attempts,
                        last_status,
                        retry_at,
                        body,
                        src,
                        now,
                        now,
                    ),
                )
                row_id = int(cur.lastrowid or 0)
            else:
                row_id = int(active[0])
                self._db.execute(
                    "UPDATE dlq_rows SET outcome = ?, reason = ?, attempts = ?, last_status = ?, "
                    "retry_at = ?, payload = ?, source = ?, state = 'open', count = count + 1, "
                    "last_seen_at = ? WHERE id = ?",
                    (
                        outcome,
                        reason,
                        attempts,
                        last_status,
                        retry_at,
                        body if body is not None else active[1],
                        src,
                        now,
                        row_id,
                    ),
                )
            stored = self._get(row_id)
        assert stored is not None
        return stored

    def rows(
        self,
        *,
        state: DlqState | None = "open",
        scope: str | None = None,
        outcome: DlqOutcome | None = None,
        limit: int = 500,
    ) -> list[DlqRow]:
        """Rows oldest first (by first failure)."""
        sql = f"SELECT {_COLS} FROM dlq_rows WHERE 1 = 1 "
        args: list[Any] = []
        for column, value in (("state", state), ("scope", scope), ("outcome", outcome)):
            if value is not None:
                sql += f"AND {column} = ? "
                args.append(value)
        sql += "ORDER BY first_seen_at, id LIMIT ?"
        args.append(limit)
        with self._lock:
            return [_row(r) for r in self._db.execute(sql, args).fetchall()]

    def replay(
        self, ids: Iterable[int] | None = None, *, scope: str | None = None, dry_run: bool = False
    ) -> list[ReplayItem]:
        """Payloads to send again, one verdict per row.

        ``ids=None`` takes every open row (optionally in ``scope``). Rows handed out move to
        ``replayed`` (``replays + 1``) unless ``dry_run``; refused rows stay as they are.
        """
        now = self._clock()
        out: list[ReplayItem] = []
        with self._lock:
            if ids is None:
                sql = f"SELECT {_COLS} FROM dlq_rows WHERE state = 'open' "
                args: list[Any] = []
                if scope is not None:
                    sql += "AND scope = ? "
                    args.append(scope)
                rows: list[DlqRow | int] = [
                    _row(r)
                    for r in self._db.execute(sql + "ORDER BY first_seen_at, id", args).fetchall()
                ]
            else:
                rows = [self._get(i) or i for i in dict.fromkeys(ids)]
            for r in rows:
                if isinstance(r, int):
                    out.append(ReplayItem(r, "not_found"))
                    continue
                item = self._verdict(r, now, scope)
                if item.go and not dry_run:
                    self._db.execute(
                        "UPDATE dlq_rows SET state = 'replayed', replays = replays + 1 "
                        "WHERE id = ? AND state = 'open'",
                        (r.id,),
                    )
                out.append(item)
        return out

    def _verdict(self, r: DlqRow, now: float, scope: str | None) -> ReplayItem:
        def refuse(status: ReplayStatus, reason: str) -> ReplayItem:
            return ReplayItem(r.id, status, r.scope, r.key, retry_at=r.retry_at, reason=reason)

        if r.state != "open" or (scope is not None and r.scope != scope):
            return refuse("not_open", f"row is {r.state}")
        if not r.safe_to_resend:
            why = (
                "may have landed: read the vendor back, then resolve(landed=...)"
                if r.outcome == "unknown"
                else "vendor rejected it: fix the data, then discard or re-intake"
            )
            return refuse("unsafe_to_resend", why)
        if r.replays >= self.max_replays:
            return refuse("replay_limit", f"replayed {r.replays} times already")
        if r.outcome == "deferred" and r.retry_at is not None and r.retry_at > now:
            return refuse("not_due", "retry_at has not passed")
        return ReplayItem(r.id, "replay", r.scope, r.key, r.payload, r.retry_at, r.reason)

    def _active(self, row_id: int) -> DlqRow:
        r = self._get(row_id)
        if r is None:
            raise RowNotFound(f"dlq row {row_id} not found")
        if r.state not in _ACTIVE:
            raise RowClosed(f"dlq row {row_id} is {r.state}")
        return r

    def resolve(self, row_id: int, *, landed: bool, note: str | None = None) -> DlqRow:
        """Record a read-back. Landed -> resolved; not landed -> safe to resend, still open."""
        with self._lock:
            r = self._active(row_id)
            if landed:
                self._db.execute(
                    "UPDATE dlq_rows SET state = 'resolved', closed_at = ?, note = ? WHERE id = ?",
                    (self._clock(), note, row_id),
                )
            elif r.outcome == "failed":
                raise ValueError("a failed row was rejected by the vendor: fix it or discard it")
            else:
                outcome = "exhausted" if r.outcome == "unknown" else r.outcome
                self._db.execute(
                    "UPDATE dlq_rows SET outcome = ?, reason = ?, state = 'open', note = ? "
                    "WHERE id = ?",
                    (outcome, f"{r.reason}; read back: not landed", note, row_id),
                )
            stored = self._get(row_id)
        assert stored is not None
        return stored

    def discard(self, row_id: int, *, note: str | None = None) -> DlqRow:
        with self._lock:
            self._active(row_id)
            self._db.execute(
                "UPDATE dlq_rows SET state = 'discarded', closed_at = ?, note = ? WHERE id = ?",
                (self._clock(), note, row_id),
            )
            stored = self._get(row_id)
        assert stored is not None
        return stored

    def summary(self, *, scope: str | None = None) -> dict[str, Any]:
        """Counts for an alert: open rows by outcome, unsafe rows, oldest open age."""
        now = self._clock()
        where, args = ("WHERE scope = ? ", (scope,)) if scope is not None else ("", ())
        with self._lock:
            states = dict(
                self._db.execute(
                    f"SELECT state, COUNT(*) FROM dlq_rows {where}GROUP BY state", args
                ).fetchall()
            )
            open_by = dict(
                self._db.execute(
                    "SELECT outcome, COUNT(*) FROM dlq_rows "
                    + (where + "AND " if where else "WHERE ")
                    + "state = 'open' GROUP BY outcome",
                    args,
                ).fetchall()
            )
            oldest = self._db.execute(
                "SELECT MIN(first_seen_at) FROM dlq_rows "
                + (where + "AND " if where else "WHERE ")
                + "state = 'open'",
                args,
            ).fetchone()[0]
        by_state = dict.fromkeys(("open", "replayed", "resolved", "discarded"), 0)
        by_state.update(states)
        by_outcome = dict.fromkeys(sorted(OUTCOMES), 0)
        by_outcome.update(open_by)
        return {
            "states": by_state,
            "open_by_outcome": by_outcome,
            "open_unsafe": by_outcome["unknown"] + by_outcome["failed"],
            "oldest_open_age_seconds": round(now - oldest, 3) if oldest is not None else None,
        }
