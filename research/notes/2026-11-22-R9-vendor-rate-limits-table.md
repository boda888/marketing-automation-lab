# R9 · Vendor rate limits in one table: scope, window, headers, who retries a 429 (2026-11-22)

## Question
What rate limits do the outbound stack's APIs publish — Smartlead, Instantly, lemlist,
HeyReach, Apollo, HubSpot, Attio, Pipedrive — per key or per account, in which window, and
with which `Retry-After` / reset headers? Which vendor clients retry a 429, and which also
retry a write that may already have landed? The answers set the lab's rate budgets
(ROADMAP 09 "shared rate budget") from published numbers rather than guesses.

## Findings
**What was and was not opened.** The vendors' doc hosts did not resolve from this session:
docs.attio.com, pipedrive.readme.io, developer.instantly.ai, docs.apollo.io,
api.smartlead.ai, developer.lemlist.com and feedback.instantly.ai all returned ENOTFOUND or a
proxy 403 on 2026-11-22. So **every number in the table is a web-search summary of the
vendor's own page. None was opened.** Where search results disagree, the table gives the
range and says so. Two vendor packages were opened and read: `pipedrive` 36.3.1 with its
CHANGELOG, and `@lemlist-official/cli` 0.4.0. The client behaviour below comes from those
packages. HubSpot and HeyReach client behaviour is from earlier notes (R7 2026-10-29,
R5 2026-11-06), not re-read today.

| Vendor | Published limit (search summary 2026-11-22) | Scope | Window | Signals | Confidence |
|---|---|---|---|---|---|
| Instantly API v2 | 100 req/s **and** 6,000 req/min (changelog March 2026: "10x"); older page: 100 per 10 s, 600 / min | whole **workspace**, all keys, v1 + v2 shared (older page) | both windows apply; any one exceeded → 429 | 429; `Retry-After` only claimed by a third-party blog | medium (official page + changelog agree on the new numbers) |
| Smartlead | help center: admin keys share **one pool of 200 / min**; each client key 60 / min; a few heavy endpoints 10 per 60 s. API docs: "tied to your plan", window resets 60 s after the first request | per key, with admin keys pooled | fixed 60 s window from first call | 429 | low (sources disagree; check the dashboard) |
| lemlist | not found in the summaries | team | ? | 429 → CLI exit code 3 | unknown |
| HeyReach | 300 / min (third-party skill pages, R5 2026-11-06) | ? | ? | 429 | low |
| Apollo | Basic / Organization 200 / min, 400 / h, 2,000 / day; Professional 200 / min, 600 / h, 6,000 / day; Free 50 / min, 200 / h, 600 / day (bulk endpoints 20 / min, 100 / h). An older table differs | team, per endpoint; MCP server also limits per IP | **fixed** minute, hour **and** day windows, all apply | `x-rate-limit-minute`, `x-rate-limit-hourly`, `x-rate-limit-24-hour`; `retry-after` (s until the exhausted window resets) on 429; `GET /api/v1/usage_stats/api_usage_stats` returns the team's live limits, 0 credits | medium |
| HubSpot | 100 per 10 s per OAuth app (SDK README, R7); private-app burst / daily by tier — search results disagree (R7) | per app per portal, daily per portal | rolling 10 s + daily | 429 with policy `TEN_SECONDLY_ROLLING` / daily | medium |
| Attio | 100 reads/s, 25 writes/s (R7 2026-10-29) | whole workspace | per second | 429 | medium |
| Pipedrive | daily **token** budget per company (each call costs tokens; search ≈ 40, list ≈ 20 — third-party) + burst per token on a rolling 2 s window (Lite 20 … Ultimate 120 per 2 s — paid-partnership blog only) | company (daily), token (burst) | rolling 2 s + daily | `x-ratelimit-limit / -remaining / -reset` on burst (third-party); community thread: the daily-token headers named in docs **do not appear** | low for the numbers, medium for the model |

**Three patterns that matter for a shared budget:**
1. **Scope is the account, not the key, for most of them.** Instantly counts the whole
   workspace across every key and both API versions. Attio counts the workspace. Pipedrive's
   daily budget belongs to the company. Smartlead pools all admin keys together. Adding a
   second API key gives no extra capacity. Two n8n workflows on the same workspace share
   one limit even when they use different keys.
2. **Several windows apply at once.** Instantly enforces per second and per minute. Apollo
   enforces minute, hour and day. Pipedrive enforces 2 s and day. A budget with a single
   rate passes the per-second check and then hits the daily wall at noon. Apollo's daily
   cap (2,000 on Basic) is the one an enrichment backfill of 5,000 contacts actually hits.
3. **Pipedrive's limits are measured in tokens, not requests.** A search costs about twice
   a list call (third-party figure). "Search then create" (R7 2026-10-29) therefore spends
   most of the daily budget on searches. The budget must charge a cost per endpoint, not
   count every call as 1.

