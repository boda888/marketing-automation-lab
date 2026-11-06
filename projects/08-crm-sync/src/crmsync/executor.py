"""Send an ``UpsertPlan`` to HubSpot: retry only the rows that can be retried, one rate budget.

Rules:

- every request waits on a ``RateBudget`` that the caller shares across all writers to the
  same portal (HubSpot limits per app / portal, not per process — research R7);
- after each call, ``parse_upsert_response`` decides per row; only rows that came back
  ``failed`` **and** ``retryable`` (429, 5xx, missing from a 200 / 207 body, unparseable body)
  are sent again, as a smaller batch. A row HubSpot rejected (``errors[]``, other 4xx) is
  never re-sent;
- a timeout or a dropped connection is retried too: the plan holds upserts on a unique
  property, so a second identical request updates the same contact instead of creating one.
  That is why this executor accepts only ``UpsertPlan`` batches and offers no create;
- 429 waits ``Retry-After`` (delta-seconds) when given, else exponential backoff; every wait
  is capped by ``max_wait_seconds``;
- the report has exactly one outcome per input row, in input order, skipped rows included.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from .hubspot import HubSpotBatch, PlannedRow, UpsertPlan, parse_upsert_response
from .models import Outcome, RowOutcome, count_outcomes

HUBSPOT_API = "https://api.hubapi.com"


@dataclass(frozen=True)
class HttpCall:
    method: str
    url: str
    json: object = None
    headers: dict[str, str] = field(default_factory=dict, repr=False)  # holds the token


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: object = None
    headers: dict[str, str] = field(default_factory=dict)


class TransportError(Exception):
    """No HTTP response (refused, reset, TLS, DNS)."""


class Transport(Protocol):
    def send(self, call: HttpCall) -> HttpResponse: ...


class RateBudget:
    """At most ``max_calls`` requests in any sliding ``per_seconds`` window.

    Share one instance between every writer for the same HubSpot portal. The default
    (100 calls / 10 s) is the lab's conservative assumption for a private app; HubSpot's
    published per-tier limits were not opened (search summary only, R7 2026-10-29).
    """

    def __init__(
        self,
        max_calls: int = 100,
        per_seconds: float = 10.0,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if max_calls < 1 or per_seconds <= 0:
            raise ValueError("max_calls must be >= 1 and per_seconds > 0")
        self.max_calls = max_calls
        self.per_seconds = per_seconds
        self._clock = clock
        self._sleep = sleep
        self._calls: deque[float] = deque()
        self.waited = 0.0

    def acquire(self) -> None:
        while True:
            now = self._clock()
            while self._calls and now - self._calls[0] >= self.per_seconds:
                self._calls.popleft()
            if len(self._calls) < self.max_calls:
                self._calls.append(now)
                return
            wait = self.per_seconds - (now - self._calls[0])
            self.waited += wait
            self._sleep(wait)


@dataclass
class UpsertReport:
    outcomes: list[RowOutcome]
    calls: int = 0
    retried_rows: int = 0  # row re-sends, summed over all retry rounds

    def summary(self) -> dict[str, object]:
        return {
            **count_outcomes(self.outcomes),
            "calls": self.calls,
            "retried_rows": self.retried_rows,
            "still_retryable": sum(1 for o in self.outcomes if o.retryable),
        }


def _retry_after(headers: dict[str, str]) -> float | None:
    value = next((v for k, v in headers.items() if k.lower() == "retry-after"), None)
    if value is None or not value.strip().isdigit():
        return None
    return float(value.strip())


def execute_upserts(
    plan: UpsertPlan,
    transport: Transport,
    *,
    token: str,
    budget: RateBudget,
    base_url: str = HUBSPOT_API,
    max_attempts: int = 3,
    max_wait_seconds: float = 30.0,
    sleep: Callable[[float], None] = time.sleep,
) -> UpsertReport:
    """Send every batch; re-send only failed-and-retryable rows, up to ``max_attempts``."""
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")
    if not token:
        raise ValueError("a HubSpot private-app token is required")
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    final: dict[str, RowOutcome] = {}
    report = UpsertReport(outcomes=[])

    for batch in plan.batches:
        pending: tuple[PlannedRow, ...] = batch.rows
        for attempt in range(1, max_attempts + 1):
            current = HubSpotBatch(batch.id_property, pending, batch.method, batch.path)
            budget.acquire()
            report.calls += 1
            if attempt > 1:
                report.retried_rows += len(pending)
            call = HttpCall(current.method, base_url + current.path, current.body(), headers)
            wait_hint: float | None = None
            try:
                response = transport.send(call)
            except (TransportError, TimeoutError) as exc:
                kind = "timeout" if isinstance(exc, TimeoutError) else "transport_error"
                outcomes = [
                    RowOutcome(r.row_id, r.key, Outcome.FAILED, reason=kind, retryable=True)
                    for r in pending
                ]
            else:
                outcomes = parse_upsert_response(current, response.status, response.body)
                if response.status == 429:
                    wait_hint = _retry_after(response.headers)
            retry: list[PlannedRow] = []
            for row, outcome in zip(pending, outcomes, strict=True):
                final[row.trace_id] = outcome
                if outcome.outcome is Outcome.FAILED and outcome.retryable:
                    retry.append(row)
            if not retry or attempt == max_attempts:
                break
            wait = wait_hint if wait_hint is not None else 2.0 ** (attempt - 1)
            sleep(min(max_wait_seconds, wait))
            pending = tuple(retry)

    sent = {r.row_id: final[r.trace_id] for b in plan.batches for r in b.rows}
    skipped = {o.row_id: o for o in plan.skipped}
    report.outcomes = [sent.get(rid) or skipped[rid] for rid in plan.row_order]
    return report
