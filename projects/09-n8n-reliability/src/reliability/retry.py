"""Per-item retries for vendor calls, the part n8n's Retry On Fail cannot do safely.

n8n's Retry On Fail re-runs the **whole node with every input item**, 2–5 tries, a fixed
wait of at most 5 s, and never reads ``Retry-After`` (research R9 2026-11-01). The HTTP
Request node never retries a 429 either (R9 2026-11-12). Pushing 100 leads, a 429 on lead
57 then re-sends leads 1–56. This module retries **one item at a time**:

- an item that succeeded is never sent again;
- ``decide()`` is the single rule table. It takes one attempt's result and returns
  ``done`` / ``retry`` (with a wait) / ``stop`` (with an outcome), so an n8n loop
  (continue-on-fail → Code → Wait → same call) can use it item by item;
- **idempotent** calls (GET / HEAD / PUT / DELETE / OPTIONS per RFC 9110 §9.2.2, an upsert
  on a unique key, or a POST carrying an ``Idempotency-Key`` the vendor honours) retry on
  408, 425, 429, 500, 502, 503, 504 and on any transport error;
- **non-idempotent** calls (a plain create: sequencer add-lead, CRM create, AI call
  create) retry only when the request surely never left (``reached_vendor=False``) or on a
  429. A 5xx or a timeout after sending ends as ``unknown``: the create may have landed, so
  the caller must read back before any re-send, never retry blindly;
- with an ``Idempotency-Key``, 409 means the first request is still being processed
  (retry later) and 422 means the key was reused with a different payload (stop), as in
  draft-ietf-httpapi-idempotency-key-header;
- the wait is ``Retry-After`` when given (delta-seconds or HTTP-date), else exponential
  backoff ``base * factor ** (attempt - 1)`` capped at ``max_backoff_seconds`` with optional
  jitter. A ``Retry-After`` longer than ``max_wait_seconds`` is not slept in-process: the
  item ends ``deferred`` with ``retry_at``, for a DLQ row or an n8n Wait node.

Outcomes: ``ok``, ``failed`` (permanent: the vendor rejected it), ``exhausted`` (still
retryable, attempts used up), ``deferred`` (vendor asked for a longer wait), ``unknown``
(non-idempotent call may have landed). Every input item gets exactly one result, in order.
"""

from __future__ import annotations

import datetime as dt
import email.utils
import random
import time
from collections.abc import Callable, Hashable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

Outcome = Literal["ok", "failed", "exhausted", "deferred", "unknown"]
Action = Literal["done", "retry", "stop"]

IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "PUT", "DELETE", "OPTIONS", "TRACE"})
RETRYABLE_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})


@dataclass(frozen=True)
class HttpResult:
    """One attempt's HTTP answer (any status; transports must not raise on 4xx / 5xx)."""

    status: int
    headers: Mapping[str, str] = field(default_factory=dict)
    body: Any = None


class TransportError(Exception):
    """No HTTP answer (DNS, refused, reset, TLS, timeout).

    ``reached_vendor`` is False only when the request surely never left this machine
    (DNS failure, connection refused); a timeout or reset after sending is True.
    """

    def __init__(self, message: str = "", *, reached_vendor: bool = True) -> None:
        super().__init__(message)
        self.reached_vendor = reached_vendor


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 4
    base_seconds: float = 1.0
    factor: float = 2.0
    max_backoff_seconds: float = 60.0
    max_wait_seconds: float = 300.0  # longer Retry-After -> deferred, not slept
    jitter: float = 0.0  # 0..1: fraction of the backoff that is randomised (not Retry-After)

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if self.base_seconds < 0 or self.factor < 1 or self.max_backoff_seconds < 0:
            raise ValueError("base_seconds >= 0, factor >= 1, max_backoff_seconds >= 0")
        if self.max_wait_seconds < 0 or not 0 <= self.jitter <= 1:
            raise ValueError("max_wait_seconds >= 0 and 0 <= jitter <= 1")

    def backoff(self, attempt: int, rand: Callable[[], float] = random.random) -> float:
        """Wait after failed attempt number ``attempt`` (1-based), before jitter is applied."""
        raw = self.base_seconds * self.factor ** (attempt - 1)
        wait = min(raw, self.max_backoff_seconds)
        if self.jitter:
            wait = wait * (1 - self.jitter) + wait * self.jitter * rand()
        return wait


def is_idempotent(method: str, headers: Mapping[str, str] | None = None) -> bool:
    """RFC 9110 §9.2.2 idempotent methods, or any request carrying an ``Idempotency-Key``.

    Only pass headers with an ``Idempotency-Key`` when the vendor documents honouring it;
    a POST upsert on a unique property is idempotent too, but the caller must say so.
    """
    if method.upper() in IDEMPOTENT_METHODS:
        return True
    return any(k.lower() == "idempotency-key" and v.strip() for k, v in (headers or {}).items())


def retry_after_seconds(
    headers: Mapping[str, str], *, now: Callable[[], float] = time.time
) -> float | None:
    """``Retry-After`` in seconds (delta-seconds or HTTP-date); past date = 0, junk = None."""
    value = next((v for k, v in headers.items() if k.lower() == "retry-after"), None)
    if value is None:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.UTC)
    return max(0.0, when.timestamp() - now())


DEFAULT_POLICY = RetryPolicy()


