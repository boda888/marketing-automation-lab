# R1 · Supabase / Postgres as the ops database behind Clay and n8n (2026-11-13)

## Question
When a team moves the ops table (accounts, leads, push state) from Airtable to Supabase /
Postgres: how do Clay and n8n write to it without creating duplicates, what does the stock n8n
Supabase node actually do with batches, upserts and paging, and what does the plan cost and
limit — i.e. where does a 2,000-lead sync break?

## Findings
Method: supabase.com, community.clay.com and postgresql.org were unreachable from this run
(ENOTFOUND). Behaviour therefore comes from **source and docs repositories on GitHub**: n8n's
Supabase node (`n8n-io/n8n` `master`), the PostgREST docs (`PostgREST/postgrest` `main`, the
REST layer Supabase serves at `/rest/v1`), the Supabase docs source (`supabase/supabase`
`master`, `apps/docs/content/guides`) and the PostgreSQL `INSERT` reference
(`postgres/postgres` `master`). Nothing was run against a live project.

**Upsert over REST (PostgREST docs).** A `POST /rest/v1/<table>` with
`Prefer: resolution=merge-duplicates` is an upsert; `resolution=ignore-duplicates` skips rows
that already exist. By default it matches on the primary key (all PK columns must be in the
body); `?on_conflict=<col>` targets another column that has a **UNIQUE constraint** — e.g.
`?on_conflict=domain` for accounts, `?on_conflict=email` for leads. With a surrogate key, use
`Prefer: resolution=merge-duplicates, missing=default`. `?columns=a,b,c` limits which payload
keys are written and ignores the rest. After creating a table or changing its key, the
PostgREST schema cache must be refreshed or upsert misbehaves. `PUT ?id=eq.4` is a single-row
upsert that needs every column in the body.

**One request = one transaction.** PostgREST: "Any database failure (like a failed
constraint) will result in a rollback of the transaction." A bulk POST of 500 rows with one
bad row (NOT NULL, type error, FK) therefore writes **none** of them.

**Duplicate keys inside one batch fail the whole batch.** PostgreSQL `INSERT … ON CONFLICT DO
UPDATE` "will not be allowed to affect any single existing row more than once; a cardinality
violation error will be raised … Rows proposed for insertion should not duplicate each other
in terms of attributes constrained by an arbiter index". So a Clay export or Apify dataset
with the same domain twice in one chunk makes the merge-duplicates upsert fail and (by the
rule above) roll back the chunk. Dedupe by the conflict key *before* the POST.

**The stock n8n Supabase node (source read):**
- Operations: `create`, `delete`, `get`, `getAll`, `update` — **no upsert**. `create` gives
  duplicate-key errors on re-runs; upsert needs the HTTP Request node (or a Postgres node on
  the direct / pooler connection).
- `create` collects **all input items into one POST** (one request for the whole run, not per
  item). Combined with the transaction rule, one bad lead loses the whole run's insert; with
  continue-on-fail the node returns a single `{error}` item paired to every input item, so
  there is no per-row outcome to route. `dataToSend` (auto-map vs define fields) is read from
  item 0 for all items.
- `update` / `delete` / `get` send one request per item (PATCH / DELETE / GET filtered by the
  item's conditions).
- Every request carries `Prefer: return=representation`, so writes return the full rows
  (counts toward egress; useful for created ids).
- `getAll` pages with `limit` ≤ 1,000 and `offset`, and only adds `order` when "Order By" is
  set — without it, offset paging over a table that is being written to has no stable order
  and can skip or repeat rows. The loop continues only while a page returns **≥ 1,000
  rows**, so if the project's API "max rows" setting is below 1,000 (default 1,000 per
  search summary below), Return All silently stops after the first page.
- OAuth credentials re-fetch the project secret key once on a 401 and retry.

**Clay → Supabase.** No first-party Clay "upsert row" action for Supabase was found (Clay
lists upsert actions for Snowflake, Databricks and ClickHouse — search summary). Community
threads describe Clay's **HTTP API** column posting to `https://<ref>.supabase.co/rest/v1/<table>`
with `apikey` + `Authorization: Bearer` + `Content-Type: application/json`; reported failures
are "Failed to parse body input" (JSON body formatting) and 404 on deleted columns / wrong
table (search summary, threads not opened). That is one request per Clay row, so adding
`?on_conflict=<key>` + `Prefer: resolution=merge-duplicates` is what makes a re-run of the
column idempotent.

