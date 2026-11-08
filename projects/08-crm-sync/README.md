# 08 · CRM sync (HubSpot, Attio; Pipedrive next)

**The pain:** the CRM no longer matches reality. An n8n HTTP node goes green on HubSpot's
batch endpoint while some rows never landed: a batch returns **HTTP 207** and HubSpot's own
SDK resolves it as success, so failed rows sit unread in `errors[]`. Retrying a batch
*create* after a timeout makes duplicates; a blank sheet cell overwrites a good CRM value;
updates matched by company domain overwrite several people of one company.

Attio adds its own trap: its assert endpoint **replaces every multiselect attribute** other
than the matching one with the list you send, so a sync that writes one `tags` value wipes
the record's other tags.

v0.0.4: HubSpot contacts **batch upsert** planning, response parsing, an executor that
re-sends only retryable rows under one rate budget per portal, and a stdlib HTTP transport
(`UrllibTransport`) that never follows redirects; **Attio assert** (one idempotent `PUT` per
person) with a multiselect-overwrite guard. One `created / updated / upserted / failed /
skipped` outcome per input row. Tested against a local HTTP server and offline fakes; never
against a live HubSpot portal or Attio workspace. No service or n8n workflow yet.

## Architecture

```
CrmRow(row_id, email, properties)                     caller's row id kept end to end
   │
plan_upserts(rows, id_property="email" | custom unique property)
   │   skipped: missing_key · fixture (.example …) · duplicate_key (first row named)
   │            · invalid_property_name
   │   blank / placeholder values dropped → never blank a CRM field ("Unknown", "", None)
   │   ≤ 100 inputs per batch, each with objectWriteTraceId = hash(key)
   ▼
HubSpotBatch.body()  →  POST /crm/v3/objects/contacts/batch/upsert   (caller sends it)
   │
parse_upsert_response(batch, status, body)            one RowOutcome per row, batch order
       results[] matched by echoed objectWriteTraceId (fallback: echoed id property)
         _new true → created · false → updated · missing → upserted
       errors[] matched by context values → failed (not retryable)
       neither → failed "not_in_response" (retryable: upsert is idempotent)
       status != COMPLETE → failed "batch_<STATUS>" for rows without a result
       429 / 5xx → all failed, retryable · other 4xx → all failed, not retryable
```

```
execute_upserts(plan, transport, token=…, budget=RateBudget(100, 10))
   │   every call waits on the shared RateBudget (sliding window, one per portal)
   │   parse_upsert_response → re-send only failed + retryable rows, as a smaller batch
   │     (429: Retry-After seconds, else 1 s / 2 s / 4 s …, capped by max_wait_seconds;
   │      timeout / connection reset: retried — the call is an upsert on a unique key)
   │   rejected rows (errors[], other 4xx) are never re-sent
   ▼
UpsertReport.outcomes   one per input row, input order, skipped rows included
UpsertReport.summary()  counts + calls + retried_rows + still_retryable
```

```
UrllibTransport(timeout=30)                          stdlib urllib, no dependency
   │   every status is an HttpResponse (4xx / 5xx keep body + headers → Retry-After, 207 errors[])
   │   3xx returned, never followed: urllib re-sends a redirected POST as a body-less GET
   │     → rows failed "http_30x", not retryable, not re-sent
   │   no response → TransportError(reached_vendor=…)
   │     False: DNS / refused / TLS cert → reason "not_sent"
   │     True:  timeout / reset / disconnect → HubSpot may have written; upsert re-send is safe
   ▼   error text = method + URL without query string; the Authorization header never appears
```

```
plan_asserts(rows, object="people", matching_attribute="email_addresses" | unique slug,
             multiselect=("email_addresses", "phone_numbers", …), overwrite=(…))
   │   skipped: missing_key · fixture · duplicate_key · invalid_property_name
   │            · multiselect_overwrite:<slugs>  (declared multiselect, not owned by this sync)
   │            · undeclared_list:<slugs>        (list value on an attribute not declared)
   │   values typed (0 / False kept as JSON number / bool), blanks + placeholders dropped
   ▼
AttioAssert → PUT /v2/objects/{object}/records?matching_attribute=…  {"data": {"values": …}}
   │
execute_asserts(plan, transport, token=…, budget=RateBudget(25, 1.0))   one call per row
       200 + data.id.record_id → upserted (Attio does not say created vs updated)
       200 without record_id → failed unparseable_body, retryable
       408 / 429 / 5xx, timeout, reset, not_sent → re-sent (assert is idempotent)
       400 value_not_found, 404 not_found, 3xx, other 4xx → failed, sent once
       429 waits Retry-After (seconds or HTTP-date), capped by max_wait_seconds
```

