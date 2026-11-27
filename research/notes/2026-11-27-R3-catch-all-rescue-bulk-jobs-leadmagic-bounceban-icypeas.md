# R3 · Catch-all rescue verifiers and async bulk jobs: LeadMagic, BounceBan, Icypeas (2026-11-27)

## Question
Two open R3 questions: (1) how "catch-all rescue" verifiers (BounceBan, LeadMagic, others)
return a verdict on an accept-all domain, what they charge for it, and how a failed or
timed-out call shows up; (2) Icypeas API statuses and billing. A third point came up while
reading: what the vendors' own n8n bulk-job templates do on re-runs and large lists.

## Findings
Method: vendor docs hosts were unreachable again from the run environment (api-doc.icypeas.com,
icypeas.com, docs.leadmagic.io, leadmagic.io incl. `/openapi.json`, bounceban.com,
support.bounceban.com, developer.millionverifier.com: no connection). Packages were downloaded
from registry.npmjs.org and read, not executed; LeadMagic's changelog, two templates and its
OpenAPI contract file were read from the vendor's GitHub repo. Nothing was called against a
live API; no independent accuracy test of any catch-all verifier was found or run.

**LeadMagic (`n8n-nodes-leadmagic` 1.8.0, 2026-11-17, vendor; repo `LeadMagic/leadmagic-n8n`):**
- Validation `POST /v1/people/email-validation` returns `email_status` `valid` / `invalid` /
  `unknown` plus `mx_provider`. README: "Email Validation [charges] for a `valid` or `invalid`
  result, and `unknown` (including catch-all domains) is free"; Email Finder charges only for a
  valid email and its results "are already validated, so don't validate them again" (vendor
  claim). So LeadMagic does **not** rescue catch-alls in validation: catch-all comes back as
  `unknown`, at no cost.
- Rate limits for Finder and Validation: 300 / min (Basic, Essential), 600 (Growth), 1,000
  (Professional), 1,500 (Ultimate), **shared across all keys**; a 429 costs nothing. 401
  repeated → key locked out 15 min "then longer"; 402 = out of credits **or account paused**.
  Requests time out at 30 s (60 s searches) and are never retried by the node.
- New in 1.8.0: **Bulk Job** — `POST /bulk/submit` with `rows` or an HTTPS `fileUrl`, up to
  500,000 rows, optional `callback: {url, events: ["completed","failed"]}` and an
  `idempotencyKey`; `GET /bulk/jobs/{id}` (status) and `/results` (`limit`, `offset`, row
  `status`). README: "failed rows are free". The OpenAPI contract (spec 1.4.35) also lists
  `/bulk/validate` (dry check of a submit), `/pause`, `/resume`, `/restart` (`startRowIndex`),
  `/errors`, `/events` and `GET /v1/analytics/credits|usage` (free).
- Two traps in the vendor's own n8n assets:
  1. The node's **Return All** on bulk results stops after `MAX_PAGES` = 200 pages × 100 rows =
     **20,000 rows**, with no error (`bulk.js` loop bound), while a job may hold 500,000.
  2. The "bulk job for large lists" template sets `idempotencyKey = {{ $execution.id }}` — a
     new execution gets a new key, so re-running the workflow after a crash submits (and pays
     for) the list again. Its poll loop (Wait 2 min → status → fallback back to Wait) has no
     attempt cap; only `completed` and `failed` / `canceled` leave it.
- The HubSpot hygiene template writes `email_status ?? 'error'` into a contact property, so a
  missing status is stored as the string `error` next to real verdicts.

**BounceBan (`n8n-nodes-bounceban` 1.1.1, 2025-12-01, published by "BounceBan APIs Support",
repo `bounceban-com`; plus third-party `@pipeworx/mcp-bounceban` 0.2.2):**
- `GET https://api-waterfall.bounceban.com/v1/verify/single?email=…` with the key in a bare
  `Authorization` header (no `Bearer`); credential test `GET api.bounceban.com/v1/account`.
  The "waterfall" host blocks up to ~80 s for a final answer (third-party README); BounceBan
  documents **HTTP 408 for a verification that timed out server-side** (third-party, citing
  BounceBan's waterfall guide).
- `mode=regular` (default) or `deepverify`: DeepVerify **assumes the email's domain is the
  person's current company website** and "can improve the success rate of verifying accept-all
  emails" — the node text warns to take that domain from a reliable source (profile / data
  vendor). That is the method the vendor names: an identity assumption, not a mailbox probe.
- `disable_catchall_verify=1` → plain SMTP only; catch-all / ESG-protected addresses return
  `result: 'unknown', score: -1` at **0 credits**. So with rescue on, the catch-all attempt is
  the paid part; `score -1` = not scored.
- Optional `url` = webhook for the result; failed deliveries are retried "up to two additional
  times within a short interval".
- Engineering smells in the vendor node: it retries **408 up to 15 times back to back** (no
  delay), sets `skipSslCertificateValidation: true`, and in "batch (concurrent)" mode turns
  every error into an output item `bounceban_result: {error}` even without Continue On Fail —
  a downstream filter on `bounceban_result.result` silently drops failed rows.
- Unauthenticated / bad-key probe (third-party): HTTP 401 with two different bodies
  (`{"error":"Authorization header is required"}` vs `{"msg":"Authorization information is
  missing or invalid."}`). Success field names (`result`, `score`, catch-all flag) are not
  confirmed by any package read here beyond the node text above.

