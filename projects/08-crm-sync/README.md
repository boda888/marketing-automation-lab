# 08 · CRM sync (HubSpot, Attio, Pipedrive)

**The pain:** the CRM no longer matches reality. An n8n HTTP node goes green on HubSpot's
batch endpoint while some rows never landed: a batch returns **HTTP 207** and HubSpot's own
SDK resolves it as success, so failed rows sit unread in `errors[]`. Retrying a batch
*create* after a timeout makes duplicates; a blank sheet cell overwrites a good CRM value;
updates matched by company domain overwrite several people of one company.

Attio adds its own trap: its assert endpoint **replaces every multiselect attribute** other
than the matching one with the list you send, so a sync that writes one `tags` value wipes
the record's other tags. Pipedrive has no upsert at all: search-then-create from two n8n
executions (or a retried create after a timeout) makes the same person twice.

v0.0.9: HubSpot contacts **batch upsert** planning, response parsing, an executor that
re-sends only retryable rows under one rate budget per portal, and a stdlib HTTP transport
(`UrllibTransport`) that never follows redirects; **Attio assert** (one idempotent `PUT` per
person) with a multiselect-overwrite guard; **Pipedrive** cached search → update / create
under one writer lock, where a create is never re-sent unless it surely never left; and
**merge webhooks** (HubSpot `*.merge`, Attio `record.merged`, Pipedrive v2 `merged_from_id`)
that re-point every stored key of a merged-away id in one shared `IdMap`, behind
**webhook origin checks** (HubSpot v3 HMAC, Attio HMAC, Pipedrive Basic) that fail closed;
one `write(rows, crm=...)` entry point stores every landed id where those merge events look. One `created / updated / upserted / failed /
skipped` outcome per input row. Tested against a local HTTP server and offline fakes; never
against a live HubSpot portal, Attio workspace or Pipedrive company. A FastAPI service (`POST /v1/write`, `POST /v1/webhooks/{crm}`)
wraps both sides; no n8n workflow or offline demo yet.

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

```
plan_persons(rows)                       e-mail is the key; name required only to create
   │   skipped: missing_key · fixture · duplicate_key · invalid_property_name
   │            (standard fields top level; custom fields only by their 40-hex key)
   ▼
execute_persons(plan, transport, token=…, budget=…, cache=MemoryPersonCache(), lock=Lock())
   cache hit  → PATCH /persons/{id}        404 → evict, fall through to search (merged / deleted)
   cache miss → under the writer lock:
       GET /persons/search?term=<email>&fields=email&exact_match=true
         hits re-checked: an item counts only if its emails[] holds the key
         0 → POST /persons (emails [{value, primary, label}])   1 → PATCH   ≥ 2 → ambiguous_match
   GET / PATCH re-sent on 408 / 429 / 5xx / timeout / reset
   POST re-sent only on 429 or not_sent; timeout / reset / 5xx / 2xx without id
     → failed outcome_unknown, not retryable, not cached → next run searches first
   e-mails never sent on update; no fields to change → updated "no_fields", no call
   report.summary(): + search_calls, cache_hits (search costs more of Pipedrive's token budget)
```

Why upsert and not create: a re-sent upsert on a unique property updates the same contact;
a re-sent create after a 5xx or timeout can create it twice. Batch create is deliberately
not offered.

```
HubSpot [ {subscriptionType: "contact.merge", primaryObjectId, mergedObjectIds, newObjectId?} ]
Attio   {events: [{event_type: "record.merged", id.record_id, duplicate_record_id, …}]}
Pipedrive v2 {meta: {entity, entity_id, merged_from_id?}, data}
   │  parse_hubspot_merges / parse_attio_merges / parse_pipedrive_merge
   │    → MergeEvent(crm="hubspot:contact" | "attio:<object_id>" | "pipedrive:person",
   │                 losing_ids, winner_id)            + ignored reasons, never guessed
   │    HubSpot: newObjectId wins over primaryObjectId; then the primary is a loser too
   │    Attio: one event per original; duplicate from another object → cross_object_merge
   │    Pipedrive: no survivor id → winner None
   ▼
apply_merges(idmap, events)        every key stored for a losing id → winner (replay = no-op)
   │                               winner None → key evicted: the next write searches again
   ▼
MemoryIdMap  (crm, key) → id with a reverse index; IdMapPersonCache(idmap) plugs it into
             execute_persons, so a merged Pipedrive person is PATCHed by its new id, no search
```

A merge webhook re-points stored ids, so a forged one would redirect later writes. Check
origin on the **raw body bytes** before parsing (research R7 note 2026-11-10):

```
verify_webhook(crm, headers, raw_body, method, url, secrets=WebhookSecrets(...), now_ms)
   hubspot    X-HubSpot-Signature-v3 == base64 HMAC-SHA256(app client secret,
              METHOD + exact public URL + raw body + X-HubSpot-Request-Timestamp)
              timestamp older than 5 min → stale_timestamp; more than 5 min ahead →
              future_timestamp (the official SDK lets future ones pass); constant-time compare
   attio      Attio-Signature / X-Attio-Signature == hex HMAC-SHA256(webhook secret, raw body)
   pipedrive  Authorization: Basic user:password set on the webhook (no signature exists)
   → AuthResult(ok, reason).status: 200 · 401 refused · 500 not_configured (no secret set:
     refused, never waved through) · unknown CRM refused
```

One entry point for all three writers, sharing one `IdMap`:

