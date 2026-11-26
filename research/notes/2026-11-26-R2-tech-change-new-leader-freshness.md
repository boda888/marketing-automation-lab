# R2 · Tech-change and "new leader" signals: what date they really carry (2026-11-26)

## Question
Which tech-change and "new leader" (executive hire / job change) sources are fresh enough to
act on within a week, and what date can a signal from them honestly carry? Covered: BuiltWith
(Domain API + Change API), Wappalyzer (lookup API), People Data Labs (person job-change
fields, company exec-hire arrays, changelog), Apollo job-change alerts.

The docs hosts `api.builtwith.com`, `www.wappalyzer.com`, `docs.peopledatalabs.com` and
`knowledge.apollo.io` did not resolve from this session (DNS, 2026-11-26). The findings
come from the vendors' own packages on npm (BuiltWith's official SDK and MCP server, the
official `peopledatalabs` JS SDK) and Pipedream's Wappalyzer component, plus labelled search
summaries. Nothing was called live; no credits were spent.

## Findings

### BuiltWith: per-technology first / last seen, plus a crawl date per path
- The official MCP server (`@builtwith/mcp-server` 1.13.0, published 2026-11-10, author
  BuiltWith) reads Domain API v26 (`v26/api.json`) and keeps, per technology:
  `FirstDetected`, `LastDetected` (numeric timestamps), `confidence` (0–1, new in v26) and,
  per crawled path, `LastIndexed` (exposed as `PathLastIndexed`). The SDK README says v26
  adds `confidence` and `Meta.company_reg`.
- **Change API** (`change1/api.json`, tool `change-api`): "technology additions and removals
  by domain", one or more comma-separated domains, optional `SINCE` as natural language
  ("last month"), **default 3 months**. The response shape is not in the package.
- **Lists API** (`lists-api`) takes `SINCE` too, plus numeric filters (`EMPLOYEES=50|GTE`,
  `REVENUE`, `SPEND`…) — i.e. "sites that started using X since …" for sourcing.
- `whoami` (account limits, credit cost per endpoint) and `usage` (balance) cost no credits;
  the Agent Payment API buys credits in blocks of ≥ 2,000. Credit price per call: not found.
- Freshness depends on how recently BuiltWith re-crawled the site, which is why
  `LastIndexed` matters: a technology was **removed** only if the path was indexed *after*
  its `LastDetected`; if `LastIndexed` is old, absence means "not re-checked", not "removed".
  An added technology is dated in the interval (previous crawl that did not see it,
  `FirstDetected`] — the previous crawl date is not in the payload, so the honest precision
  is "on or before `FirstDetected`".
- Search summaries (not opened): BuiltWith's dataset schema stores `FirstDetected` /
  `LastDetected` as millisecond timestamps; its AI-agent guide lowers confidence by 0.25 when
  `FirstDetected == LastDetected` (seen once). Third-party Apify scrapers expose month-level
  strings ("2019-11", "Apr 2021"), and a live-scan scraper leaves both dates null.

