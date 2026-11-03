# R2 · Firecrawl `/monitor` vs n8n Cron + `changeTracking` for site-change signals (2026-11-03)

## Question
For a weekly "what changed on the account's site" signal (careers, pricing, about / news
pages of ~100 target accounts): should Firecrawl schedule the checks (`/monitor` + webhook
into n8n) or should n8n run a Cron and scrape with the `changeTracking` format itself? What
does each cost per 100 accounts per month, and what does the webhook actually carry into n8n?

## Findings
Method: firecrawl.dev and docs.firecrawl.dev were blocked by this run's network policy (as on
2026-10-23), so the docs were read from a fresh clone of `firecrawl/firecrawl-docs` at
`9842574` (committed 2026-11-02), including its two OpenAPI files (`v2-openapi.json`,
`webhooks-openapi.json`), and the n8n node was read from the npm tarball
`@mendable/n8n-nodes-firecrawl` 2.2.0 (published 2026-09-18). Nothing was run against the
live API; behaviour below is what the docs and schemas state.

**Monitor model (`features/monitoring*.mdx`, `MonitorCreateRequest`).** A monitor has 1–50
targets of three types: `scrape` (a list of known URLs — the schema sets no max on `urls`),
`crawl` (one start URL + `crawlOptions` such as `limit`, `includePaths`) and `search`
(1–12 queries, `searchWindow` 5m … 7d, `maxResults` 1–50 combined across queries).
Schedules are cron or text (`weekly`, `daily at 9am`), minimum 5 minutes; text schedules are
spread by monitor id. Monitor scrapes default `maxAge: 0` (always fresh). Each page in a check
gets `same / new / changed / removed / error`. An optional plain-language `goal` turns on a
"judge" that marks changed pages `isMeaningful` with `confidence` and `meaningfulChanges`.
`retentionDays` defaults to 30 (max 365).

**Cost (docs pricing table, `billing.mdx`).** No per-monitor fee: 1 credit per URL per check
(scrape target), 1 per discovered page per check (crawl target), 2 per 10 results (search
target), +1 per changed page the judge validates, format add-ons as in a normal scrape
(JSON-mode change tracking = 5 credits per page). The create response returns
`estimatedCreditsPerMonth` (an upper bound when judging is on: the docs' 1-URL / 30-minute
example estimates 2,880). Each check also reports `estimatedCredits` vs `actualCredits`.
Plain scrapes with `changeTracking` (markdown / git-diff mode) cost the same 1 credit per
page — change tracking itself is free — so **the two approaches cost the same per page**:
100 accounts × 3 pages × ~4.3 weekly checks ≈ **1,300 credits / month**, plus at most one
judge credit per changed page on `/monitor`. A `crawl` target instead of 3 named URLs pays
for every page discovered, every week.