```
write(rows, crm="hubspot" | "attio" | "pipedrive", transport, token, budget, idmap=…,
      options=WriteOptions(id_property / object / attio_object_id / multiselect / overwrite))
   │   plan + execute with that CRM's writer → one RowOutcome per input row, input order
   │   landed (created / updated / upserted with an id) → idmap.put(namespace, key, id)
   │   failed / skipped rows never touch the map; Pipedrive also reads it before searching
   ▼
namespaces = what the merge parsers emit:
   hubspot:contact · pipedrive:person · attio:<object UUID>
   Attio webhooks name the object by UUID, the assert path usually by slug ("people"):
   with an IdMap, an Attio write without attio_object_id is refused (ValueError) before
   any call, instead of storing ids where no merge event will ever find them
```

The service puts both halves behind HTTP (`crmsync.api`):

```
POST /v1/write            Bearer token (optional) → write(rows, crm) → one outcome per row
   {crm, rows[≤1000]}      CRM without a token → 404 · duplicate row_id / Attio slug with
                           no object UUID → 422, nothing sent · IdMap shared by all writes
POST /v1/webhooks/{crm}   no bearer (CRMs cannot send one): their signature is the auth
   raw body bytes ─► verify_webhook(url = configured public_base_url + path + query)
                     refused → 401 · no secret configured → 500 (CRM retries later)
                  ─► json → 400 invalid_json
                  ─► hubspot: drop items whose eventId was already applied (attemptNumber
                     changes per retry, so it is never part of the key); eventIds marked
                     seen only after apply_merges ran
                  ─► parse_*_merges → apply_merges → 200 {merge, duplicates, ignored}
```

Webhook work is in-memory only, so the answer goes back well inside HubSpot's ~5 s
(search summary). `public_base_url` must be `https://`: HubSpot v3 signs the URL it called,
not the host the app sees behind a proxy.

`hubspot_legacy_signature` (v1 / v2) reproduces the vendor SDK's own test vectors, which is
how the concatenation order was checked.

## Run it

```bash
python -m pip install -e ".[dev]"
python -m pytest -q
```

Service (not run by the lab against a real CRM):

```python
import os
from crmsync import UrllibTransport, WebhookSecrets, WriteOptions
from crmsync.api import CrmService, create_app

app = create_app(
    CrmService(
        transport=UrllibTransport(),
        tokens={"hubspot": os.environ["HUBSPOT_TOKEN"]},
        secrets=WebhookSecrets(hubspot_client_secret=os.environ["HUBSPOT_CLIENT_SECRET"]),
        public_base_url="https://hooks.example",  # the URL HubSpot calls
        options={"attio": WriteOptions(attio_object_id="<people object UUID>")},
    ),
    api_token=os.environ["CRMSYNC_API_TOKEN"],
)
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

Pipedrive (not run by the lab against a real company account):

```python
import os
from crmsync import CrmRow, MemoryPersonCache, RateBudget, UrllibTransport
from crmsync import execute_persons, plan_persons

cache = MemoryPersonCache()  # replace with a sheet / Postgres table to keep ids between runs
report = execute_persons(
    plan_persons([CrmRow("row-7", "ana@northwind.io", {"name": "Ana Ruiz", "org_id": 12})]),
    UrllibTransport(),
    token=os.environ["PIPEDRIVE_TOKEN"],
    budget=RateBudget(10, 2.0),
    cache=cache,
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
- The service is tested only with FastAPI's `TestClient` and offline fakes. `/v1/write`
  holds the HTTP request open for the whole CRM write (no job queue); the default rate
  budgets (HubSpot 100 / 10 s, Attio 25 / 1 s, Pipedrive 10 / 2 s) are lab assumptions.
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
- Pipedrive shapes come from the official `pipedrive` client 36.3.1 (npm, v2 models and
  `persons-api`); pipedrive.readme.io and developers.pipedrive.com were not opened. The
  writer lock is per process; several n8n workers need one queue. How soon a just-created
  person shows up in search (index lag) is not documented in what was read — the cache is
  what protects a re-run in the same process. `RateBudget(10, 2.0)` in the example is an
  assumption, not Pipedrive's token budget (third-party summaries only, R7).
- Merge payloads were not read from vendor docs: docs.attio.com, attio.com,
  developers.hubspot.com, community.hubspot.com and pipedrive.readme.io did not resolve from the
  build environment (2026-11-09). `contact.merge` and `record.merged` are confirmed only as
  event names in `@hubspot/api-client` 14.1.2 and `attio-ts-sdk` 2.3.3; field names come from
  search summaries of the vendor pages. When HubSpot sets `newObjectId`, and whether Pipedrive
  puts `merged_from_id` in `meta` or `data` (both are read), is unconfirmed. Webhook signature
  checks run in `POST /v1/webhooks/{crm}`; `MemoryIdMap` and the HubSpot `eventId` memory
  (`SeenEvents`, 50,000 ids) are per process, so a restart forgets both (no persistent store
  yet). Never fed a live webhook.
- Webhook auth: the HubSpot v3 rule (concatenation, base64, 5 min) is from the official
  `@hubspot/api-client` 14.1.3 `Signature` util and its test vectors; the header names, and
  everything about Attio's signature (header, hex, no timestamp), are search summaries only —
  third-party sources disagree on whether Attio signs a timestamp. Whether HubSpot re-signs a
  redelivery with a fresh timestamp is unconfirmed (an old one would be refused as stale).
  No live delivery was ever verified.
- `write()` stores ids for HubSpot and Attio but those writers do not need them (they upsert
  on a unique property); only Pipedrive reads the map. Not built yet: lifecycle / ownership
  rules (see ROADMAP 08).