**Who retries a 429, and who also retries a write:**
- **`@lemlist-official/cli` 0.4.0** (`dist/service/api-client.js`, `lemlist-cli-conf.js`):
  3 attempts with a 30 s timeout. It **always** retries a 429, because "a 429 means the
  server rejected the request, so replaying is always safe". It uses `Retry-After` in
  seconds, capped at 10 s, otherwise backs off 500 ms → 1 s → 2 s (also capped at 10 s).
  It retries 5xx and network errors **only for GET / HEAD / OPTIONS / PUT / DELETE**,
  never for a POST, "so no double-created leads". When retries run out it exits with code 3
  (rate limit). The 10 s cap means a per-minute window is not ridden out: after three tries
  in about 20 s the call fails, and the caller must wait for the window.
- **`pipedrive` 36.3.1**: **no retries at all.** Before 36.1.0 (2026-10-21) the SDK dropped
  all response headers. Every method now has a `…WithHttpInfo` variant that returns
  `{data, status, headers}`. On failure it rejects with `$response: {status, headers}`, so
  `Retry-After` on a 429 and `x-ratelimit-remaining` are readable. As a fix in 36.3.x the
  interceptor keeps headers even on empty error bodies. The plain methods still return only
  the body.
- HubSpot SDK: retries and limiter both off unless configured (R7). HeyReach's third-party
  client retries 429 / 5xx up to 3 times and so re-sends a timed-out add-leads (R5).

The lemlist CLI's rule is the same one the lab's `decide()` table already applies (409 / 429
safe to replay, possibly-landed creates never re-sent). It is a vendor endorsing that rule,
not a new requirement.

## Apply
- **Size the n8n Batching from the account limit divided by the workflows that share it,**
  not from one workflow's view. Instantly: 100 / s is generous, but 6,000 / min is
  100 / s only if nothing else runs. Smartlead: if admin keys share 200 / min, three
  workflows on admin keys get about 60 / min each, so set Batch Interval ≥ 1,000 ms per
  item on each. Prefer per-client keys (60 / min each) for client workspaces.
- **Apollo enrichment / search in n8n or a Clay HTTP column:** call
  `GET /api/v1/usage_stats/api_usage_stats` (0 credits) at the start of a run and stop the
  batch before the daily cap. Do not let it 429 for the rest of the day. On a 429, wait the
  `retry-after` seconds and do not count it as a failed row. A Wait of 60 s is wrong when
  the hourly or daily window is the one exhausted.
- **Pipedrive from n8n:** cache `person search by email` results in your ops table (Supabase
  / Airtable) and search only for keys you have never seen. Searches are the expensive
  calls in the token budget. If you call the SDK from a Code node, use the `…WithHttpInfo`
  methods (≥ 36.1.0) and log `x-ratelimit-remaining`.
- **lemlist CLI / skill in an agent:** exit code 3 means it already retried for about 20 s.
  Re-queue the lead for the next window rather than relaunching the command in a loop.

## Lab impact
ROADMAP 09's open "shared rate budget" item is now specified: a budget per **account
scope** (workspace / company / admin pool, not key), with **several windows at once**
(second / minute / hour / day, all must pass) and a **cost per endpoint** (Pipedrive
tokens). On a 429, `Retry-After` overrides the window. The lab's vendor limits start from
the table above and are marked "search summary" until a doc page is opened. Edited in the
root ROADMAP 09 item. No new item was added.

## Sources
- https://registry.npmjs.org/@lemlist-official/cli/-/cli-0.4.0.tgz — opened 2026-11-22 (`dist/service/api-client.js`: `RETRYABLE_STATUSES`, `IDEMPOTENT_METHODS`, `canRetry`, `retryDelayMs`; `dist/lemlist-cli-conf.js`: 3 attempts, 500 ms base, 10 s cap, 30 s timeout, exit code 3; `agent-plugin/skills/lemlist-cli/SKILL.md`)
- https://registry.npmjs.org/pipedrive/-/pipedrive-36.3.1.tgz — opened 2026-11-22 (`dist/versions/v2/base.js`: response / error interceptors, `toFullResponseError`, no retry logic)
- https://raw.githubusercontent.com/pipedrive/client-nodejs/master/CHANGELOG.md — opened 2026-11-22 (36.1.0, 2026-10-21: `WithHttpInfo`, `$response` with `Retry-After`)
- https://developer.instantly.ai/getting-started/rate-limit — search summary, not opened (ENOTFOUND)
- https://feedback.instantly.ai/changelog/10x-api-rate-limits-new-developer-docs-are-now-live — search summary, not opened (ENOTFOUND)
- https://api.smartlead.ai/guides/rate-limits, https://helpcenter.smartlead.ai/en/articles/68-how-to-get-started-with-the-smartlead-api — search summary, not opened (ENOTFOUND / proxy 403)
- https://docs.apollo.io/reference/rate-limits, https://docs.apollo.io/reference/view-api-usage-stats — search summary, not opened (ENOTFOUND)
- https://pipedrive.readme.io/docs/core-api-concepts-rate-limiting, https://truto.one/blog/how-to-integrate-with-the-pipedrive-api-2026-engineering-guide/, https://devcommunity.pipedrive.com/t/headers-x-daily-ratelimit-token-limit-and-x-daily-ratelimit-token-remaining-dont-exist-in-response/19998 — search summary, not opened
- https://docs.attio.com/rest-api/guides/rate-limiting, https://developer.lemlist.com/api-reference/getting-started/rate-limits — not opened (ENOTFOUND); Attio numbers carried from R7 2026-10-29