### Wappalyzer: verification months, not change events
- `GET https://api.wappalyzer.com/v2/lookup/?urls=…` with `x-api-key` (Pipedream component
  `@pipedream/wappalyzer` 0.1.1, parameter texts copied from Wappalyzer's docs).
- Cached results by default: `max_age` (months, default **2**) = verified at least once in
  the last N months; `min_age` + `squash=false` returns results **grouped by month** (up to
  12 months per request) — that is the only history, so a tech change is a month diff.
- `live=true` scans now; with `recursive=true` (default) the crawl is asynchronous (up to
  15 min), results come via `callback_url` (required for live + recursive) or by repeating
  the request (free for an hour). `denoise` (default true) drops low-confidence hits.
- Price conflict: the component text says live + recursive costs **two** credits instead of
  one; a search summary of the current lookup docs says 1 credit per URL and **5** with
  live + recursive. Rate limit 10 req/s with ≤ 10 URLs per request (search summary).
- Search summary only: third-party actors expose a `confirmedAt` Unix timestamp per
  technology; not confirmed in the official schema.

### People Data Labs: "observed" dates, monthly releases
- Person record (`peopledatalabs` 14.8.0 types): `job_start_date`, `job_last_changed`,
  `job_last_verified`, per-employer `job_history` with `start_date` / `end_date` /
  `first_seen` / `last_seen` / `is_primary`.
- Company record: `recent_exec_hires[]` (`joined_date`, title class / levels / role,
  `previous_company_*`), `recent_exec_departures[]` (`departed_date`, `new_company_*`),
  `recent_exec_internal_changes[]` (`new_role_date`, `previous_job_title*`) — a ready-made
  "new leader" list per account. `technologies_used[]` carries `inferred_first_used`,
  `inferred_last_used`, `job_posting_first_observed` / `_last_observed`: tech inferred from
  job posts and résumés, not from the website.
- Person Retrieve takes `filter_updated: 'job_change' | …` and returns `billed: boolean`;
  the Changelog API diffs two dataset versions (`originVersion`, `currentVersion`, `type`
  added / deleted / updated / merged / opted_out, `fields_updated`, `scroll_token`).
- Search summaries (PDL changelog pages, not opened): monthly data releases since Jan 2022
  with a major release each quarter; `job_last_changed` is when PDL **observed** the change
  (a move made on 1 Oct and made public on 1 Dec reads December); `job_last_verified`
  replaced `job_last_updated` in v27.0 (Jul 2024); the May 2026 release (v34.1) moved some
  timestamps to capture time, so some `job_last_verified` dates went *earlier*.
- Consequence: a PDL job change can be weeks old when you first see it (public-profile lag +
  up to a month of release cadence). Date the signal by `job_start_date` (often month
  precision), keep `job_last_changed` as "observed", and decide freshness on the start date.

### Apollo job-change alerts
Search summary only (help centre DNS-blocked): alerts on saved contacts who changed
company, pushed to Salesforce / HubSpot / Pipedrive after field mapping, 1 credit per
updated contact, plan-dependent; no documented job-change webhook and no stated detection
frequency ("continuously refreshed" is a vendor claim).

### Which of these is fresh enough for a 7-day play
| Source | Event | Best honest date | Fresh within a week? |
|---|---|---|---|
| BuiltWith Change / Domain API | tech added / removed | ≤ `FirstDetected`; removal only if `LastIndexed` > `LastDetected` | only if the site was re-crawled recently — check `LastIndexed` |
| Wappalyzer cached | tech verified in month M | month | no (month buckets, `max_age` 2) |
| Wappalyzer `live=true` | tech present now | your scan time | yes, as your **own** diff vs your last scan |
| PDL person / company | job change, exec hire | `job_start_date` / `joined_date` (month?) | rarely: observation lag + monthly release |
| Apollo alerts | contact changed company | not documented | unknown |
| Own scrape diff (team / leadership page, R2 2026-10-23 note) | new name on page | (previous scrape, this scrape] | yes, at your scrape interval |

## Apply
- **Tech-change in Clay / n8n:** when you use BuiltWith, keep `FirstDetected`, `LastDetected`
  **and** the path's `LastIndexed` columns. Write "recently added X" only when
  `FirstDetected` is inside your window; treat "dropped X" as true only when `LastIndexed` is
  newer than `LastDetected`, otherwise mark it "not re-checked". Run `whoami` (free) once to
  see the per-endpoint credit cost before a bulk Change API run.
- **Weekly tech diff you control:** for a short target list, run Wappalyzer `live=true,
  recursive=false` (synchronous, one URL per request) on a schedule and diff against last
  week's set in your ops table — the date is your scan, which is fresher than any vendor's
  history. Use `max_age=1` for cached lookups when you only need "uses X now".
- **New-leader plays:** pull PDL company `recent_exec_hires` (or person `job_start_date`) and
  gate the copy on the **start** date (e.g. ≤ 90 days), never on `job_last_changed`; say "you
  joined in <month>" only at the date's precision. For a 7-day play, watch the account's own
  leadership / team page with a scrape diff instead.
- Never write "you just switched to X" from a month-level or crawl-lagged date.

## Lab impact
- 03 signal engine: tech-change and leadership adapters should date events as intervals:
  BuiltWith add → `(None, FirstDetected]`, removal only when `LastIndexed > LastDetected`
  (else `Unknown`), Wappalyzer month buckets → month precision, PDL `joined_date` /
  `job_start_date` as the event date with `job_last_changed` kept as observed-at provenance.
  Added to ROADMAP 03's signal-source item.

## Sources
- https://registry.npmjs.org/@builtwith/mcp-server/-/mcp-server-1.13.0.tgz — opened 2026-11-26 (`bw-mcp-v1.js`, `package.json`)
- https://registry.npmjs.org/@builtwith/sdk/-/sdk-1.10.0.tgz — opened 2026-11-26 (`src/index.js`, `README.md`)
- https://registry.npmjs.org/@pipedream/wappalyzer/-/wappalyzer-0.1.1.tgz — opened 2026-11-26 (`wappalyzer.app.mjs`, `actions/get-technologies/get-technologies.mjs`)
- https://registry.npmjs.org/peopledatalabs/-/peopledatalabs-14.8.0.tgz — opened 2026-11-26 (`dist/types/common-types.d.ts`, `retrieve-types.d.ts`, `changelog-types.d.ts`, `README.md`)
- https://builtwith.com/datasets/schema — search summary, not opened
- https://api.builtwith.com/llms.txt — search summary, not opened (DNS-blocked)
- https://www.wappalyzer.com/docs/api/v2/lookup/ — search summary, not opened (DNS-blocked)
- https://docs.peopledatalabs.com/changelog/april-2024-release-announcement-v260 — search summary, not opened
- https://docs.peopledatalabs.com/changelog/may-2026-release-notes-v341 — search summary, not opened
- https://docs.peopledatalabs.com/docs/data-updates — search summary, not opened (DNS-blocked)
- https://knowledge.apollo.io/hc/en-us/articles/5130064363661-Use-Job-Change-Alerts-to-Enrich-Contacts — search summary, not opened (DNS-blocked)
