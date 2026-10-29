# R7 · HubSpot vs Attio vs Pipedrive vs Twenty: upsert, dedupe, merges and rate limits over the API (2026-10-29)

## Question
When n8n (or a service) writes enriched leads and outcomes into the CRM, which of HubSpot,
Attio, Pipedrive and Twenty can **upsert** a person on a key in one call, what happens to
duplicates and merged records, what does a partial failure look like, and which rate
limits shape a 2,000-row sync?

## Findings
Method: developers.hubspot.com, docs.attio.com, attio.mintlify.app, pipedrive.readme.io,
devcommunity.pipedrive.com and docs.twenty.com did not resolve from this run (`ENOTFOUND`),
and the API hosts did not answer either. So I read, without running them, the packages on
registry.npmjs.org: **HubSpot's** official Node client (`@hubspot/api-client` 14.1.2,
published 2026-10-28, generated from HubSpot's OpenAPI), **Pipedrive's** official client
(`pipedrive` 36.1.0, 2026-10-21, v1 + v2 APIs), **Twenty's** own `twenty-client-sdk` and
`twenty-sdk` 2.51.3 (2026-10-25, from the twentyhq/twenty monorepo) and, for Attio, the
third-party `attio-js` 0.1.1 (Speakeasy-generated, April 2025), which ships a copy of
Attio's OpenAPI spec (`openapi.json`, "Attio API" 2.0.0). Attio has no official REST
client on npm. Its `attio` package is the app-building CLI. The Attio facts below are
therefore as of that spec copy, which may be stale. Rate-limit numbers come from web
searches only. I did not open the pages behind them.

**Write semantics:**

| | HubSpot | Attio | Pipedrive | Twenty |
|---|---|---|---|---|
| Upsert on a key | `POST /crm/v3/objects/contacts/batch/upsert`: inputs `{id, idProperty, properties, objectWriteTraceId}` (`idProperty` e.g. `email` or a unique custom property). Each result carries **`_new: true/false`**, so you know created vs updated | `PUT /v2/objects/{object}/records?matching_attribute=<slug or id>` ("Assert a record"). The attribute **must be unique**. Found → update, not found → create | **none**: `GET /api/v2/persons/search?term=…&fields=email&exact_match=true` (case-insensitive, wraps `/v1/itemSearch`) then `POST /api/v2/persons`. Two concurrent runs can both miss and both create | upsert exists: record events include `UPSERTED` (`ObjectRecordUpsertEvent` with optional `before`), and workflows have an `UPSERT_RECORD` step. The REST query parameter for it was not in the packages |
| Plain create on a duplicate | — | `POST …/records` "will throw on conflicts of unique attributes" (spec text) | creates a second person | object-level `duplicateCriteria` (lists of field names, e.g. email or name + company) drive duplicate detection in the app |
| Partial failure | batch returns **HTTP 207** with `status`, `results[]`, `numErrors`, `errors[]` (`category`, `message`, `context`). The SDK **resolves** 207 as success, so failed rows are only in `errors[]` | one record per call: 400 `value_not_found` / `missing_value` (bad matching attribute), 404 unknown object | one record per call | — |
| Merge | `POST /crm/v3/objects/contacts/merge` `{primaryObjectId, objectIdToMerge}` | webhook event **`record.merged`** (the losing record id stops existing) | `PUT /v1/persons/{id}/merge` | `MERGE_MULTIPLE_RECORDS` in the app |
| Webhooks | (not read this run) | `record.created / updated / deleted / merged`, `list-entry.*`, `note.*`, `task.*`, `comment.*` | v2 payloads are the default since 2025-03-17. Auth is **HTTP Basic** (`http_auth_user` / `_password`), no body signature | database events incl. `UPSERTED` |

**Client behaviour that changes the sync design:**
- **HubSpot SDK:** rate limiting (Bottleneck) and retries are both **off unless
  configured**. The README's limiter preset is 9 requests per second with 6 concurrent, and
  3 concurrent / 550 ms for search. Retries (`numberOfApiCallRetries` 0–6) apply to every
  request: on 5xx after 200 ms × attempt, on 429 `TEN_SECONDLY_ROLLING` after 10 s ×
  attempt, and on the search "secondly limit" after 1 s × attempt. A retried **batch
  create** after a 5xx can therefore create twice. A retried **batch upsert** on email
  cannot. The README says OAuth apps get 100 requests per 10 s.
- **Pipedrive SDK:** axios with **no retry**. Errors are rejected as the response body, so
  `Retry-After` and rate-limit headers are lost unless you call the `…WithHttpInfo`
  variants (`fullResponse`). Custom fields are hash keys, and a single read returns at most
  15 of them via `custom_fields`.
- **Attio:** assert on `email_addresses` or `domains` is the idempotent write. Plain
  create on the same data fails on the unique attribute instead of duplicating.

**Rate limits (web-search summaries only, 2026-10-29):**
- HubSpot: 190 requests / 10 s per private app on Professional and Enterprise. The daily
  pool is shared by all apps in the account, about 625k (Pro) to 1M (Enterprise), but
  sources disagree. Everything after the limit gets 429. Daily usage can be read from
  `/account-info/v3/api-usage/daily/private-apps`.
- Attio: 100 reads / s and 25 writes / s for the whole workspace. A 429 means the request
  was not processed and is safe to retry, typically in the next second.
