"""Attio "assert a record": one idempotent write per person, one outcome per row.

API shape from the generated types and docs comments in ``attio-ts-sdk`` 2.3.3 (npm,
generated from Attio's OpenAPI; docs.attio.com and api.attio.com were not reachable from
the build environment on 2026-11-08):

- ``PUT /v2/objects/{object}/records?matching_attribute=<slug or id>`` with
  ``{"data": {"values": {slug: [value, ...]}}}``. The matching attribute must be unique.
  Found → that record is updated; not found → a record is created. **One record per call**
  (no batch assert);
- ``200`` answers ``{"data": {"id": {"workspace_id", "object_id", "record_id"},
  "created_at", "web_url", "values"}}``. Nothing in it says whether the record was created
  or updated, so a landed row is ``upserted``, never guessed as ``created``;
- documented errors: ``400 value_not_found`` and ``404 not_found`` (unknown object), each
  ``{"status_code", "type", "code", "message"}``;
- **multiselect overwrite:** "If the matching attribute is a multiselect attribute, new
  values will be added and existing values will not be deleted. For any other multiselect
  attribute, all values will be either created or deleted as necessary to match the list
  of supplied values." A sync that sends one ``tags`` value therefore wipes every other tag.

Rules this module enforces:

- a declared multiselect attribute (other than the matching one) is only sent when the
  caller names it in ``overwrite``; otherwise the row is skipped with
  ``multiselect_overwrite:<slugs>`` instead of silently deleting CRM values. A list value on
  an undeclared attribute is skipped too (``undeclared_list:<slugs>``): declare it first;
- blank and placeholder values are dropped, never sent over a CRM value; ``0`` and
  ``False`` are kept as real values (JSON numbers / booleans, not strings);
- fixture e-mails (``.example`` …) never leave unless ``allow_fixtures``; a key goes out at
  most once per run;
- assert on a unique attribute is idempotent, so 408 / 429 / 5xx, timeouts and resets are
  re-sent. ``attio-ts-sdk``'s own retry defaults retry only GET / HEAD / OPTIONS, so a PUT
  that hit a 429 there fails unless the caller retries it — this executor does, per row.
"""

from __future__ import annotations

import time
import urllib.parse
from collections.abc import Callable, Collection
from dataclasses import dataclass, field

from .executor import (
    HttpCall,
    RateBudget,
    Transport,
    TransportError,
    UpsertReport,
    retry_after_seconds,
)
from .models import (
    PROPERTY_NAME,
    CrmRow,
    Outcome,
    RowOutcome,
    Scalar,
    clean,
    is_fixture_email,
    normalise_email,
)

ATTIO_API = "https://api.attio.com"
EMAIL_ATTRIBUTE = "email_addresses"
# Retryable because the request is an assert on a unique attribute (idempotent).
RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})

AttioValue = str | int | float | bool


def attio_value(value: Scalar) -> AttioValue | None:
    """Typed JSON value, or ``None`` = do not send. ``0`` / ``False`` are real values."""
    if isinstance(value, bool | int | float):
        return value
    return clean(value)


@dataclass(frozen=True)
class AttioAssert:
    row_id: str
    key: str
    object: str
    matching_attribute: str
    values: dict[str, list[object]]
    method: str = "PUT"

    @property
    def path(self) -> str:
        obj = urllib.parse.quote(self.object, safe="")
        query = urllib.parse.urlencode({"matching_attribute": self.matching_attribute})
        return f"/v2/objects/{obj}/records?{query}"

    def body(self) -> dict:
        return {"data": {"values": self.values}}


@dataclass
class AssertPlan:
    calls: list[AttioAssert] = field(default_factory=list)
    skipped: list[RowOutcome] = field(default_factory=list)
    row_order: list[str] = field(default_factory=list)


def _row_key(row: CrmRow, matching_attribute: str) -> str | None:
    if matching_attribute == EMAIL_ATTRIBUTE:
        source = row.email if row.email is not None else row.properties.get(EMAIL_ATTRIBUTE)
        return normalise_email(source)
    return clean(row.properties.get(matching_attribute))


def _values(raw: object) -> list[AttioValue]:
    items = raw if isinstance(raw, list | tuple) else [raw]
    return [v for item in items if (v := attio_value(item)) is not None]


