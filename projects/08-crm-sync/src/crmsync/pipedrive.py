"""Pipedrive persons: cached search, then update or create, one writer, no blind re-create.

Pipedrive has no upsert. API shape from Pipedrive's official client ``pipedrive`` 36.3.1
(npm, 2026-11-06, ``dist/versions/v2``; base ``https://api.pipedrive.com/api/v2``, header
``x-api-token``):

- ``GET /persons/search?term=…&fields=email&exact_match=true`` ("not case sensitive",
  wraps ``/v1/itemSearch``) answers ``{"success", "data": {"items": [{"result_score",
  "item": {"id", "name", "emails": [str], …}}]}}``;
- ``PATCH /persons/{id}`` updates; ``POST /persons`` creates (``name`` required, ``emails``
  as ``[{"value", "primary", "label"}]``, custom fields under ``custom_fields`` keyed by
  their 40-hex field key). Both answer ``{"success", "data": Person}``. ``job_title``,
  ``notes``, ``birthday`` … answer 403 unless the company has contact sync on.

Rules this module enforces:

- a local email → person id cache is read first (search costs several times a write in
  Pipedrive's token budget per third-party summaries, R7); only a miss searches;
- one writer: search + create for a row run under one lock, so two rows (or threads
  sharing the lock) cannot both miss and both create. The lock is per process — separate
  n8n workers need one queue or a shared lock;
- search hits are re-checked: an item counts only if one of its ``emails`` equals the key.
  Two or more matching persons → ``failed / ambiguous_match`` (merge them first); the lab
  never picks one;
- **create is not idempotent.** It is sent once. 429 and a request that surely never left
  (``reached_vendor=False``) are retried; a timeout, reset or 5xx after sending →
  ``failed / outcome_unknown``, not retryable and not cached, so the next run searches
  before it could create again. Search (GET) and update (PATCH with the same values) are
  retried on 408 / 429 / 5xx and transport errors;
- a cached id that answers 404 on update is evicted and the row is searched again once;
- e-mails are never sent on update (the existing list is left alone); blank and
  placeholder values are never sent; fixtures and duplicate keys never leave.
"""

from __future__ import annotations

import re
import threading
import time
import urllib.parse
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from typing import Protocol

from .executor import (
    HttpCall,
    HttpResponse,
    RateBudget,
    Transport,
    TransportError,
    UpsertReport,
    retry_after_seconds,
)
from .models import (
    CrmRow,
    Outcome,
    RowOutcome,
    Scalar,
    clean,
    is_fixture_email,
    normalise_email,
)

PIPEDRIVE_API = "https://api.pipedrive.com/api/v2"
STANDARD_FIELDS = frozenset(
    {"name", "owner_id", "org_id", "visible_to", "job_title", "marketing_status"}
)
CUSTOM_FIELD_KEY = re.compile(r"^[0-9a-f]{40}$")
RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})


class PersonIdCache(Protocol):
    def get(self, email: str) -> str | None: ...
    def put(self, email: str, person_id: str) -> None: ...
    def evict(self, email: str) -> None: ...


@dataclass
class MemoryPersonCache:
    """In-process cache; swap for a sheet / Postgres table to keep it between runs."""

    ids: dict[str, str] = field(default_factory=dict)

    def get(self, email: str) -> str | None:
        return self.ids.get(email)

    def put(self, email: str, person_id: str) -> None:
        self.ids[email] = person_id

    def evict(self, email: str) -> None:
        self.ids.pop(email, None)


def _value(value: Scalar) -> str | int | float | bool | None:
    if isinstance(value, bool | int | float):
        return value
    return clean(value)


@dataclass(frozen=True)
class PersonWrite:
    row_id: str
    email: str
    fields: dict[str, object]  # standard fields, top level
    custom_fields: dict[str, object]

    def update_body(self) -> dict:
        body: dict[str, object] = dict(self.fields)
        if self.custom_fields:
            body["custom_fields"] = dict(self.custom_fields)
        return body

    def create_body(self) -> dict:
        body = self.update_body()
        body["emails"] = [{"value": self.email, "primary": True, "label": "work"}]
        return body