**Plans and limits (Supabase docs source):**
- Free: 2 projects; **projects with low activity over 7 days may be paused** (restore by hand
  from the dashboard); 500 MB database per project, 5 GB egress. Pro / Team: no pausing, 8 GB
  disk per project then $0.125 / GB, 250 GB egress then $0.09 / GB; compute billed per project
  (Micro ≈ $10 / month, shared CPU, 1 GB RAM).
- Nano (free) and Micro compute: 60 direct database connections, 200 pooler clients, default
  pool size 15 — relevant when n8n's Postgres node or queue-mode workers open connections.
- No published per-second REST rate limit for the Data API; the docs only say "a number of
  safeguards against bursts" exist.
- Pro is $25 / month per organization with $10 compute credit (search summary, pricing page
  not opened).

## Apply
- Give every ops table a natural UNIQUE key (`domain` for accounts, lowercased `email` or
  LinkedIn URL for leads) and write through an **HTTP Request** node:
  `POST /rest/v1/leads?on_conflict=email` with `Prefer: resolution=merge-duplicates,return=representation`.
  Do the same in Clay's HTTP API column so re-running a column never makes a second row.
- Before that POST, run **Remove Duplicates** on the conflict key and split into chunks
  (e.g. Loop Over Items, 200–500 per call); one duplicate key or one bad row fails the whole
  chunk, so send failed chunks to a retry branch that posts their rows one by one to find the
  offender instead of losing the run.
- Don't use the n8n Supabase node's Create for syncs: it is insert-only and posts every input
  item in one request. Keep it for single-row `get` / `update`.
- On Get Many, always set Order By (e.g. `id.asc`) and check the project's max-rows setting
  is ≥ 1,000, or Return All can stop after page one.
- Free plan is fine for a pilot; an ops DB that a weekly flow touches can be paused after 7
  quiet days — use Pro (or a heartbeat) before a client depends on it.

## Lab impact
The lab's planned 09 ops-table writer (R1 2026-11-02 note) should cover Supabase / PostgREST
next to Airtable: dedupe rows by the conflict key inside each chunk, chunk the upsert, and on a
failed chunk bisect / fall back to per-row writes so outcomes stay per row
(`next_steps` 09 item edited, no new project).

## Sources
- https://raw.githubusercontent.com/n8n-io/n8n/master/packages/nodes-base/nodes/Supabase/Supabase.node.ts — opened 2026-11-13
- https://raw.githubusercontent.com/n8n-io/n8n/master/packages/nodes-base/nodes/Supabase/GenericFunctions.ts — opened 2026-11-13
- https://raw.githubusercontent.com/n8n-io/n8n/master/packages/nodes-base/nodes/Supabase/RowDescription.ts — opened 2026-11-13
- https://raw.githubusercontent.com/PostgREST/postgrest/main/docs/references/api/tables_views.rst — opened 2026-11-13
- https://raw.githubusercontent.com/PostgREST/postgrest/main/docs/references/transactions.rst — opened 2026-11-13
- https://raw.githubusercontent.com/postgres/postgres/master/doc/src/sgml/ref/insert.sgml — opened 2026-11-13
- https://raw.githubusercontent.com/supabase/supabase/master/apps/docs/content/guides/platform/billing-on-supabase.mdx — opened 2026-11-13
- https://raw.githubusercontent.com/supabase/supabase/master/apps/docs/content/guides/platform/compute-and-disk.mdx — opened 2026-11-13
- https://raw.githubusercontent.com/supabase/supabase/master/apps/docs/content/guides/deployment/going-into-prod.mdx — opened 2026-11-13
- https://community.clay.com/x/support/9fxy5mclv7u8/setting-up-http-api-for-clay-records-in-supabase-a — search summary, not opened (ENOTFOUND)
- https://www.clay.com/integrations/action/upsert-row-snowflake — search summary, not opened
- https://www.cloudzero.com/blog/supabase-pricing/ — search summary, not opened (Pro $25, $10 compute credit; max-rows default 1,000 not confirmed)
- https://supabase.com/docs/guides/api/rest/upsert , https://supabase.com/pricing — blocked (ENOTFOUND), not opened
