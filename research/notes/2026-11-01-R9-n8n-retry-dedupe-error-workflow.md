# R9 · n8n retries, cross-run dedupe, error workflows and concurrency (2026-11-01)

## Question
Which reliability patterns do outbound n8n stacks rely on — node retries, per-item loops,
"remove items processed in previous executions", error workflows, concurrency limits — and
what do these features actually do in n8n's current source, so a lead is never pushed twice
or lost silently?

## Findings
All facts below come from reading n8n's `master` source and docs repo on GitHub on
2026-11-01. Nothing was run against a live n8n instance; the behaviour is a reading of the
code, not an observed result.

**Retry On Fail is per node, capped, fixed-delay.** `WorkflowExecute.getRetryParams` clamps
`maxTries` to 2–5 (default 3) and `waitBetweenTries` to 0–5,000 ms (default 1,000), and
waits the same delay every time — no exponential backoff, no `Retry-After`. A retry re-runs
the **whole node with all its input items**: if an HTTP Request node pushing 100 leads throws
on lead 57, the retry sends leads 1–56 again. For nodes that return an error item instead
of throwing, `checkFailure` only looks at the first item of the first output
(`data[0][0].json.error`). So Retry On Fail is only safe on idempotent calls (upsert, GET,
assert) or with one item per execution; a 429 storm with a 60 s window cannot be ridden out
by 5 × 5 s retries.

**Remove Duplicates v2 → "Remove Items Processed in Previous Executions"** is n8n's built-in
cross-run dedupe. Keep-logic options: value is new (default, stores up to `historySize`
keys, default 10,000), value higher than any stored (incremental id), or date later than any
stored. Scope `node` (default) or `workflow`. Three properties matter for outbound:
1. Keys are recorded **when the node runs** (`checkProcessedAndRecord`), before anything
   downstream. If the sequencer push after it fails, the lead is already marked as seen and
   the next run drops it — lost, not retried.
2. The node throws `"The number of items to be processed exceeds the maximum history size"`
   when stored count + batch size > `historySize`. The store evicts oldest keys down to
   `historySize`, so a full store sits at the cap, and by this reading any further input
   then fails the node until the history is cleared or raised. The node adds a warning hint
   once the store is over 50 % full.
3. Seen items go out on a second output, so duplicates can be logged instead of silently
   dropped.

**Loop Over Items (Split in Batches v3)** has outputs `done` and `loop`, batch size default 1,
and a `reset` option; the node itself notes that most nodes already run once per item. It is
the tool for "one lead per item through a fan-out lookup" (PRACTICE: 5 campaign items per
lead → 555 items), not a rate limiter.

**Error workflows fire only on a failed execution.** `executeErrorWorkflow` runs only when
`resultData.error` is set and never for manual runs. Items sent to a node's error output or
swallowed by "continue on fail" do not reach it. The payload carries execution id, URL,
error message/stack, `lastNodeExecuted`, mode and `retryOf` — **no input items** — so the
error workflow cannot tell which lead failed without reading the execution back through the
API. If error executions are not saved (`saveDataErrorExecution` off), the error workflow
still fires but its URL points to an execution that was never stored. An error workflow is
not triggered for itself.

**Concurrency control** (self-hosted): `N8N_CONCURRENCY_PRODUCTION_LIMIT` (off by default)
queues production executions FIFO above the limit in regular mode and also sets worker
concurrency in queue mode. It does not apply to sub-workflow, manual, error or CLI
executions; queued executions cannot be retried, and cancelling one drops it. It limits
executions per instance, not calls per vendor API key, so it is not a rate limiter for
Smartlead / HubSpot / Clay.

## Apply
- Turn Retry On Fail **off** on every create / push node that receives more than one item
  (sequencer add-lead, CRM create, Clay row push, AI call create); retry only upserts, or
  loop one item per execution and retry inside that.
- Put "Remove Items Processed in Previous Executions" **after** the push succeeds, not
  before it — or dedupe on your own store with a reserve → commit step. Before the first
  run raise History Size well above the expected lifetime volume, and log the second
  (duplicates) output.
- Use the error workflow for alerts only ("execution X failed at node Y"); send per-lead
  failures from each node's error output to a DLQ sheet / table with the lead id, and keep
  "Save failed executions" on so the alert link opens.
- Rate-limit per vendor key in the call path (batch interval / Wait node / your own service),
  not with `N8N_CONCURRENCY_PRODUCTION_LIMIT`.

## Lab impact
Root ROADMAP 09 item edited: the reliability kit should ship per-item retries with
exponential backoff + `Retry-After` for idempotent calls only, a dedupe store that commits a
key only after the downstream step succeeds, per-item DLQ intake from error outputs (the
error workflow is alert-only and carries no items), and rate budgets per vendor key.

## Sources
- https://raw.githubusercontent.com/n8n-io/n8n/master/packages/core/src/execution-engine/workflow-execute.ts — opened 2026-11-01 (`getRetryParams`, retry loop, `checkFailure`)
- https://raw.githubusercontent.com/n8n-io/n8n/master/packages/nodes-base/nodes/Transform/RemoveDuplicates/v2/RemoveDuplicatesV2.description.ts — opened 2026-11-01
- https://raw.githubusercontent.com/n8n-io/n8n/master/packages/nodes-base/nodes/Transform/RemoveDuplicates/v2/RemoveDuplicatesV2.node.ts — opened 2026-11-01
- https://raw.githubusercontent.com/n8n-io/n8n/master/packages/cli/src/deduplication/deduplication-helper.ts — opened 2026-11-01 (eviction to `maxEntries`)
- https://raw.githubusercontent.com/n8n-io/n8n/master/packages/nodes-base/nodes/Transform/RemoveDuplicates/v2/test/RemoveDuplicates.test.ts — opened 2026-11-01
- https://raw.githubusercontent.com/n8n-io/n8n/master/packages/nodes-base/nodes/SplitInBatches/v3/SplitInBatchesV3.node.ts — opened 2026-11-01
- https://raw.githubusercontent.com/n8n-io/n8n/master/packages/nodes-base/nodes/ErrorTrigger/ErrorTrigger.node.ts — opened 2026-11-01
- https://raw.githubusercontent.com/n8n-io/n8n/master/packages/cli/src/execution-lifecycle/execute-error-workflow.ts — opened 2026-11-01
- https://raw.githubusercontent.com/n8n-io/n8n/master/packages/cli/src/execution-lifecycle/execution-lifecycle-hooks.ts — opened 2026-11-01 (manual runs excluded, error workflow fires even when the execution is not saved)
- https://raw.githubusercontent.com/n8n-io/n8n-docs/main/docs/deploy/host-n8n/configure-n8n/scaling/control-concurrency.md — opened 2026-11-01
- Practitioner write-ups and Make / Zapier / Clay comparisons not researched this run (open questions kept in research/ROADMAP.md).