**What the webhook carries (`webhooks-openapi.json` → `MonitorPageData`,
`MonitorCheckCompletedData`).** Two events: `monitor.page` (sent as each page's scrape
finishes) and `monitor.check.completed` (summary counts). Gaps that matter for a signal
pipeline:
- `monitor.page` has **no HTTP `statusCode`** (the check API's page object has it) and its
  status enum is `same / new / changed / error` — **no `removed`**. A job page that
  disappeared shows up only in `check.completed` summary counts and in
  `GET /v2/monitor/{id}/checks/{checkId}?status=removed`.
- `monitor.page` has no `targetId` either (the check API has it): map back to the account by
  URL, so keep URLs byte-identical between the account table and the monitor.
- `monitor.check.completed` statuses are `completed / partial / failed / skipped_overlap`;
  a check refused for credits is recorded as `skipped_no_credits` but that status is **not in
  the webhook enum**, and the docs say running out of credits never pauses the monitor and
  there is "no failure counter": the n8n side just goes quiet. The only way to see it is
  `GET /v2/monitor/{id}/checks` (newest first) and counting the leading skips.
- Delivery: 2xx within 10 s, three retries after 1 / 5 / 15 min, then marked failed — an n8n
  outage longer than ~21 minutes loses the event; the check API keeps the result for
  `retentionDays`.
- Signing: every webhook carries `X-Firecrawl-Signature: sha256=<HMAC-SHA256 of the raw
  body>` with the account's webhook secret; the docs' top cause of failed checks is hashing
  the parsed JSON instead of the raw body. Custom `headers` (e.g. a bearer token) can be set
  per monitor as well.

**Search targets as a funding / leadership / launch signal (`monitoring-web-scale.mdx`).**
A `search` monitor alerts once per **canonical URL** (`metadata.searchStatus`: `alert`,
`already_seen`, `watching`, `ignored`, `skipped`). The docs state outright that one
real-world event reported across many article URLs alerts once per URL, not once per event.
Search credits are billed on raw results of every query before merge / dedupe. Editing the
goal or queries re-judges results lazily, only when they resurface.

**Snapshot sharing (`change-tracking.mdx`).** A `changeTracking` scrape compares with the
most recent snapshot of the same URL **by any scrape of the team** (same markdown format and
`tag`); snapshots never expire. Without a `tag`, an ad-hoc scrape of `/careers` from Clay or
a test run silently becomes the baseline of next week's diff. Different `onlyMainContent` /
`includeTags` settings between runs make diffs unreliable. `changeTracking` requests bypass
the cache (`maxAge` ignored).

**n8n.** The vendor node 2.2.0 has no monitor operations (only keywords mention
"monitor"); its Scrape / Batch Scrape support the `changeTracking` format with `modes`,
`schema`, `prompt` and `tag`. So `/monitor` from n8n means HTTP Request nodes to create /
read monitors plus a Webhook node for events. The Firecrawl MCP server exposes
`firecrawl_monitor_*` tools (docs warn that `firecrawl_monitor_delete` is permanent).

**Not covered.** Website-visitor identification (RB2B) was searched: RB2B's support site was
DNS-blocked; a search summary (not opened) says RB2B's webhook is Pro-only, sends person
fields (LinkedIn URL, name, title, company, business email, city/state/zip, seen-at,
captured URL) with an optional `is_repeat_visit` flag, and that its API is an OEM partner
API without a visitors endpoint. Match rates and US-only coverage were not found.

## Apply
- **Pick by who needs the status code.** For "hiring / pricing / about page changed" on a
  fixed URL list, an n8n Cron (weekly) → Firecrawl Scrape with `changeTracking` (`tag:
  "weekly-signal"`) is the same credits as `/monitor` and returns `metadata.statusCode` and
  `changeStatus` including `removed` on every item — easier to turn into evidence. Use
  `/monitor` when you want Firecrawl to own the schedule or want its judge to filter noise.
- **Always set a `tag`** on signal change-tracking scrapes (and keep `onlyMainContent` the
  same every run), so a manual scrape in Clay or a test does not become the baseline.
- **If you use `/monitor`:** subscribe to both events; on `monitor.check.completed` with
  `summary.removed > 0` or `error > 0` call the check API to get the URLs and status codes;
  verify `X-Firecrawl-Signature` on the raw body (n8n Webhook node → Raw Body on); and add a
  weekly n8n watchdog that reads `GET /v2/monitor/{id}/checks` and alerts on
  `skipped_no_credits` or on a `lastRunAt` older than the schedule — the webhook never says
  "out of credits".
- **Budget:** 100 accounts × 3 pages weekly ≈ 1,300 credits / month — fits Hobby's 5,000;
  a `crawl` target per account does not. Check `estimatedCreditsPerMonth` on create and
  `actualCredits` per check.
- **Funding / launch search monitors:** dedupe in your own step on company + event type +
  month before scoring or alerting — Firecrawl dedupes per article URL, so one funding round
  covered by five outlets arrives five times.

## Lab impact
- 03 signal engine "first-party page changes" source (root `ROADMAP.md`): accept both input
  shapes — a scrape item with `changeTracking` (`changeStatus`, `previousScrapeAt`,
  `metadata.statusCode`) and a `/monitor` page event, where `statusCode` is absent and must
  map to `Unknown` until confirmed via the check API; `removed` only arrives via the check
  API. Edited that ROADMAP item rather than adding a new one.
- 03 dedupe: search-monitor results are per URL, so event dedupe (company + event type +
  date bucket) stays in code — already a PRACTICE rule ("same funding event counted twice").
- 09 reliability kit: "silent schedule" watchdog (no event within the expected interval →
  alert) is a generic need; added to the existing 09 next step.

## Sources
- https://github.com/firecrawl/firecrawl-docs/blob/98425740e69a81817131f7227abadf550cc3fbcc/features/monitoring.mdx — opened (git clone) 2026-11-03
- https://github.com/firecrawl/firecrawl-docs/blob/98425740e69a81817131f7227abadf550cc3fbcc/features/monitoring-page.mdx — opened (git clone) 2026-11-03
- https://github.com/firecrawl/firecrawl-docs/blob/98425740e69a81817131f7227abadf550cc3fbcc/features/monitoring-website.mdx — opened (git clone) 2026-11-03
- https://github.com/firecrawl/firecrawl-docs/blob/98425740e69a81817131f7227abadf550cc3fbcc/features/monitoring-web-scale.mdx — opened (git clone) 2026-11-03
- https://github.com/firecrawl/firecrawl-docs/blob/98425740e69a81817131f7227abadf550cc3fbcc/features/change-tracking.mdx — opened (git clone) 2026-11-03
- https://github.com/firecrawl/firecrawl-docs/blob/98425740e69a81817131f7227abadf550cc3fbcc/api-reference/v2-openapi.json — opened (git clone) 2026-11-03
- https://github.com/firecrawl/firecrawl-docs/blob/98425740e69a81817131f7227abadf550cc3fbcc/api-reference/webhooks-openapi.json — opened (git clone) 2026-11-03
- https://github.com/firecrawl/firecrawl-docs/blob/98425740e69a81817131f7227abadf550cc3fbcc/webhooks/overview.mdx — opened (git clone) 2026-11-03
- https://github.com/firecrawl/firecrawl-docs/blob/98425740e69a81817131f7227abadf550cc3fbcc/webhooks/security.mdx — opened (git clone) 2026-11-03
- https://github.com/firecrawl/firecrawl-docs/blob/98425740e69a81817131f7227abadf550cc3fbcc/mcp-server/tools.mdx — opened (git clone) 2026-11-03
- https://registry.npmjs.org/@mendable/n8n-nodes-firecrawl/-/n8n-nodes-firecrawl-2.2.0.tgz — opened (npm tarball) 2026-11-03
- https://support.rb2b.com/en/articles/8976614-setup-guide-webhook — search summary, not opened (DNS-blocked 2026-11-03)
- https://reply.io/blog/rb2b-api/ — search summary, not opened