Why upsert and not create: a re-sent upsert on a unique property updates the same contact;
a re-sent create after a 5xx or timeout can create it twice. Batch create is deliberately
not offered.

## Run it

```bash
python -m pip install -e ".[dev]"
python -m pytest -q
```

```python
from crmsync import CrmRow, plan_upserts, parse_upsert_response

plan = plan_upserts(
    [CrmRow("row-7", "ana@northwind.example", {"lifecyclestage": "lead"})], allow_fixtures=True
)
batch = plan.batches[0]
body = batch.body()  # send with your HTTP client / n8n HTTP Request node
outcomes = parse_upsert_response(batch, 207, {"status": "COMPLETE", "results": [], "errors": []})
```

Attio (not run by the lab against a real workspace):

```python
import os
from crmsync import CrmRow, RateBudget, UrllibTransport, execute_asserts, plan_asserts

plan = plan_asserts(
    [CrmRow("row-7", "ana@northwind.io", {"job_title": "COO", "tags": ["clay"]})],
    multiselect=("email_addresses", "phone_numbers", "tags"),
    overwrite=("tags",),  # only if this sync owns the whole tag list
)
report = execute_asserts(
    plan, UrllibTransport(), token=os.environ["ATTIO_TOKEN"], budget=RateBudget(25, 1.0)
)
```

HubSpot live send (not run by the lab against a real portal):

```python
import os
from crmsync import RateBudget, UrllibTransport, execute_upserts

report = execute_upserts(
    plan, UrllibTransport(timeout=30), token=os.environ["HUBSPOT_TOKEN"], budget=RateBudget()
)
print(report.summary())  # created / updated / failed / skipped, calls, still_retryable
```

## Limits (honest)
- `UrllibTransport` is tested only against a local `http.server` on 127.0.0.1 (207 split,
  429 `Retry-After`, 302 not followed, read timeout, refused connection). Whether HubSpot
  sends `Retry-After` on 429 is unconfirmed: developers.hubspot.com did not resolve from the
  build environment (2026-11-07) and only third-party pages mention it, so the executor falls
  back to 1 s / 2 s / 4 s. HubSpot's Python SDK README suggests a urllib3 `Retry` for 429 /
  5xx, but urllib3's default `allowed_methods` excludes POST, so that setup never retries a
  batch upsert — this executor does, per row.
- API shape read from HubSpot's official Node client `@hubspot/api-client` 14.1.2 (models
  `SimplePublicObjectBatchInputUpsert`, `SimplePublicUpsertObject`,
  `BatchResponseSimplePublicUpsertObjectWithErrors`, `StandardError`); developers.hubspot.com
  was unreachable from the build environment, so the 100-input limit and the note that
  partial upserts are not supported with `idProperty=email` come from a search summary of
  HubSpot's contacts guide. Never run against a live HubSpot portal.
- The keys inside an error's `context` are not specified in the SDK; rows are matched by any
  context value equal to the key or trace id. Unmatched errors are attached to the rows
  missing from `results`.
- `RateBudget` defaults to 100 calls / 10 s, the lab's conservative assumption; HubSpot's
  per-tier limits were not opened (search summary only). The budget is per process object:
  writers in separate processes or n8n workers need a shared store (not built).
- `Retry-After` is read as delta-seconds or an HTTP-date; anything else falls back to backoff.
- Attio shapes come from the generated types and endpoint docs comments in the third-party
  `attio-ts-sdk` 2.3.3 (npm, generated from Attio's OpenAPI); docs.attio.com and
  api.attio.com were refused by the build environment's proxy (2026-11-08). The 25 writes / s
  workspace budget is a search summary of Attio's rate-limit guide, not an opened page.
  Which multiselect attributes an object has is the caller's declaration (`multiselect`);
  the lab does not read the object schema. Whether two concurrent asserts on the same new
  key can both create is not documented in what was read: keep one writer per workspace.
- Not built yet: Pipedrive cached
  search + single writer, merge remapping, lifecycle / ownership rules (see ROADMAP 08).
