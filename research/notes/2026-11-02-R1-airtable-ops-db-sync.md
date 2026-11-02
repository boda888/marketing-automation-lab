# R1 · Airtable as the ops database behind Clay / n8n (2026-11-02)

## Question
If Airtable is the system of record for accounts and leads between Clay, n8n and the
sequencer: what are its API limits, how does its upsert behave on duplicate keys, and what do
the stock n8n Airtable nodes and the official JS client actually do with batching, retries
and change polling — i.e. where does a 2,000-lead sync break?

## Findings
Method: airtable.com and support.airtable.com were unreachable from this run (proxy 403 /
ENOTFOUND), so limits come from a web-search summary (labelled below) and behaviour comes
from **source code**: n8n's Airtable v2 node and trigger (n8n `master`), the official
`airtable` JS client (`Airtable/airtable.js` `master`, npm latest 0.12.2 published
2023-08-16) and the MCP Registry. Nothing was run against a live base.

**Limits (search summary, not opened):** 5 requests / s per base; 50 requests / s for all
traffic of one personal access token user / service account; after a 429, wait 30 s before
requests succeed again; up to 10 records per create / update request (so ≈ 50 records / s
per base at best). Monthly API call caps by plan were reported (Free 1,000, Team 100,000 then
throttled to 2 req / s, Business / Enterprise no monthly cap) but sources disagree on details.
Records-per-base limits per plan were not found.

**Upsert (`PATCH` with `performUpsert.fieldsToMergeOn`)** — search summary of Airtable
community threads and a third-party API reference: the merge fields act as a composite key;
0 matches → create, 1 match → update, **more than one match → the whole request fails** (all
10 records in that call); merge fields cannot be computed fields (formula, lookup, rollup);
the response lists `createdRecords` / `updatedRecords` ids next to `records`. The n8n source
below confirms the multiple-match error exists: it catches the message
`Cannot update more than one record`.

**n8n Airtable v2 node, `record → upsert`** (`upsert.operation.ts`, `transport/index.ts`):
- Sends **one `PATCH` per input item** (`records` has one entry per loop iteration), so the
  10-records-per-request batch is never used for upsert or create: 2,000 leads = 2,000
  requests, ≥ 400 s at 5 req / s per base, and any other flow writing the same base shares
  that budget.
- Returns only `responseData.records`; `createdRecords` / `updatedRecords` are dropped, so a
  flow cannot count created vs updated rows from the node output.
- On the duplicate-key error it **falls back silently**: builds
  `AND({col} = '<value>')` by string interpolation (a value containing `'`, e.g. a company
  called `O'Hara Labs`, breaks the formula), fetches the matches and updates **only the first
  one** unless "Update All Matches" is on — the duplicate rows stay and nothing is logged.
- Matching on `id`: a `422` makes it **create** the records instead (a deleted / wrong record
  id becomes a new row).
- `update` with more than 10 records chunks into PATCHes of 10 (`batchUpdate`); `create`
  is one `POST` per item.

**n8n Airtable Trigger** (`AirtableTrigger.node.ts`) polls with
`IS_AFTER({<trigger field>}, DATETIME_PARSE("<lastTimeChecked>", …))`, where the trigger
field must be a Created / Last Modified time field; `lastTimeChecked` is set to the poll
start time, and on first activation it starts at "now" — records changed before activation
are never emitted. Every poll is one or more list calls against the same 5 req / s budget.

**Official JS client `airtable` 0.12.2**: retries a 429 **without an attempt limit** using
full-jitter exponential backoff, initial 5 s, capped at 600 s (`internal_config.json`); can
be switched off with `noRetryIfRateLimited`. Only 429 is retried (a 429'd write was not
applied, so this is safe); timeouts abort and reject. The package has had no release since
2023-08.

**MCP:** official remote server `com.airtable/mcp` 0.1.0 (`https://mcp.airtable.com/mcp`) is
in the registry next to many third-party Airtable servers (Waystation, Pipeworx, MCP Armory,
`domdomegg/airtable-mcp-server` 1.14.0 …). The `domdomegg` server exposes
`create_record` / `update_records` / `delete_records`, and its README warns that its HTTP
mode has **no authentication** and is reachable from a browser via DNS rebinding.