@dataclass
class PersonPlan:
    writes: list[PersonWrite] = field(default_factory=list)
    skipped: list[RowOutcome] = field(default_factory=list)
    row_order: list[str] = field(default_factory=list)


def plan_persons(rows: list[CrmRow], *, allow_fixtures: bool = False) -> PersonPlan:
    """One write per person e-mail; every other row gets a ``skipped`` outcome."""
    plan = PersonPlan()
    seen: dict[str, str] = {}
    row_ids: set[str] = set()
    for row in rows:
        if row.row_id in row_ids:
            raise ValueError(f"row_id {row.row_id!r} is not unique: outcomes are keyed by it")
        row_ids.add(row.row_id)
        plan.row_order.append(row.row_id)
        email = normalise_email(row.email if row.email is not None else row.properties.get("email"))

        def skip(reason: str, row: CrmRow = row, key: str | None = email) -> None:
            plan.skipped.append(RowOutcome(row.row_id, key, Outcome.SKIPPED, reason=reason))

        if email is None:
            skip("missing_key:email")
            continue
        if is_fixture_email(email) and not allow_fixtures:
            skip("fixture")
            continue
        if email in seen:
            skip(f"duplicate_key:first_row={seen[email]}")
            continue
        names = [n for n in row.properties if n != "email"]
        bad = sorted(n for n in names if n not in STANDARD_FIELDS and not CUSTOM_FIELD_KEY.match(n))
        if bad:
            skip("invalid_property_name:" + ",".join(bad))
            continue
        values = {n: v for n in names if (v := _value(row.properties[n])) is not None}
        std = {n: v for n, v in values.items() if n in STANDARD_FIELDS}
        custom = {n: v for n, v in values.items() if n not in STANDARD_FIELDS}
        seen[email] = row.row_id
        plan.writes.append(PersonWrite(row.row_id, email, std, custom))
    return plan


@dataclass
class PersonReport(UpsertReport):
    search_calls: int = 0
    cache_hits: int = 0

    def summary(self) -> dict[str, object]:
        return {
            **super().summary(),
            "search_calls": self.search_calls,
            "cache_hits": self.cache_hits,
        }


class _Failed(Exception):
    def __init__(self, reason: str, retryable: bool) -> None:
        super().__init__(reason)
        self.reason = reason
        self.retryable = retryable


def _api_reason(response: HttpResponse) -> str:
    reason = f"http_{response.status}"
    if isinstance(response.body, dict):
        message = clean(response.body.get("error")) or clean(response.body.get("message"))
        reason += f": {message}" if message else ""
    return reason


@dataclass
class _Sender:
    transport: Transport
    budget: RateBudget
    report: PersonReport
    headers: dict[str, str]
    max_attempts: int
    max_wait_seconds: float
    sleep: Callable[[float], None]

    def send(self, call: HttpCall, *, idempotent: bool) -> HttpResponse:
        """2xx response, or ``_Failed``. Non-idempotent calls re-send only when surely unsent."""
        for attempt in range(1, self.max_attempts + 1):
            last = attempt == self.max_attempts
            self.budget.acquire()
            self.report.calls += 1
            if attempt > 1:
                self.report.retried_rows += 1
            wait: float | None = None
            try:
                response = self.transport.send(call)
            except (TransportError, TimeoutError) as exc:
                never_left = isinstance(exc, TransportError) and not exc.reached_vendor
                if not (idempotent or never_left):
                    raise _Failed("outcome_unknown", retryable=False) from exc
                if last:
                    kind = "not_sent" if never_left else "timeout_or_transport_error"
                    raise _Failed(kind, retryable=True) from exc
            else:
                if 200 <= response.status < 300:
                    return response
                transient = response.status in RETRYABLE_STATUS or response.status >= 500
                if not transient:
                    raise _Failed(_api_reason(response), retryable=False)
                if not idempotent and response.status != 429:
                    # 5xx after a create was sent: Pipedrive may have written it.
                    raise _Failed("outcome_unknown", retryable=False)
                if last:
                    raise _Failed(_api_reason(response), retryable=True)
                if response.status == 429:
                    wait = retry_after_seconds(response.headers)
            self.sleep(
                min(self.max_wait_seconds, wait if wait is not None else 2.0 ** (attempt - 1))
            )
        raise AssertionError("unreachable")