def plan_asserts(
    rows: list[CrmRow],
    *,
    object: str = "people",
    matching_attribute: str = EMAIL_ATTRIBUTE,
    multiselect: Collection[str] = (EMAIL_ATTRIBUTE, "phone_numbers"),
    overwrite: Collection[str] = (),
    allow_fixtures: bool = False,
) -> AssertPlan:
    """One assert call per sendable row; every other row gets a ``skipped`` outcome.

    ``multiselect`` names the object's multiselect attributes (from its schema; the default
    covers Attio's standard ``people`` e-mail and phone attributes). ``overwrite`` names the
    ones this sync owns and may replace wholesale.
    """
    if not PROPERTY_NAME.match(matching_attribute):
        raise ValueError(f"invalid matching_attribute {matching_attribute!r}")
    if not clean(object):
        raise ValueError("object is required (slug or UUID, e.g. 'people')")
    multi = set(multiselect) - {matching_attribute}
    owned = set(overwrite)

    plan = AssertPlan()
    seen: dict[str, str] = {}
    row_ids: set[str] = set()
    for row in rows:
        if row.row_id in row_ids:
            raise ValueError(f"row_id {row.row_id!r} is not unique: outcomes are keyed by it")
        row_ids.add(row.row_id)
        plan.row_order.append(row.row_id)
        key = _row_key(row, matching_attribute)

        def skip(reason: str, row: CrmRow = row, key: str | None = key) -> None:
            plan.skipped.append(RowOutcome(row.row_id, key, Outcome.SKIPPED, reason=reason))

        if key is None:
            skip(f"missing_key:{matching_attribute}")
            continue
        email = normalise_email(row.email) if row.email is not None else None
        email = email or (key if matching_attribute == EMAIL_ATTRIBUTE else None)
        if email and is_fixture_email(email) and not allow_fixtures:
            skip("fixture")
            continue
        if key.lower() in seen:
            skip(f"duplicate_key:first_row={seen[key.lower()]}")
            continue
        bad = sorted(name for name in row.properties if not PROPERTY_NAME.match(name))
        if bad:
            skip("invalid_property_name:" + ",".join(bad))
            continue
        values = {
            name: vals
            for name, raw in row.properties.items()
            if name != matching_attribute and (vals := _values(raw))
        }
        undeclared = sorted(
            n
            for n, raw in row.properties.items()
            if isinstance(raw, list | tuple) and n in values and n not in multi
        )
        if undeclared:
            skip("undeclared_list:" + ",".join(undeclared))
            continue
        clobber = sorted(n for n in values if n in multi and n not in owned)
        if clobber:
            skip("multiselect_overwrite:" + ",".join(clobber))
            continue
        if matching_attribute == EMAIL_ATTRIBUTE:
            values[EMAIL_ATTRIBUTE] = [{"email_address": key}]
        else:
            values[matching_attribute] = [key]
        seen[key.lower()] = row.row_id
        plan.calls.append(AttioAssert(row.row_id, key, object, matching_attribute, values))
    return plan


def _fail(call: AttioAssert, reason: str, retryable: bool) -> RowOutcome:
    return RowOutcome(call.row_id, call.key, Outcome.FAILED, reason=reason, retryable=retryable)


def parse_assert_response(call: AttioAssert, status: int, body: object) -> RowOutcome:
    """The row's outcome from the status and the body of one assert call."""
    if status in RETRYABLE_STATUS or status >= 500:
        return _fail(call, f"http_{status}", retryable=True)
    if status != 200:
        reason = f"http_{status}"
        if isinstance(body, dict):
            code, message = clean(body.get("code")), clean(body.get("message"))
            reason += f" {code}" if code else ""
            reason += f": {message}" if message else ""
        return _fail(call, reason, retryable=False)
    data = body.get("data") if isinstance(body, dict) else None
    ident = data.get("id") if isinstance(data, dict) else None
    record_id = clean(ident.get("record_id")) if isinstance(ident, dict) else None
    if record_id is None:
        return _fail(call, "unparseable_body", retryable=True)
    return RowOutcome(call.row_id, call.key, Outcome.UPSERTED, crm_id=record_id)


def execute_asserts(
    plan: AssertPlan,
    transport: Transport,
    *,
    token: str,
    budget: RateBudget,
    base_url: str = ATTIO_API,
    max_attempts: int = 3,
    max_wait_seconds: float = 30.0,
    sleep: Callable[[float], None] = time.sleep,
) -> UpsertReport:
    """Send each assert; re-send only failed-and-retryable rows, up to ``max_attempts``.

    Share one ``budget`` across every writer to the workspace. ``RateBudget(25, 1.0)``
    matches the 25 writes / s per workspace that search summaries attribute to Attio's
    rate-limiting guide (page not opened).
    """
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")
    if not token:
        raise ValueError("an Attio API token is required")
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    report = UpsertReport(outcomes=[])
    sent: dict[str, RowOutcome] = {}

    for call in plan.calls:
        for attempt in range(1, max_attempts + 1):
            budget.acquire()
            report.calls += 1
            if attempt > 1:
                report.retried_rows += 1
            http = HttpCall(call.method, base_url + call.path, call.body(), headers)
            wait_hint: float | None = None
            try:
                response = transport.send(http)
            except (TransportError, TimeoutError) as exc:
                if isinstance(exc, TimeoutError):
                    kind = "timeout"
                elif not exc.reached_vendor:
                    kind = "not_sent"
                else:
                    kind = "transport_error"
                outcome = _fail(call, kind, retryable=True)
            else:
                outcome = parse_assert_response(call, response.status, response.body)
                if response.status == 429:
                    wait_hint = retry_after_seconds(response.headers)
            sent[call.row_id] = outcome
            if not outcome.retryable or attempt == max_attempts:
                break
            wait = wait_hint if wait_hint is not None else 2.0 ** (attempt - 1)
            sleep(min(max_wait_seconds, wait))

    skipped = {o.row_id: o for o in plan.skipped}
    report.outcomes = [sent.get(rid) or skipped[rid] for rid in plan.row_order]
    return report
