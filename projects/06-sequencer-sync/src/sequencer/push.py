"""Execute a ``PushPlan`` through an adapter and a transport, then reconcile what landed.

Retry rule: a lead-creating POST is retried only when the vendor certainly did not
process it (HTTP 429). A 5xx or a timeout may have created the leads, so the batch is
kept as ``outcome_unknown`` with its idempotency keys still reserved; ``reconcile()``
settles it from the campaign's member list instead of a blind second POST.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from enum import StrEnum
from typing import Protocol

from .adapters import HttpRequest, ScopeMapping
from .models import normalise_linkedin_url
from .plan import PlannedLead, PushLog, PushPlan


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: object = None
    headers: dict[str, str] = field(default_factory=dict)


class TransportError(Exception):
    """No HTTP response. ``reached_vendor=False`` only when the request surely never left."""

    def __init__(self, message: str, *, reached_vendor: bool = True) -> None:
        super().__init__(message)
        self.reached_vendor = reached_vendor


class Transport(Protocol):
    def send(self, request: HttpRequest) -> HttpResponse: ...


class BatchStatus(StrEnum):
    ACCEPTED = "accepted"  # 2xx: the vendor took the batch (it may still skip some leads)
    REJECTED = "rejected"  # 4xx except 429: our request is wrong; keys released
    RATE_LIMITED = "rate_limited"  # 429 after the retries: nothing processed; keys released
    NOT_SENT = "not_sent"  # transport failed before the request left; keys released
    OUTCOME_UNKNOWN = "outcome_unknown"  # 5xx / 3xx / timeout: keys stay reserved


@dataclass
class BatchResult:
    status: BatchStatus
    url: str
    leads: tuple[PlannedLead, ...]
    http_status: int | None = None
    attempts: int = 1
    error: str = ""


@dataclass
class PushReport:
    vendor: str
    campaign_id: str
    requested_scope: str
    effective_scope: str
    skipped_before_send: dict[str, int]
    person_key: str = "email"  # what reconcile() matches member lists on
    batches: list[BatchResult] = field(default_factory=list)
    added: int | None = None  # set by reconcile() with a before-snapshot
    skipped_by_vendor: int | None = None  # accepted but absent afterwards
    confirmed_unknown: int = 0  # outcome_unknown leads found in the campaign afterwards
    cleared_unknown: int = 0  # outcome_unknown leads absent afterwards: safe to push again

    def _count(self, *statuses: BatchStatus) -> int:
        return sum(len(b.leads) for b in self.batches if b.status in statuses)

    @property
    def sent(self) -> int:
        return self._count(BatchStatus.ACCEPTED)

    @property
    def unknown(self) -> int:
        return self._count(BatchStatus.OUTCOME_UNKNOWN)

    @property
    def failed(self) -> int:
        return self._count(BatchStatus.REJECTED, BatchStatus.RATE_LIMITED, BatchStatus.NOT_SENT)

    def summary(self) -> dict[str, object]:
        return {
            "vendor": self.vendor,
            "campaign_id": self.campaign_id,
            "requested_scope": self.requested_scope,
            "effective_scope": self.effective_scope,
            "person_key": self.person_key,
            "skipped_before_send": self.skipped_before_send,
            "sent": self.sent,
            "outcome_unknown": self.unknown,
            "failed": self.failed,
            "added": self.added,
            "skipped_by_vendor": self.skipped_by_vendor,
            "batches": [
                {
                    "status": b.status.value,
                    "http_status": b.http_status,
                    "leads": len(b.leads),
                    "attempts": b.attempts,
                    "url": b.url,
                    "error": b.error,
                }
                for b in self.batches
            ],
        }


def retry_after_seconds(headers: dict[str, str], now: float | None = None) -> float | None:
    """``Retry-After`` as delta-seconds or HTTP-date (RFC 9110 §10.2.3); ``None`` if absent."""
    value = next((v for k, v in headers.items() if k.lower() == "retry-after"), None)
    if value is None:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value).timestamp()
    except (TypeError, ValueError, IndexError):
        return None
    return max(0.0, when - (time.time() if now is None else now))


def _error_text(body: object) -> str:
    if isinstance(body, dict):
        for key in ("message", "error", "detail"):
            if body.get(key):
                return str(body[key])[:300]
    return str(body)[:300] if body else ""


def execute_plan(
    plan: PushPlan,
    adapter: ScopeMapping,
    transport: Transport,
    push_log: PushLog,
    *,
    max_rate_limit_retries: int = 2,
    max_wait_seconds: float = 60.0,
    sleep: Callable[[float], None] = time.sleep,
) -> PushReport:
    """Send ``plan.to_send`` in vendor-sized batches; one ``BatchResult`` per request."""
    if adapter.vendor != plan.vendor:
        raise ValueError(f"plan is for {plan.vendor!r}, adapter is {adapter.vendor!r}")
    report = PushReport(
        vendor=plan.vendor,
        campaign_id=plan.campaign.campaign_id,
        requested_scope=plan.dedupe_scope.value,
        effective_scope=adapter.effective_scope(plan.dedupe_scope).value,
        skipped_before_send=plan.skipped_by_reason,
        person_key=plan.person_key,
    )
    # Reserve first: a concurrent run holding a key keeps that lead out of this push.
    ready = [p for p in plan.to_send if push_log.reserve(p.key)]
    raced = len(plan.to_send) - len(ready)
    if raced:
        report.skipped_before_send = {
            **report.skipped_before_send,
            "push_pending": report.skipped_before_send.get("push_pending", 0) + raced,
        }

    for request in adapter.build(plan.campaign, ready, plan.dedupe_scope):
        result = _send(request, transport, max_rate_limit_retries, max_wait_seconds, sleep)
        for planned in request.leads:
            if result.status is BatchStatus.ACCEPTED:
                push_log.commit(planned.key)
            elif result.status is not BatchStatus.OUTCOME_UNKNOWN:
                push_log.release(planned.key)
        report.batches.append(result)
    return report


def _send(
    request: HttpRequest,
    transport: Transport,
    max_retries: int,
    max_wait: float,
    sleep: Callable[[float], None],
) -> BatchResult:
    url = request.redacted_url()
    attempts = 0
    while True:
        attempts += 1
        try:
            response = transport.send(request)
        except TransportError as exc:
            status = BatchStatus.OUTCOME_UNKNOWN if exc.reached_vendor else BatchStatus.NOT_SENT
            return BatchResult(status, url, request.leads, None, attempts, str(exc)[:300])
        except TimeoutError as exc:
            return BatchResult(
                BatchStatus.OUTCOME_UNKNOWN, url, request.leads, None, attempts, f"timeout: {exc}"
            )
        code = response.status
        if 200 <= code < 300:
            return BatchResult(BatchStatus.ACCEPTED, url, request.leads, code, attempts)
        if code == 429:
            wait = retry_after_seconds(response.headers)
            wait = min(max_wait, wait if wait is not None else 2.0 ** (attempts - 1))
            if attempts <= max_retries:
                sleep(wait)
                continue
            return BatchResult(
                BatchStatus.RATE_LIMITED,
                url,
                request.leads,
                code,
                attempts,
                _error_text(response.body),
            )
        # 5xx may have created the leads; a 3xx (never followed) may follow a processed POST.
        unknown = code >= 500 or 300 <= code < 400
        status = BatchStatus.OUTCOME_UNKNOWN if unknown else BatchStatus.REJECTED
        return BatchResult(status, url, request.leads, code, attempts, _error_text(response.body))


def _member_key(person_key: str) -> Callable[[str], str | None]:
    if person_key == "linkedin_url":
        return normalise_linkedin_url
    return lambda value: value.strip().lower() or None


def reconcile(
    report: PushReport,
    push_log: PushLog,
    members_after: Iterable[str],
    members_before: Iterable[str] | None = None,
) -> PushReport:
    """Settle the report against the campaign's members read back from the vendor.

    Members are emails, or profile URLs in any LinkedIn form when the report's
    ``person_key`` is ``linkedin_url``. ``outcome_unknown`` leads present afterwards are
    committed (they were created); absent ones are released so the next run may push them.
    With ``members_before`` (read before the push), ``added`` counts leads that are new in
    the campaign, and ``skipped_by_vendor`` counts accepted leads the vendor did not keep
    (its own dedupe or block lists) — the "sent vs added" gap that is otherwise invisible.
    """
    key = _member_key(report.person_key)

    def members(values: Iterable[str]) -> set[str]:
        return {k for k in (key(v) for v in values) if k}

    after = members(members_after)
    before = None if members_before is None else members(members_before)
    accepted: list[str] = []
    for batch in report.batches:
        for planned in batch.leads:
            member = key(planned.member) or ""
            if batch.status is BatchStatus.ACCEPTED:
                accepted.append(member)
            elif batch.status is BatchStatus.OUTCOME_UNKNOWN:
                if member in after:
                    push_log.commit(planned.key)
                    report.confirmed_unknown += 1
                    accepted.append(member)
                else:
                    push_log.release(planned.key)
                    report.cleared_unknown += 1
    report.skipped_by_vendor = sum(1 for m in accepted if m not in after)
    if before is not None:
        report.added = sum(1 for m in accepted if m in after and m not in before)
    return report