**Icypeas (third-party `@slates-integrations/icypeas` 0.3.0, 2026-10-14, and
`@pipeworx/mcp-icypeas` 0.2.2; the vendor's own `n8n-nodes-icypeas` is from 2023):**
- Base `https://app.icypeas.com/api/`, key in a bare `Authorization` header. Bad / missing key:
  HTTP 401 `{"error":"UserNotFoundError","code":"user_not_found_error",…}` (third-party probe).
- **Every single search is asynchronous**: `POST /email-search`, `/email-verification`,
  `/domain-search` enqueue and return an id; read it with `POST /bulk-single-searchs/read
  {id}` or receive `custom.webhookUrl` (with your `custom.externalId`). Status lifecycle:
  `NONE` / `SCHEDULED` / `IN_PROGRESS` → `FOUND` / `DEBITED` (result) or `NOT_FOUND` /
  `DEBITED_NOT_FOUND` (no result). `certainty`: `ultra_sure`, `probable`, `not_found`,
  `undeliverable`; `mxProvider` returned.
- Bulk: `POST /bulk-search` (task `email-search` rows `[first, last, domainOrCompany]`,
  `email-verification`, `domain-search`), **≤ 5,000 rows per bulk**, per-item webhook
  `webhookUrlItem`, done webhook `webhookUrlBulkDone`, `externalIds`; list files
  `/search-files/read`, page results with a `next` cursor. Credits:
  `/a/actions/subscription-information`.
- Webhooks are signed HMAC-SHA1 with the account's API secret plus a timestamp (third-party
  spec text; exact header / payload composition not seen). Verification "can verify Google
  and Microsoft catch-all domains" (third-party spec repeating vendor copy).
- Billing per status is not documented in anything read: the `DEBITED_NOT_FOUND` value
  suggests some not-found searches can still be charged — unconfirmed.

## Apply
- **Catch-all cohort, two cheap steps:** validate with LeadMagic (or MillionVerifier) first —
  catch-all comes back `unknown` and is free there — then send only those rows to a rescue
  verifier. On BounceBan use `deepverify` **only** when the email domain equals the
  company domain from your Clay / data-vendor column; keep `regular` otherwise.
- **Treat a 408 / timeout / `score -1` as "unknown, try later", never "invalid".** In n8n,
  use an HTTP Request node with your own retry (e.g. 3 tries, 60 s apart) instead of the
  BounceBan node's 15 instant retries; in "batch" mode check `bounceban_result.error` before
  any filter, or the failed rows vanish.
- **LeadMagic bulk in n8n:** set the idempotency key from the list, not the execution (e.g.
  `{{ sheetId }}-{{ list version }}`), add a max-attempts counter to the poll loop (or use the
  callback URL), and page results yourself with `limit`/`offset` until you have the job's row
  count — "Return All" stops at 20,000 rows without telling you.
- **Icypeas in Clay / n8n:** the first response is a ticket, not an email. Either pass a
  webhook URL + your row id as `externalId`, or poll `bulk-single-searchs/read` until the
  status leaves `NONE` / `SCHEDULED` / `IN_PROGRESS`; never map the enqueue response to
  "not found". Split lists over 5,000 rows.
- **Measure, don't trust:** log credits before/after the first 50 catch-all rows per vendor
  (LeadMagic `/v1/analytics/credits`, BounceBan `/v1/account`, Icypeas subscription info) and
  their bounce rate after sending — no independent test of rescue accuracy exists.

## Lab impact
- 04 ladder mappers (existing ROADMAP item extended): LeadMagic `email_status`
  (`unknown` = unverified, never `invalid`), BounceBan (`score -1` / 408 = unknown, DeepVerify
  recorded as provenance because it rests on a company-domain assumption), Icypeas status
  lifecycle (non-terminal statuses = pending, never a verdict).
- 04 async bulk adapter pattern (next_steps): idempotency key derived from the input list,
  bounded polling, results paged to the job's total and checked against it.

## Sources
- https://registry.npmjs.org/n8n-nodes-leadmagic/-/n8n-nodes-leadmagic-1.8.0.tgz — opened (README, dist `bulk.js`, `search.js`, descriptions) 2026-11-27
- https://raw.githubusercontent.com/LeadMagic/leadmagic-n8n/main/CHANGELOG.md — opened 2026-11-27
- https://raw.githubusercontent.com/LeadMagic/leadmagic-n8n/main/templates/bulk-job-large-lists.json — opened 2026-11-27
- https://raw.githubusercontent.com/LeadMagic/leadmagic-n8n/main/templates/hubspot-email-hygiene.json — opened 2026-11-27
- https://raw.githubusercontent.com/LeadMagic/leadmagic-n8n/main/tests/openapi-contract.json — opened 2026-11-27 (routes and fields of spec 1.4.35)
- https://registry.npmjs.org/n8n-nodes-bounceban/-/n8n-nodes-bounceban-1.1.1.tgz — opened (README, node + credential source) 2026-11-27
- https://registry.npmjs.org/@pipeworx/mcp-bounceban/-/mcp-bounceban-0.2.2.tgz — opened (README, tools.json) 2026-11-27
- https://registry.npmjs.org/@slates-integrations/icypeas/-/icypeas-0.3.0.tgz — opened (docs/SPEC.md, dist) 2026-11-27
- https://registry.npmjs.org/@pipeworx/mcp-icypeas/-/mcp-icypeas-0.2.2.tgz — opened (README) 2026-11-27
- api-doc.icypeas.com, leadmagic.io/docs, leadmagic.io/openapi.json, bounceban.com, support.bounceban.com/article/what-is-deepverify — not reachable from the run environment 2026-11-27, not used