- Pipedrive: token budget per company per day. Third-party figures are about 30,000 base
  tokens × plan multiplier (1 Lite … 7 Ultimate) × seats. Endpoints cost by type (e.g.
  search ≈ 40, POST/PUT ≈ 10, GET one ≈ 2 per a third-party list). There is a 2-second
  burst window, lower for API-token than for OAuth traffic. So **search-then-create costs
  about 5× a write**, which makes a local email → person-id cache worthwhile.

## Apply
- **HubSpot from n8n:** use batch **upsert** with `idProperty: email` (or your own unique
  `lab_person_key` property), never batch create (check the batch size limit in the docs;
  it was not in the SDK). Then check the body,
  not the status: on 207 send `errors[]` rows to a review / DLQ sheet, and log `_new` per
  row to count created vs updated. A green HTTP node is not proof every row landed.
- **Turn retries on only for idempotent calls.** HubSpot SDK `numberOfApiCallRetries`
  (or n8n Retry On Fail) is fine for upsert / update / read but not for create. Set the
  limiter to stay under 19 requests per second per app, and remember that every app in
  the portal shares one daily pool.
- **Attio:** write people with assert on `email_addresses` and companies on `domains`.
  Subscribe to `record.merged` and re-point any stored Attio record id (sheet, Clay table,
  sequencer custom field) to the surviving record, or later updates go to a deleted id.
- **Pipedrive:** keep an email → person_id map (sheet / Postgres) and search only on a
  cache miss with `exact_match=true&fields=email`. Run one writer at a time (n8n batch
  size 1 or a queue) so two runs cannot both create. Put Basic auth on the webhook URL and
  re-read the person by id before acting, since the payload is not signed.
- **Twenty:** set `duplicateCriteria` on People (email) and Companies (domain) before the
  first import. The open-source CRM only dedupes what you configure.

## Lab impact
Project 08 (CRM sync): the adapter interface should be `upsert(person_key, fields)` →
`created | updated | failed(reason)`. HubSpot maps to batch upsert, where a 207 is split
per row and never reported as success. Attio maps to assert. Pipedrive maps to
cached-search + create behind a single-writer lock, with a dry-run mode. Retries are
allowed only on idempotent operations. Merge events (HubSpot merge, Attio
`record.merged`) remap stored CRM ids. Rate limiting is per account / app (HubSpot daily
pool, Pipedrive token budget), not per process. Root ROADMAP 08 item edited.

## Sources
- https://registry.npmjs.org/@hubspot/api-client/-/api-client-14.1.2.tgz — opened 2026-10-29 (README "Rate limiting" / "Retry mechanism", `lib/src/configuration/constants.js`, `lib/src/client.js`, `services/decorators/RetryDecorator.js`, `codegen/crm/contacts/apis/BatchApi.js`, models `SimplePublicObjectBatchInputUpsert`, `SimplePublicUpsertObject`, `BatchResponseSimplePublicUpsertObjectWithErrors`, `StandardError`, `PublicMergeInput`)
- https://registry.npmjs.org/pipedrive/-/pipedrive-36.1.0.tgz — opened 2026-10-29 (README, `versions/v2/base.js`, `v2/api/persons-api.d.ts`, `v1/api/persons-api.d.ts`, `v1/models/add-webhook-request.d.ts`, package.json)
- https://registry.npmjs.org/attio-js/-/attio-js-0.1.1.tgz — opened 2026-10-29 (third-party; `openapi.json` paths `/v2/objects/{object}/records` PUT/POST and `/v2/webhooks`, `docs/sdks/records/README.md`)
- https://registry.npmjs.org/twenty-client-sdk/-/twenty-client-sdk-2.51.3.tgz and https://registry.npmjs.org/twenty-sdk/-/twenty-sdk-2.51.3.tgz — opened 2026-10-29 (`metadata/generated/schema.d.ts` `duplicateCriteria`, `DatabaseEventAction`; `define/index.d.ts` `ObjectRecordUpsertEvent`, `UPSERT_RECORD`)
- https://registry.npmjs.org/attio-mcp/-/attio-mcp-1.7.0.tgz — opened 2026-10-29 (third-party MCP server; no rate-limit or assert details found)
- https://developers.hubspot.com/docs/developer-tooling/platform/usage-guidelines, https://docs.attio.com/rest-api/guides/rate-limiting, https://pipedrive.readme.io/docs/core-api-concepts-rate-limiting, https://docs.twenty.com/developers/api-and-webhooks/api — tried 2026-10-29, DNS ENOTFOUND, not opened
- https://developers.hubspot.com/docs/developer-tooling/platform/usage-guidelines, https://developers.hubspot.com/changelog/increasing-our-api-limits, https://developers.HubSpot.com/docs/API/private-apps — search summary, not opened (HubSpot limits)
- https://attio.mintlify.app/rest-api/guides/rate-limiting, https://hexdocs.pm/attio/Attio.Records.md — search summary, not opened (Attio limits, assert)
- https://truto.one/blog/how-to-integrate-with-the-pipedrive-api-2026-engineering-guide/, https://pipedrive.readme.io/docs/guide-for-optimizing-api-usage, https://devcommunity.pipedrive.com/t/introducing-token-based-rate-limits-for-api-usage-what-you-need-to-know/18647 — search summary, not opened (Pipedrive token budget)
