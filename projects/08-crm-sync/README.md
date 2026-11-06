# 08 · CRM sync (HubSpot first; Attio and Pipedrive next)

**The pain:** the CRM no longer matches reality. An n8n HTTP node goes green on HubSpot's
batch endpoint while some rows never landed: a batch returns **HTTP 207** and HubSpot's own
SDK resolves it as success, so failed rows sit unread in `errors[]`. Retrying a batch
*create* after a timeout makes duplicates; a blank sheet cell overwrites a good CRM value;
updates matched by company domain overwrite several people of one company.

v0.0.2: HubSpot contacts **batch upsert** planning, response parsing and an executor that
re-sends only retryable rows under one rate budget per portal, with one
`created / updated / upserted / failed / skipped` outcome per input row. The executor takes
an injected transport (offline fake in tests); no live HTTP transport, service or n8n
workflow yet.

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

## Limits (honest)
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
- `Retry-After` is read as delta-seconds only; an HTTP-date value falls back to backoff.
- Not built yet: live HTTP transport, Attio assert, Pipedrive cached
  search + single writer, merge remapping, lifecycle / ownership rules (see ROADMAP 08).