## Apply
- Make the merge key a plain text field you control (`person_key` = lower-cased email, or
  `domain` for accounts) and **dedupe it before every upsert**: one duplicate key fails the
  whole 10-record call via the API, and in n8n it silently updates only the first duplicate.
  Add a daily "duplicate keys" view (group by key, count > 1) and fix those rows first.
- For volume, skip n8n's Airtable upsert node: use an HTTP Request node that sends
  `PATCH /v0/{base}/{table}` with `performUpsert` and 10 records per call (n8n Loop Over Items,
  batch 10, Wait ≈ 250 ms ⇒ ≤ 4 req / s), and log `createdRecords` / `updatedRecords` from
  the response — 2,000 leads become 200 calls instead of 2,000.
- On 429 wait **≥ 30 s** before the next call (n8n's 5 s max Retry-On-Fail wait is too short),
  and keep Clay → Airtable, n8n → Airtable and Airtable automations from hitting one base at
  the same time; split hot tables (raw signals, event logs) into a separate base.
- When polling Airtable from n8n, trigger on a dedicated "Last Modified (status fields)" field,
  not "any field", so the flow's own writes back to the row don't re-trigger it; backfill rows
  that existed before activation manually.
- For Claude, connect the official `mcp.airtable.com/mcp` server, never run a community
  server's HTTP mode without an auth proxy, and use a read-only token for research agents.

## Lab impact
A reusable "sheet / ops-DB writer" belongs in 09 (reliability kit): batch upsert of ≤ 10 rows
per call, pre-flight duplicate-key check (refuse the batch and list the clashing rows rather
than letting one duplicate fail or silently mis-update), per-row created / updated counts,
429 → wait ≥ 30 s, and a requests-per-second budget per base shared across writers. Added to
09 in the root ROADMAP. No code change in this run.

## Sources
- https://raw.githubusercontent.com/n8n-io/n8n/master/packages/nodes-base/nodes/Airtable/v2/actions/record/upsert.operation.ts — opened 2026-11-02
- https://raw.githubusercontent.com/n8n-io/n8n/master/packages/nodes-base/nodes/Airtable/v2/actions/record/create.operation.ts — opened 2026-11-02
- https://raw.githubusercontent.com/n8n-io/n8n/master/packages/nodes-base/nodes/Airtable/v2/actions/record/update.operation.ts — opened 2026-11-02
- https://raw.githubusercontent.com/n8n-io/n8n/master/packages/nodes-base/nodes/Airtable/v2/transport/index.ts — opened 2026-11-02
- https://raw.githubusercontent.com/n8n-io/n8n/master/packages/nodes-base/nodes/Airtable/v2/helpers/utils.ts — opened 2026-11-02
- https://raw.githubusercontent.com/n8n-io/n8n/master/packages/nodes-base/nodes/Airtable/AirtableTrigger.node.ts — opened 2026-11-02
- https://raw.githubusercontent.com/Airtable/airtable.js/master/src/base.ts — opened 2026-11-02
- https://raw.githubusercontent.com/Airtable/airtable.js/master/src/exponential_backoff_with_jitter.ts — opened 2026-11-02
- https://raw.githubusercontent.com/Airtable/airtable.js/master/src/internal_config.json — opened 2026-11-02
- https://registry.npmjs.org/airtable — opened 2026-11-02 (latest 0.12.2, 2023-08-16)
- https://registry.modelcontextprotocol.io/v0/servers?search=airtable — opened 2026-11-02
- https://raw.githubusercontent.com/domdomegg/airtable-mcp-server/master/README.md — opened 2026-11-02
- https://airtable.com/developers/web/api/rate-limits — search summary, not opened (ENOTFOUND / proxy 403)
- https://support.airtable.com/docs/en/managing-api-call-limits-in-airtable — search summary, not opened
- https://community.airtable.com/development-apis-11/new-beta-rest-api-upserts-5313 — search summary, not opened
