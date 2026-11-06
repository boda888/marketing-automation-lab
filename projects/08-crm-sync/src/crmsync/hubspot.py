"""HubSpot contacts batch upsert: requests that cannot duplicate, and one outcome per row.

API shape from HubSpot's official Node client ``@hubspot/api-client`` 14.1.2 (generated from
HubSpot's OpenAPI; ``crm/contacts`` ``BatchApi.upsert`` and its models):

- ``POST /crm/v3/objects/contacts/batch/upsert`` with
  ``{"inputs": [{"id", "idProperty", "objectWriteTraceId", "properties"}]}``;
- the response (200 or **207**) has ``status`` (PENDING / PROCESSING / CANCELED / COMPLETE),
  ``results[]`` (each with ``id``, ``_new``, ``properties`` and the echoed
  ``objectWriteTraceId``), ``numErrors`` and ``errors[]`` (``category``, ``message``,
  ``context: {name: [values]}``). The SDK resolves 207 as success, so a green call does not
  mean every row landed.

Rules this module enforces:

- a row's outcome comes from the body, never from the HTTP status. Results are matched by
  the ``objectWriteTraceId`` we set; a row that is in neither ``results`` nor an error's
  ``context`` is ``failed / not_in_response``, never counted as written;
- at most 100 inputs per call (HubSpot batch limit, per search summary of the contacts
  guide; not confirmed from a page opened in this run);
- a key goes out at most once per run, so the response can always be mapped back;
- blank and placeholder values are dropped, never sent as ``""`` over a CRM value;
- upsert on a unique property is idempotent, so 429 / 5xx / missing rows are ``retryable``.
  Batch *create* is not and is deliberately not offered here.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from .models import (
    PROPERTY_NAME,
    CrmRow,
    Outcome,
    RowOutcome,
    clean,
    is_fixture_email,
    normalise_email,
    property_value,
)

HUBSPOT_BATCH_LIMIT = 100
UPSERT_PATH = "/crm/v3/objects/contacts/batch/upsert"


def trace_id(key: str) -> str:
    """Stable per-key trace id HubSpot echoes back on each result."""
    return "lab-" + hashlib.sha256(key.encode()).hexdigest()[:24]


@dataclass(frozen=True)
class PlannedRow:
    row_id: str
    key: str
    trace_id: str
    properties: dict[str, str]


@dataclass(frozen=True)
class HubSpotBatch:
    id_property: str
    rows: tuple[PlannedRow, ...]
    method: str = "POST"
    path: str = UPSERT_PATH

    def body(self) -> dict:
        return {
            "inputs": [
                {
                    "id": r.key,
                    "idProperty": self.id_property,
                    "objectWriteTraceId": r.trace_id,
                    "properties": r.properties,
                }
                for r in self.rows
            ]
        }


@dataclass
class UpsertPlan:
    batches: list[HubSpotBatch] = field(default_factory=list)
    skipped: list[RowOutcome] = field(default_factory=list)
    row_order: list[str] = field(default_factory=list)  # input row ids, in input order


def _row_key(row: CrmRow, id_property: str) -> str | None:
    if id_property == "email":
        return normalise_email(row.email if row.email is not None else row.properties.get("email"))
    return clean(row.properties.get(id_property))


def plan_upserts(
    rows: list[CrmRow],
    *,
    id_property: str = "email",
    batch_size: int = HUBSPOT_BATCH_LIMIT,
    allow_fixtures: bool = False,
) -> UpsertPlan:
    """Split rows into upsert batches; every row not sent gets a ``skipped`` outcome.

    ``id_property`` must be a unique property: ``email`` or the portal's own unique id
    (HubSpot's guide says partial upserts are not supported with ``email``; a custom unique
    property avoids that).
    """
    if not 1 <= batch_size <= HUBSPOT_BATCH_LIMIT:
        raise ValueError(f"batch_size must be 1..{HUBSPOT_BATCH_LIMIT}")
    if not PROPERTY_NAME.match(id_property):
        raise ValueError(f"invalid id_property {id_property!r}")

    plan = UpsertPlan()
    seen: dict[str, str] = {}
    ready: list[PlannedRow] = []
    row_ids: set[str] = set()
    for row in rows:
        if row.row_id in row_ids:
            raise ValueError(f"row_id {row.row_id!r} is not unique: outcomes are keyed by it")
        row_ids.add(row.row_id)
        plan.row_order.append(row.row_id)
        key = _row_key(row, id_property)

        def skip(reason: str, row: CrmRow = row, key: str | None = key) -> None:
            plan.skipped.append(RowOutcome(row.row_id, key, Outcome.SKIPPED, reason=reason))

        if key is None:
            skip(f"missing_key:{id_property}")
            continue
        email = normalise_email(row.email) if row.email is not None else None
        email = email or (key if id_property == "email" else None)
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
        props = {
            name: text
            for name, value in row.properties.items()
            if (text := property_value(value)) is not None
        }
        if email:
            props["email"] = email
        if id_property != "email":
            props[id_property] = key
        seen[key.lower()] = row.row_id
        ready.append(PlannedRow(row.row_id, key, trace_id(key.lower()), props))

    for start in range(0, len(ready), batch_size):
        plan.batches.append(HubSpotBatch(id_property, tuple(ready[start : start + batch_size])))
    return plan


def _fail(row: PlannedRow, reason: str, retryable: bool) -> RowOutcome:
    return RowOutcome(row.row_id, row.key, Outcome.FAILED, reason=reason, retryable=retryable)


def _error_reason(error: dict) -> str:
    category = clean(error.get("category")) or "ERROR"
    message = clean(error.get("message"))
    return f"{category}: {message}" if message else category


def parse_upsert_response(batch: HubSpotBatch, status: int, body: object) -> list[RowOutcome]:
    """One outcome per batch row, in batch order, from the HTTP status *and* the body."""
    if status == 429 or status >= 500:
        return [_fail(r, f"http_{status}", retryable=True) for r in batch.rows]
    if status not in (200, 207):
        reason = f"http_{status}"
        if isinstance(body, dict):
            reason += f" {_error_reason(body)}"
        return [_fail(r, reason, retryable=False) for r in batch.rows]
    if not isinstance(body, dict) or not isinstance(body.get("results", []), list):
        return [_fail(r, "unparseable_body", retryable=True) for r in batch.rows]

    by_trace = {r.trace_id: r for r in batch.rows}
    by_key = {r.key.lower(): r for r in batch.rows}
    landed: dict[str, RowOutcome] = {}
    for result in body.get("results") or []:
        if not isinstance(result, dict):
            continue
        row = by_trace.get(str(result.get("objectWriteTraceId")))
        if row is None:
            echoed = (result.get("properties") or {}).get(batch.id_property)
            row = by_key.get(str(echoed).strip().lower()) if echoed is not None else None
        if row is None or row.trace_id in landed:
            continue
        new = result.get("_new")
        outcome = {True: Outcome.CREATED, False: Outcome.UPDATED}.get(new, Outcome.UPSERTED)
        crm_id = clean(result.get("id"))
        landed[row.trace_id] = RowOutcome(row.row_id, row.key, outcome, crm_id=crm_id)

    errored: dict[str, RowOutcome] = {}
    unattributed: list[str] = []
    for error in body.get("errors") or []:
        if not isinstance(error, dict):
            continue
        values: set[str] = set()
        for items in (error.get("context") or {}).values():
            if isinstance(items, list):
                values.update(str(v).strip().lower() for v in items)
            elif items is not None:
                values.add(str(items).strip().lower())
        matched = False
        for row in batch.rows:
            if row.key.lower() in values or row.trace_id in values:
                matched = True
                if row.trace_id not in landed and row.trace_id not in errored:
                    errored[row.trace_id] = _fail(row, _error_reason(error), retryable=False)
        if not matched:
            unattributed.append(_error_reason(error))

    batch_status = str(body.get("status") or "").upper()
    missing = "not_in_response" if batch_status in ("", "COMPLETE") else f"batch_{batch_status}"
    if unattributed:
        missing += "; unattributed errors: " + " | ".join(unattributed)
    return [
        landed.get(r.trace_id) or errored.get(r.trace_id) or _fail(r, missing, retryable=True)
        for r in batch.rows
    ]