@dataclass(frozen=True)
class Decision:
    action: Action
    outcome: Outcome | None = None  # set when action is "done" or "stop"
    wait_seconds: float = 0.0  # set when action is "retry"
    retry_at: float | None = None  # epoch seconds, set when outcome is "deferred"
    reason: str = ""

    @property
    def wait_ms(self) -> int:
        return round(self.wait_seconds * 1000)


def decide(
    result: HttpResult | TransportError,
    *,
    attempt: int,
    idempotent: bool,
    keyed: bool = False,
    policy: RetryPolicy | None = None,
    now: Callable[[], float] = time.time,
    rand: Callable[[], float] = random.random,
) -> Decision:
    """What to do after attempt number ``attempt`` (1-based) of one item.

    ``keyed`` = the request carried an ``Idempotency-Key`` (409 / 422 then follow the draft).
    """
    if attempt < 1:
        raise ValueError("attempt is 1-based")
    policy = policy or DEFAULT_POLICY
    idempotent = idempotent or keyed  # a key the vendor honours makes the POST replay-safe

    if isinstance(result, TransportError):
        if not result.reached_vendor:
            reason = "not_sent"
        elif idempotent:
            reason = "transport_error"
        else:
            return Decision("stop", "unknown", reason="transport_error_after_send")
        return _retry_or_exhaust(reason, None, attempt, policy, now, rand)

    status = result.status
    if 200 <= status < 300:
        return Decision("done", "ok", reason=f"http_{status}")
    if keyed and status == 409:
        return _retry_or_exhaust("idempotency_key_in_flight", result, attempt, policy, now, rand)
    if keyed and status == 422:
        return Decision("stop", "failed", reason="idempotency_key_reused")
    if status == 429:
        return _retry_or_exhaust("http_429", result, attempt, policy, now, rand)
    if status in RETRYABLE_STATUSES:
        if not idempotent:
            return Decision("stop", "unknown", reason=f"http_{status}_after_send")
        return _retry_or_exhaust(f"http_{status}", result, attempt, policy, now, rand)
    return Decision("stop", "failed", reason=f"http_{status}")


def _retry_or_exhaust(
    reason: str,
    result: HttpResult | None,
    attempt: int,
    policy: RetryPolicy,
    now: Callable[[], float],
    rand: Callable[[], float],
) -> Decision:
    hinted = retry_after_seconds(result.headers, now=now) if result is not None else None
    if hinted is not None and hinted > policy.max_wait_seconds:
        return Decision("stop", "deferred", retry_at=now() + hinted, reason=reason)
    if attempt >= policy.max_attempts:
        return Decision("stop", "exhausted", reason=reason)
    wait = hinted if hinted is not None else policy.backoff(attempt, rand)
    return Decision("retry", wait_seconds=wait, reason=reason)


@dataclass
class ItemResult:
    key: Hashable
    outcome: Outcome
    attempts: int
    reason: str
    last_status: int | None = None
    retry_at: float | None = None
    waited_seconds: float = 0.0
    response: HttpResult | None = None

    @property
    def safe_to_resend(self) -> bool:
        """False for ``unknown`` (may have landed) and ``failed`` (vendor rejected it)."""
        return self.outcome in {"exhausted", "deferred"}

    def dlq_row(self) -> dict[str, Any]:
        """Flat row for a dead-letter table / sheet; never called for ``ok`` items."""
        return {
            "key": str(self.key),
            "outcome": self.outcome,
            "reason": self.reason,
            "attempts": self.attempts,
            "last_status": self.last_status,
            "retry_at": self.retry_at,
            "safe_to_resend": self.safe_to_resend,
        }


@dataclass
class RunReport:
    results: list[ItemResult]

    def counts(self) -> dict[str, int]:
        out = dict.fromkeys(("ok", "failed", "exhausted", "deferred", "unknown"), 0)
        for r in self.results:
            out[r.outcome] += 1
        return out

    @property
    def calls(self) -> int:
        return sum(r.attempts for r in self.results)

    def dlq_rows(self) -> list[dict[str, Any]]:
        return [r.dlq_row() for r in self.results if r.outcome != "ok"]


def run_items(
    items: Iterable[Any],
    call: Callable[[Any], HttpResult],
    *,
    idempotent: bool,
    keyed: bool = False,
    key: Callable[[Any], Hashable] = lambda item: item,
    policy: RetryPolicy | None = None,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.time,
    rand: Callable[[], float] = random.random,
) -> RunReport:
    """Send each item with ``call``; retry only that item, per ``decide()``.

    ``call`` returns an ``HttpResult`` for any status and raises ``TransportError`` when
    there is no answer. Any other exception propagates: it is a bug, not a vendor failure.
    """
    policy = policy or DEFAULT_POLICY
    results: list[ItemResult] = []
    for item in items:
        attempt, waited = 0, 0.0
        while True:
            attempt += 1
            try:
                result: HttpResult | TransportError = call(item)
            except TransportError as exc:
                result = exc
            d = decide(
                result,
                attempt=attempt,
                idempotent=idempotent,
                keyed=keyed,
                policy=policy,
                now=now,
                rand=rand,
            )
            if d.action == "retry":
                sleep(d.wait_seconds)
                waited += d.wait_seconds
                continue
            response = result if isinstance(result, HttpResult) else None
            assert d.outcome is not None
            results.append(
                ItemResult(
                    key=key(item),
                    outcome=d.outcome,
                    attempts=attempt,
                    reason=d.reason,
                    last_status=response.status if response else None,
                    retry_at=d.retry_at,
                    waited_seconds=waited,
                    response=response,
                )
            )
            break
    return RunReport(results)
