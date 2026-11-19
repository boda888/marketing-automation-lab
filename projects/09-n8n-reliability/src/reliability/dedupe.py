"""Cross-run dedupe that marks a key done only after the step it guards succeeded.

n8n's Remove Duplicates ("Remove Items Processed in Previous Executions") records a key when
the node runs (``checkProcessedAndRecord``), before anything downstream: a push that fails
after it leaves the lead marked as seen, and the next run drops it. It also throws once
stored keys + batch size exceed ``historySize``. This store splits the mark in two:

    claim(scope, key)        -> reserved (go, with a token) | committed (skip: already done)
                                | in_flight (another execution holds it) | stale (see below)
    commit(scope, key, token) after the guarded call succeeded
    release(scope, key, token) after it certainly did not land (safe to try again later)

A reservation that is neither committed nor released within ``stale_after`` seconds is
``stale``: the holder died or ended ``unknown``, so the call may have landed. It is never
handed to a new caller automatically; ``resolve(scope, key, landed=...)`` records the
result of a read-back (landed -> committed, not landed -> released). ``pending()`` lists
reservations for a watchdog.

Tokens stop one execution from committing or releasing another one's reservation. Scopes
keep the same key apart per target (``smartlead:campaign-42``, ``hubspot:contacts``), which
matches PRACTICE's "dedupe at campaign level, global suppression is a separate list".
"""

from __future__ import annotations

import secrets
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

