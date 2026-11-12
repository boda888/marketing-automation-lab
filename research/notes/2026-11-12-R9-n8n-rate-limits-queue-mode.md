# R9 · Rate-limiting vendor calls in n8n, and what queue mode changes (2026-11-12)

## Question
How do practitioners keep n8n under a vendor's rate limit (Smartlead, HubSpot, Clay webhooks,
LLM APIs): HTTP Request batching, Loop Over Items + Wait, Redis counters? What do those
features actually do in n8n's current source, and does queue mode change the picture once
several executions run at once?

## Findings
The n8n behaviour below comes from reading n8n's `master` source and docs repo on GitHub on
2026-11-12. docs.n8n.io did not resolve from this session, so the official "rate limits" page
is known only from a search summary. Nothing was run on a live n8n.

**HTTP Request batching staggers the start of requests. It does not cap how many are in
flight.** In `HttpRequestV3.node.ts` the item loop builds each request and starts it at once
(`this.helpers.request(...)` is pushed onto `requestPromises`, not awaited). Before item
`i`, it sleeps `batchInterval` ms only when `i % batchSize === 0`. Afterwards it awaits
all requests together with `Promise.allSettled`. So:
- If **Batching is not added** (the default is an empty `batch: {}`), batch size becomes 1
  and the interval is undefined, so the node sends no sleep at all: **every input item's
  request goes out at once**. With 2,000 leads, that is 2,000 concurrent requests.
- Once **Batching is added**, it defaults to **50 items per batch every 1,000 ms**. That
  means up to 50 requests start together each second, whether or not the earlier ones have
  answered. When the vendor is slow, in-flight requests pile up. Batch size 1 with an
  interval gives an even rate, but still no concurrency cap.
- A **429 is not retried by the node**. It only rewrites the error message to "Try spacing
  your requests out using the batching settings under 'Options'". `Retry-After` is never
  read. Unless "continue on fail" is on, one 429 fails the whole node. With Retry On Fail
  on, the whole node runs again with every item (R9 2026-11-01 note), which re-sends the
  requests that had already succeeded.

**The pacing applies to one execution only.** Batching and Wait-node delays exist inside a
single execution. Nothing in n8n shares them across executions. In queue mode, the main
process puts executions on Redis (Bull), and each worker runs **10 jobs in parallel by
default** (`n8n worker --concurrency`, from `enable-queue-mode.md`). Ten reply webhooks
arriving together, or a schedule that overlaps the previous run, each start their own "50
per second". `N8N_CONCURRENCY_PRODUCTION_LIMIT` limits executions per instance, not calls
per API key (R9 2026-11-01). Queue mode also needs Postgres ("SQLite isn't supported" for
the distributed setup). It cannot keep binary data on the filesystem (use S3). The main
process's encryption key must be shared with every worker.

**A Redis counter in n8n is the usual cross-execution fix, but the stock node makes it a
trap.** In `Redis.node.ts`, `incr` with Expire on runs `INCR` and then `EXPIRE key ttl`
**on every call**. The TTL therefore restarts on each increment. If calls arrive more often
than the TTL, a "per-minute" key never expires and the count only grows. A limiter that
checks `count <= N` then blocks every call for good once traffic is steady. The node also
**replaces the item** with `{ <key>: <count> }`, which drops the lead's fields. A Merge by
position or paired item is needed to get them back. For a fixed window, put the window in
the key name (e.g. `smartlead:2026-11-12T09:41`) and set the TTL once, or call the limiter
from a Code / HTTP step that owns its own logic.

**Wait node.** In the current source, every timed wait goes through
`putExecutionToWait(waitTill)`. The execution is parked and resumed later. Loop Over Items →
call → Wait (n s) → loop is therefore a sound way to pace one long batch inside one
execution. It still does not coordinate with other executions. The search-summary
community guides say the same thing: Retry On Fail is a safety net, and pacing is set from
each API's published limit.

## Apply
- On every HTTP Request node that writes to a vendor (sequencer add-lead, CRM upsert, Clay
  webhook, LLM call), **add Batching explicitly**. Set Items per Batch = 1 and Batch Interval
  = 1,000 ÷ (allowed requests per second) ms, with a small margin. Without the option, the
  node sends every item at once.
- Turn on "continue on fail" on those nodes. Route items whose error is `429` to a Wait node
  of at least the vendor's window (60 s for per-minute limits), and then back into the same
  call **for those items only**. Never use Retry On Fail for this: it re-sends the leads that
  already succeeded.
- If two workflows (or overlapping runs of one) hit the same API key, pace them through one
  shared budget. Either feed all writes into a single queue workflow (Schedule → read N
  ready rows → push) or check a Redis counter whose key holds the time window. Do not rely
  on the Redis node's Expire, which resets on every increment.
- For queue mode with worker concurrency 10, count the worst case as 10 × the per-execution
  rate. If that exceeds the vendor limit, lower `--concurrency` on the workers that run
  outbound pushes, or add the shared budget above.

## Lab impact
Root ROADMAP 09 rate-budget item edited. The kit's per-vendor budget must be shared across
executions and workers, not per process. It should be exposed to n8n as an `acquire` call
that returns how long to wait, and should honour `Retry-After` on 429. That matters because
n8n's own batching is per execution, starts requests without a concurrency cap and never
retries a 429. A shipped n8n example should set Batching explicitly on every write node and
include a fixed-window Redis key pattern, since the stock `incr` + Expire never expires
under steady traffic. New research questions were added to research/ROADMAP.md.

## Sources
- https://raw.githubusercontent.com/n8n-io/n8n/master/packages/nodes-base/nodes/HttpRequest/V3/HttpRequestV3.node.ts — opened 2026-11-12 (batch sleep, requests started unawaited, `Promise.allSettled`, 429 message)
- https://raw.githubusercontent.com/n8n-io/n8n/master/packages/nodes-base/nodes/HttpRequest/V3/Description.ts — opened 2026-11-12 (Batching defaults 50 items / 1,000 ms, -1 disables)
- https://raw.githubusercontent.com/n8n-io/n8n/master/packages/nodes-base/nodes/Redis/Redis.node.ts — opened 2026-11-12 (`incr` then `expire` on every call, item replaced by `{key: value}`)
- https://raw.githubusercontent.com/n8n-io/n8n/master/packages/nodes-base/nodes/Wait/Wait.node.ts — opened 2026-11-12 (`putExecutionToWait` for timed waits)
- https://raw.githubusercontent.com/n8n-io/n8n-docs/main/docs/deploy/host-n8n/configure-n8n/scaling/enable-queue-mode.md — opened 2026-11-12 (worker concurrency default 10, Postgres, S3 for binary data, webhook processors)
- https://docs.n8n.io/integrations/builtin/rate-limits — search summary, not opened (ENOTFOUND from this session 2026-11-12): Retry On Fail, or Loop Over Items + Wait
- https://community.n8n.io/t/handle-rate-limits-and-retries-for-external-apis/306975 — search summary, not opened (built-in retry ignores `Retry-After`)