def _person_id(body: object) -> str | None:
    data = body.get("data") if isinstance(body, dict) else None
    pid = data.get("id") if isinstance(data, dict) else None
    return str(pid) if isinstance(pid, int | str) and str(pid).strip() else None


def _search(sender: _Sender, base_url: str, email: str) -> list[str]:
    query = urllib.parse.urlencode({"term": email, "fields": "email", "exact_match": "true"})
    url = f"{base_url}/persons/search?{query}"
    response = sender.send(HttpCall("GET", url, None, sender.headers), idempotent=True)
    sender.report.search_calls += 1
    data = response.body.get("data") if isinstance(response.body, dict) else None
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise _Failed("unparseable_search", retryable=True)
    found: list[str] = []
    for entry in items:
        item = entry.get("item") if isinstance(entry, dict) else None
        if not isinstance(item, dict) or item.get("id") is None:
            continue
        emails = {normalise_email(e) for e in item.get("emails") or []}
        if email in emails and str(item["id"]) not in found:
            found.append(str(item["id"]))
    return found


def execute_persons(
    plan: PersonPlan,
    transport: Transport,
    *,
    token: str,
    budget: RateBudget,
    cache: PersonIdCache | None = None,
    lock: AbstractContextManager | None = None,
    base_url: str = PIPEDRIVE_API,
    max_attempts: int = 3,
    max_wait_seconds: float = 30.0,
    sleep: Callable[[float], None] = time.sleep,
) -> PersonReport:
    """Write each person: cache → search → update or create. One outcome per input row."""
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")
    if not token:
        raise ValueError("a Pipedrive API token is required")
    cache = cache if cache is not None else MemoryPersonCache()
    lock = lock if lock is not None else threading.Lock()
    report = PersonReport(outcomes=[])
    headers = {"x-api-token": token, "Content-Type": "application/json"}
    sender = _Sender(transport, budget, report, headers, max_attempts, max_wait_seconds, sleep)
    sent: dict[str, RowOutcome] = {}

    def call(method: str, url: str, body: object = None) -> HttpCall:
        return HttpCall(method, url, body, headers)

    def update(w: PersonWrite, pid: str) -> RowOutcome:
        body = w.update_body()
        if not body:  # nothing to change: the person exists, no call spent
            return RowOutcome(w.row_id, w.email, Outcome.UPDATED, crm_id=pid, reason="no_fields")
        url = f"{base_url}/persons/{urllib.parse.quote(pid, safe='')}"
        response = sender.send(call("PATCH", url, body), idempotent=True)
        return RowOutcome(
            w.row_id, w.email, Outcome.UPDATED, crm_id=_person_id(response.body) or pid
        )

    def write(w: PersonWrite) -> RowOutcome:
        cached = cache.get(w.email)
        if cached is not None:
            report.cache_hits += 1
            try:
                return update(w, cached)
            except _Failed as exc:
                if not exc.reason.startswith("http_404"):
                    raise
                cache.evict(w.email)  # merged or deleted since it was cached: search again
        with lock:
            found = _search(sender, base_url, w.email)
            if len(found) > 1:
                raise _Failed("ambiguous_match:" + ",".join(found), retryable=False)
            if found:
                cache.put(w.email, found[0])
                return update(w, found[0])
            if "name" not in w.fields:
                raise _Failed("missing_name_for_create", retryable=False)
            response = sender.send(
                call("POST", f"{base_url}/persons", w.create_body()), idempotent=False
            )
            pid = _person_id(response.body)
            if pid is None:
                # 2xx without an id: it may exist now. Search before any re-create.
                raise _Failed("outcome_unknown", retryable=False)
            cache.put(w.email, pid)
            return RowOutcome(w.row_id, w.email, Outcome.CREATED, crm_id=pid)

    for w in plan.writes:
        try:
            sent[w.row_id] = write(w)
        except _Failed as exc:
            sent[w.row_id] = RowOutcome(
                w.row_id, w.email, Outcome.FAILED, reason=exc.reason, retryable=exc.retryable
            )

    skipped = {o.row_id: o for o in plan.skipped}
    report.outcomes = [sent.get(rid) or skipped[rid] for rid in plan.row_order]
    return report