ClaimStatus = Literal["reserved", "committed", "in_flight", "stale"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS dedupe_keys (
    scope TEXT NOT NULL,
    key TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('reserved', 'committed')),
    token TEXT NOT NULL,
    reserved_at REAL NOT NULL,
    committed_at REAL,
    PRIMARY KEY (scope, key)
)
"""


class TokenMismatch(Exception):
    """commit / release with a token that does not hold the reservation."""


class NotReserved(Exception):
    """commit / release / resolve on a key that has no reservation."""


class AlreadyCommitted(Exception):
    """release on a committed key: what landed cannot be un-marked."""


class StillInFlight(Exception):
    """resolve on a reservation younger than ``stale_after`` without ``force``."""


@dataclass(frozen=True)
class Claim:
    status: ClaimStatus
    token: str | None  # only on ``reserved``: pass it to commit / release
    since: float  # reserved_at, or committed_at for ``committed``
    age_seconds: float

    @property
    def go(self) -> bool:
        return self.status == "reserved"


@dataclass(frozen=True)
class Reservation:
    scope: str
    key: str
    reserved_at: float
    age_seconds: float
    stale: bool


def _check(scope: str, key: str) -> None:
    if not scope.strip() or not key.strip():
        raise ValueError("scope and key must be non-blank")


class KeyStore:
    """Reserve -> commit key store on one SQLite file (``":memory:"`` for tests)."""

    def __init__(
        self,
        path: str | Path = ":memory:",
        *,
        stale_after: float = 900.0,
        busy_timeout: float = 10.0,
        clock: Callable[[], float] = time.time,
        new_token: Callable[[], str] = lambda: secrets.token_urlsafe(16),
    ) -> None:
        if stale_after <= 0:
            raise ValueError("stale_after must be > 0")
        self.path = str(path)
        self.stale_after = stale_after
        self._clock = clock
        self._new_token = new_token
        self._lock = threading.Lock()
        self._db = sqlite3.connect(
            self.path, timeout=busy_timeout, isolation_level=None, check_same_thread=False
        )
        if self.path != ":memory:":
            self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute(_SCHEMA)

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> KeyStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _row(self, scope: str, key: str) -> tuple[str, str, float, float | None] | None:
        return self._db.execute(
            "SELECT state, token, reserved_at, committed_at FROM dedupe_keys "
            "WHERE scope = ? AND key = ?",
            (scope, key),
        ).fetchone()

    def claim(self, scope: str, key: str, *, stale_after: float | None = None) -> Claim:
        _check(scope, key)
        limit = self.stale_after if stale_after is None else stale_after
        now = self._clock()
        token = self._new_token()
        with self._lock:
            cur = self._db.execute(
                "INSERT OR IGNORE INTO dedupe_keys (scope, key, state, token, reserved_at) "
                "VALUES (?, ?, 'reserved', ?, ?)",
                (scope, key, token, now),
            )
            if cur.rowcount == 1:
                return Claim("reserved", token, now, 0.0)
            row = self._row(scope, key)
        assert row is not None
        state, _, reserved_at, committed_at = row
        if state == "committed":
            assert committed_at is not None
            return Claim("committed", None, committed_at, now - committed_at)
        age = now - reserved_at
        return Claim("stale" if age >= limit else "in_flight", None, reserved_at, age)

    def commit(self, scope: str, key: str, token: str) -> None:
        """Mark done. Repeating a commit with the same token is a no-op."""
        _check(scope, key)
        with self._lock:
            row = self._row(scope, key)
            if row is None:
                raise NotReserved(f"{scope}/{key} is not reserved")
            state, held, _, _ = row
            if not secrets.compare_digest(held, token):
                raise TokenMismatch(f"{scope}/{key} is held by another token")
            if state == "reserved":
                self._db.execute(
                    "UPDATE dedupe_keys SET state = 'committed', committed_at = ? "
                    "WHERE scope = ? AND key = ?",
                    (self._clock(), scope, key),
                )

    def release(self, scope: str, key: str, token: str) -> None:
        """Drop a reservation whose call certainly did not land; committed keys stay."""
        _check(scope, key)
        with self._lock:
            row = self._row(scope, key)
            if row is None:
                raise NotReserved(f"{scope}/{key} is not reserved")
            state, held, _, _ = row
            if not secrets.compare_digest(held, token):
                raise TokenMismatch(f"{scope}/{key} is held by another token")
            if state == "committed":
                raise AlreadyCommitted(f"{scope}/{key} is committed; it cannot be released")
            self._db.execute(
                "DELETE FROM dedupe_keys WHERE scope = ? AND key = ? AND state = 'reserved'",
                (scope, key),
            )

    def resolve(
        self, scope: str, key: str, *, landed: bool, force: bool = False
    ) -> ClaimStatus | None:
        """Record a read-back for a stale reservation nobody will finish.

        ``landed=True`` -> committed; ``False`` -> released (the next claim reserves it).
        Returns the new state (``None`` when released). A committed key is left alone. A
        reservation younger than ``stale_after`` raises ``StillInFlight`` unless ``force``
        (its holder may still commit it).
        """
        _check(scope, key)
        with self._lock:
            row = self._row(scope, key)
            if row is None:
                raise NotReserved(f"{scope}/{key} is not reserved")
            if row[0] == "committed":
                return "committed"
            if not force and self._clock() - row[2] < self.stale_after:
                raise StillInFlight(f"{scope}/{key} was reserved less than stale_after ago")
            if landed:
                self._db.execute(
                    "UPDATE dedupe_keys SET state = 'committed', committed_at = ? "
                    "WHERE scope = ? AND key = ?",
                    (self._clock(), scope, key),
                )
                return "committed"
            self._db.execute(
                "DELETE FROM dedupe_keys WHERE scope = ? AND key = ?",
                (scope, key),
            )
            return None

    def pending(self, *, scope: str | None = None, older_than: float = 0.0) -> list[Reservation]:
        """Open reservations at least ``older_than`` seconds old, oldest first."""
        now = self._clock()
        sql = "SELECT scope, key, reserved_at FROM dedupe_keys WHERE state = 'reserved' "
        args: list[object] = []
        if scope is not None:
            sql += "AND scope = ? "
            args.append(scope)
        sql += "AND reserved_at <= ? ORDER BY reserved_at, scope, key"
        args.append(now - older_than)
        with self._lock:
            rows = self._db.execute(sql, args).fetchall()
        return [
            Reservation(s, k, at, now - at, stale=now - at >= self.stale_after) for s, k, at in rows
        ]

    def counts(self, *, scope: str | None = None) -> dict[str, int]:
        sql = "SELECT state, COUNT(*) FROM dedupe_keys "
        args: tuple[str, ...] = ()
        if scope is not None:
            sql += "WHERE scope = ? "
            args = (scope,)
        with self._lock:
            rows = self._db.execute(sql + "GROUP BY state", args).fetchall()
        out = {"reserved": 0, "committed": 0}
        out.update(dict(rows))
        return out
