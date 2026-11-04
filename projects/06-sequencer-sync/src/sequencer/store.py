"""Persistent push log on SQLite (standard library): the same reserve -> commit contract.

``InMemoryPushLog`` forgets everything when the process ends, so a run that crashed after
a 5xx would push the same leads again. ``SqlitePushLog`` keeps every key on disk:

- ``reserve`` is one ``INSERT OR IGNORE`` on the primary key, so of two runs (processes
  or threads, each with its own connection) racing for one key exactly one wins;
- ``commit`` marks a key committed whether or not it was reserved first (``reconcile``
  commits keys it finds in the campaign);
- ``release`` deletes only a *reserved* key; a committed key is never released;
- ``pending(older_than=...)`` lists reservations still waiting for ``reconcile``, oldest
  first, for a watchdog ("outcome_unknown for more than an hour → alert").

Every statement runs in autocommit mode with a busy timeout, and the file uses WAL so
readers do not block the writer. Keys are the sha256 idempotency keys only: no email
or profile URL is stored.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .plan import PushState

_SCHEMA = """
CREATE TABLE IF NOT EXISTS push_log (
    key TEXT PRIMARY KEY,
    state TEXT NOT NULL CHECK (state IN ('reserved', 'committed')),
    reserved_at REAL,
    committed_at REAL
)
"""


@dataclass(frozen=True)
class PendingKey:
    key: str
    reserved_at: float
    age_seconds: float


class SqlitePushLog:
    """``PushLog`` backed by one SQLite file (or ``":memory:"`` for tests)."""

    def __init__(
        self,
        path: str | Path,
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
        self._db.execute(_SCHEMA)

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> SqlitePushLog:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def state(self, key: str) -> PushState | None:
        with self._lock:
            row = self._db.execute("SELECT state FROM push_log WHERE key = ?", (key,)).fetchone()
        return PushState(row[0]) if row else None

    def reserve(self, key: str) -> bool:
        """Reserve ``key``; ``False`` if it is already reserved or committed (by anyone)."""
        with self._lock:
            cur = self._db.execute(
                "INSERT OR IGNORE INTO push_log (key, state, reserved_at) VALUES (?, ?, ?)",
                (key, PushState.RESERVED.value, self._clock()),
            )
        return cur.rowcount == 1

    def commit(self, key: str) -> None:
        now = self._clock()
        with self._lock:
            self._db.execute(
                "INSERT INTO push_log (key, state, committed_at) VALUES (?, ?, ?) "
                "ON CONFLICT (key) DO UPDATE SET state = excluded.state, "
                "committed_at = excluded.committed_at",
                (key, PushState.COMMITTED.value, now),
            )

    def release(self, key: str) -> None:
        """Drop a reservation; a committed key stays committed."""
        with self._lock:
            self._db.execute(
                "DELETE FROM push_log WHERE key = ? AND state = ?",
                (key, PushState.RESERVED.value),
            )

    def pending(self, older_than: float = 0.0) -> list[PendingKey]:
        """Reserved keys whose reservation is at least ``older_than`` seconds old."""
        now = self._clock()
        with self._lock:
            rows = self._db.execute(
                "SELECT key, reserved_at FROM push_log WHERE state = ? AND reserved_at <= ? "
                "ORDER BY reserved_at, key",
                (PushState.RESERVED.value, now - older_than),
            ).fetchall()
        return [PendingKey(k, at, now - at) for k, at in rows]

    def counts(self) -> dict[str, int]:
        with self._lock:
            rows = self._db.execute(
                "SELECT state, COUNT(*) FROM push_log GROUP BY state"
            ).fetchall()
        return dict(rows)
