# 09 · n8n reliability kit

**The pain:** branches that fail silently, and retries that do more harm than the failure.
n8n's Retry On Fail re-runs the **whole node with every input item** (2–5 tries, a fixed
wait of at most 5 s, no `Retry-After`), so a 429 on lead 57 of 100 re-sends leads 1–56 to
the sequencer. The HTTP Request node never retries a 429 by itself, and the error workflow
carries no items, so the lead that failed is not named anywhere a person looks (research
[R9 2026-11-01](../../research/notes/2026-11-01-R9-n8n-retry-dedupe-error-workflow.md),
[R9 2026-11-12](../../research/notes/2026-11-12-R9-n8n-rate-limits-queue-mode.md)).

v0.0.1 (started 2026-11-18): **per-item retries** (`reliability.retry`).
v0.0.2 (2026-11-19): the same rule table over HTTP for n8n, `POST /v1/decide` (`reliability.api`).
v0.0.3 (2026-11-19): the n8n loop that uses it, [`n8n/retry-loop.json`](n8n/retry-loop.json),
replayed offline against the service.
v0.0.4 (2026-11-19): **dedupe keys that commit only after success** (`reliability.dedupe`,
`/v1/keys/*`).
v0.0.5 (2026-11-20): **dead-letter store with a replay that refuses unsafe rows**
(`reliability.dlq`, `/v1/dlq/*`).
v0.0.6 (2026-11-21): the loop's stop branch writes to `POST /v1/dlq` (lead as payload,
workflow + execution id as source) instead of a sub-workflow you had to provide.
v0.0.7 (2026-11-21): the loop **claims a dedupe key before every push**, commits it on `done`,
releases it after a stop that certainly did not land and keeps it reserved after `unknown`,
so a re-run or a DLQ replay never re-sends a lead that is already in the campaign.
v0.0.8 (2026-11-21): [`n8n/dlq-replay.json`](n8n/dlq-replay.json), a scheduled replay of the
rows the store calls safe, fed back through the same loop, plus an alert while
`open_unsafe` > 0.
v0.0.9 (2026-11-22): `POST /v1/dlq/{id}/resolve` also resolves the row's dedupe key (same
scope + key) and reports `key_state`, so one read-back clears both the DLQ row and the
reservation the loop kept after `unknown`.

v0.0.10 (2026-11-23): **shared rate budget per vendor account scope** (`reliability.ratebudget`,
`/v1/rate/*`): several windows at once, a cost per call, `Retry-After` blocking the scope for
every caller, one SQLite file shared by every worker.

v0.0.11 (2026-11-24): **`retry-loop.json` spends the shared budget.** Every push, first
attempt and retries alike, asks `POST /v1/rate/acquire` first and switches on its `action`:
`go` → push, `wait` → Wait `wait_ms` and ask again (no attempt spent), `stop` (the wait is
past `rate_max_wait_ms`, 60 s) → a deferred DLQ row and a released key, the lead never sent.
A vendor 429 blocks the whole account scope: `/v1/decide` with `rate_scope` penalises the
budget until `Retry-After`, so a second execution on the same workspace waits too. The
offline replay now simulates time and runs two executions side by side on one budget.

v0.0.12 (2026-11-25): **error-workflow alerts that do not flood or leak**
(`reliability.alerts`, `POST /v1/alerts/n8n-error`, [`n8n/error-alert.json`](n8n/error-alert.json)):
one alert per failure fingerprint and window, repeats reported as "+N similar", e-mails
masked, trigger failures told apart from execution failures; the kit's workflows name it as
their error workflow and save failed executions so the alert's link opens.

v0.0.13 (2026-11-25): **watchdog for schedules that stop without an error**
(`reliability.watchdog`, `/v1/watchdog/*`, [`n8n/watchdog.json`](n8n/watchdog.json)): a dead
man's switch per scheduled workflow (period + grace), `dlq-replay.json` pings it at the end
of every run, and the same check alerts on key reservations a dead run left behind.

## Per-item retries

```python
from reliability import HttpResult, RetryPolicy, TransportError, run_items


def push(lead):  # your transport: HttpResult for any status, TransportError for no answer
    ...


report = run_items(
    leads,
    push,
    idempotent=False,
    key=lambda lead: lead["email"],
    policy=RetryPolicy(max_attempts=4, max_wait_seconds=300),
)
report.counts()  # {"ok": 97, "failed": 1, "exhausted": 0, "deferred": 1, "unknown": 1}
report.dlq_rows()  # one flat row per item that did not land, with safe_to_resend
```

Only the item that failed is sent again; an item that succeeded never is. One rule table,
`decide(result, attempt=, idempotent=, keyed=)`, returns `done` / `retry` (with `wait_ms`) /
`stop` (with an outcome), so an n8n loop can use it item by item: HTTP Request with
continue-on-fail → `decide` → Wait `wait_ms` → back into the same call for that item only.

| answer | idempotent call (GET / PUT / DELETE, upsert on a unique key, POST + honoured `Idempotency-Key`) | plain create (add-lead, CRM create, call create) |
|---|---|---|
| 2xx | `ok` | `ok` |
| 429 | retry: `Retry-After`, else backoff | retry: `Retry-After`, else backoff |
| 408 / 425 / 5xx | retry with backoff | **`unknown`** — may have landed; read back, never re-send blind |
| timeout / reset after sending | retry with backoff | **`unknown`** |
| DNS / refused (never left) | retry | retry |
| 409 with `Idempotency-Key` | retry (first request still in flight) | — |
| 422 with `Idempotency-Key` | `failed` (key reused with another payload) | — |
| other 4xx | `failed` | `failed` |
| `Retry-After` > `max_wait_seconds` | `deferred` + `retry_at` | `deferred` + `retry_at` |
| attempts used up | `exhausted` | `exhausted` |

Backoff is `base * factor ** (attempt - 1)` capped at `max_backoff_seconds`, with optional
jitter; `Retry-After` is read as delta-seconds or HTTP-date. A `Retry-After` longer than
`max_wait_seconds` is not slept inside the execution: the item ends `deferred` with
`retry_at`, for a DLQ table or an n8n Wait node.

Why these rules:
- RFC 9110 §9.2.2 idempotent methods; the same set (plus TRACE) is urllib3's default
  `allowed_methods`, and urllib3 also honours `Retry-After` before its own backoff.
- A POST becomes replay-safe only with an `Idempotency-Key` the vendor documents. Per
  draft-ietf-httpapi-idempotency-key-header, a second request while the first is still
  processing gets 409 and a reused key with a different payload gets 422.
- A 429 on a create is retried because the vendor refused it, which is the R9 advice
  (route 429 items back into the same call); a 5xx or a timeout on a create is not, because
  the lead may already be in the campaign.

## `POST /v1/decide` for an n8n loop

```bash
python -m pip install -e ".[api]"
RELIABILITY_API_TOKEN=... uvicorn reliability.api:app_from_env --factory   # token optional
```

After each attempt for **one** item, n8n posts what happened and branches on `action`:

```json
{"attempt": 1, "method": "POST", "statusCode": 429, "headers": {"retry-after": "7"},
 "request_headers": {}, "key": "ana@acme.example"}
```

→ `{"action": "retry", "wait_ms": 7000, "next_attempt": 2, "reason": "http_429", ...}`.
`done` carries `outcome: "ok"`; `stop` carries `outcome`, `safe_to_resend` and a flat
`dlq_row`; `deferred` adds `retry_at` (epoch) and `retry_at_iso` for a Wait-until or a DLQ row.

- `statusCode` / `status` + response `headers` come from HTTP Request with *Never Error* and
  the full response; with no HTTP answer send `error: {"code": "ECONNREFUSED"}` instead.
  Only DNS (`ENOTFOUND`, `EAI_AGAIN`), refused / unreachable and TLS-certificate codes mean
  "never left"; a timeout, a reset, an unknown or a missing code means "may have landed", so
  a plain create ends `unknown` instead of being re-sent. `reached_vendor` overrides the code.
- `request_headers` with an `Idempotency-Key` makes the call keyed (409 retry, 422 failed);
  `"idempotent": true` marks a POST upsert on a unique key. Otherwise the method decides.
- `policy` takes the `RetryPolicy` fields (unknown fields are a 422, not ignored).
- 200 for every decision, 422 for a body that cannot be decided (both or neither of
  `status` / `error`, bad policy), 401 without the bearer token when one is configured.

## n8n loop: `n8n/retry-loop.json`

A sub-workflow that pushes leads to a sequencer one at a time, claims each lead's key first
and asks `/v1/decide` after every attempt:

```
Leads in -> Collect leads -> Config -> One item per lead -> Loop over leads (batch 1)
  loop -> Claim body -> Claim key (POST /v1/keys/claim) -> Claim + lead -> Route by claim
            committed / in_flight -> Skip (logged) -> Next lead
            stale / other         -> Alert: claim needs review -> Next lead
            reserved -> First attempt (+ token)
  -> Attempt -> Push lead (Never Error, full response, no Retry On Fail)
            HTTP answer  -> Decide body: statusCode + headers ┐
            no answer    -> Decide body: error.code          ┴-> Decide (POST /v1/decide)
          -> Decision + lead -> Route by action
               done  -> Commit key (POST /v1/keys/commit) -> Next lead -> Loop
               retry -> Wait (wait_ms / 1000 s) -> Next attempt -> Attempt   (same lead only)
               stop  -> DLQ intake body -> DLQ (POST /v1/dlq) -> Key after stop
                          unknown                     -> Keep reservation -> Next lead
                          failed / exhausted / deferred -> Release key (POST /v1/keys/release) -> Next lead
               other -> Alert -> Next lead
  done -> All leads handled
```

- **Batch size 1** is load-bearing: n8n's Wait node reads its amount from item 0 only
  (`getNodeParameter('amount', 0)` in n8n-nodes-base 2.48.4), and one lead per pass keeps a
  retry from re-sending any other lead.
- `attempt` and `lead` are re-read from the `Attempt` node, never from the vendor's answer,
  so a lead survives an error item that carries nothing but `{error}`.
- `Config.idempotent` is `false` for add-lead: a 5xx or a dropped connection after sending
  ends `unknown` (DLQ, `safe_to_resend: false`) and is never pushed again. A
  `Retry-After` above `max_wait_seconds` ends `deferred` with `retry_at_iso` instead of
  holding the execution open.
- The decide call uses a Header Auth credential (`Authorization: Bearer <token>`) and has
  no continue-on-error: if the service is down the run fails loudly for the error workflow.
- A `stop` is stored with `POST /v1/dlq`: `scope` from `Config.scope` (`vendor:campaign`),
  `row` = the decision's `dlq_row`, `payload` = the lead (what replay hands back) and
  `source` = `{workflow: $workflow.name, node, execution_id: $execution.id}` (n8n built-ins:
  `$workflow` in n8n-workflow 2.16.0 `workflow-data-proxy.js`, `$execution.id` in n8n-core
  2.16.1 `get-additional-keys.js`). Same credential, no continue-on-error: a failed write
  fails the run instead of losing the row.
- **Dedupe key per person and target**: `claim {scope, key: email}` before the first
  attempt; the token rides along every attempt (`Attempt` → `Decision + lead` →
  `Next attempt`) so only this execution can commit or release it. `committed` (pushed by an
  earlier run) and `in_flight` (another execution holds it) are skipped and logged, never an
  error that stops the batch; `stale` (reserved ≥ `RELIABILITY_STALE_AFTER` ago, never
  finished) goes to a review alert and is not pushed until someone reads the vendor back and
  calls `POST /v1/keys/resolve`.
- The key is committed only after a 2xx, released only after `failed` / `exhausted` /
  `deferred` (read from the outcome the DLQ stored), and kept after `unknown`. A replayed DLQ
  payload re-enters the same loop, so its claim is what stops a lead that landed in a later
  run from being pushed twice. A 409 on commit / release (another token) fails the run: it
  means two executions touched one key.

`tests/test_n8n.py` replays [`examples/demo-loop.json`](examples/demo-loop.json) (8
synthetic `.example` leads: 201, 429 + `Retry-After: 7`, 502, refused then 200, socket hang
up, `Retry-After: 900`, 400, 429 × 4) through the workflow's own connections: Set
expressions are resolved by a small evaluator, the push goes to a scripted vendor, decide
goes to the FastAPI app. It checks every lead ends exactly once, push counts and waits per
lead, and the DLQ rows the service stored (`unknown` ×2, `deferred`, `failed`, `exhausted`,
each with the lead as payload and the execution as source), and that a dry-run replay right
after the run hands back only the `exhausted` lead (`unknown` refused, `deferred` not due).
Further runs on the same service check the keys: run 2 pushes only the three released leads
(the landed ones are `committed`, the `unknown` ones `in_flight`); 901 s later the `unknown`
ones are `stale` and alert instead of being pushed; a DLQ replay of a lead that landed in run 2
is skipped by its claim.

## Dedupe keys: claim → commit only after the call landed

n8n's Remove Duplicates ("Remove Items Processed in Previous Executions") records a key
**when the node runs** (`checkProcessedAndRecord` in n8n-nodes-base 2.48.4), before the push
after it: if the push fails, the lead is already "seen" and the next run drops it. It also
throws once stored keys + batch size exceed `historySize`. `KeyStore` splits the mark:

| call | when | result |
|---|---|---|
| `claim(scope, key)` | before the push | `reserved` + `token` (go) · `committed` (skip: done) · `in_flight` (another execution holds it) · `stale` (reserved ≥ `stale_after` ago, never finished: may have landed) |
| `commit(scope, key, token)` | push / decide ended `ok` | later claims return `committed`; repeating is a no-op |
| `release(scope, key, token)` | it certainly did not land (`failed`, `exhausted`, `deferred`) | the next run can claim it again |
| `resolve(scope, key, landed=)` | after a read-back of a `stale` key | landed → `committed`, not landed → released; refused while younger than `stale_after` unless `force` |
| `pending(scope=, older_than=)` | watchdog | open reservations, oldest first, with `stale` |

- A `stale` reservation is never handed to a new caller: a push that ended `unknown` keeps
  its reservation, so the lead is not re-sent until someone reads the vendor back.
- The token stops one execution from committing or releasing another one's reservation.
- Scopes keep keys apart per target (`smartlead:campaign-42`), so "already in campaign A"
  never blocks campaign B; global suppression stays a separate list.
- SQLite (WAL, `INSERT OR IGNORE`): concurrent claims from separate connections reserve a
  key exactly once (tested with 8 threads over 4 connections on one file).

Over HTTP (same bearer token as `/v1/decide`): `POST /v1/keys/claim`
`{scope, key, stale_after_seconds?}` → `{status, go, token, since, age_seconds}`;
`POST /v1/keys/commit` and `/v1/keys/release` `{scope, key, token}` (409 another token or
already committed, 404 no reservation); `POST /v1/keys/resolve` `{scope, key, landed,
force?}`; `GET /v1/keys/pending?scope=&older_than=`. `RELIABILITY_DB` sets the SQLite path
(default in-memory, lost on restart), `RELIABILITY_STALE_AFTER` the seconds (default 900).

## Dead-letter rows: one per key, replay only what is safe

Every `stop` from `/v1/decide` carries a flat `dlq_row`; `POST /v1/dlq` stores it with the
item's payload and where it came from, so the lead that failed is named in one table instead
of an error-workflow alert that carries no items.

```json
{"scope": "smartlead:campaign-42", "row": {"key": "ana@acme.example", "outcome": "unknown", ...},
 "payload": {"email": "ana@acme.example", "first_line": "..."},
 "source": {"workflow": "push", "node": "Push lead", "execution_id": "77"}}
```

| outcome | `POST /v1/dlq/replay` | what a person does |
|---|---|---|
| `exhausted` | handed back with its payload | nothing; replay re-enters the loop at attempt 1 |
| `deferred` | handed back once `retry_at` has passed, else `not_due` | nothing |
| `unknown` | **refused** (`unsafe_to_resend`): may have landed | read the vendor back → `POST /v1/dlq/{id}/resolve {landed}`: landed → `resolved`; not landed → becomes `exhausted` (replayable); the key follows (below) |
| `failed` | **refused**: the vendor rejected the data | fix the data and push it again, or `POST /v1/dlq/{id}/discard` |

- **One row per `(scope, key)`.** A repeat failure of the same lead (including after a
  replay) updates the open row: `count` + 1, latest outcome / reason, `first_seen_at` kept.
  A resolved or discarded key that fails again is a new row.
- `safe_to_resend` is recomputed from the outcome on intake; the caller's value is ignored.
- A replayed row moves to `replayed`, so a second replay call never hands it out twice;
  `replays` counts redrives and `max_replays` (default 3) stops a poison item with
  `replay_limit`. `dry_run: true` returns the same verdicts and changes nothing.
- `GET /v1/dlq?state=open|replayed|resolved|discarded|all&scope=&outcome=` lists rows oldest
  first; `GET /v1/dlq/summary` gives open rows by outcome, `open_unsafe` (unknown + failed)
  and the oldest open age for an alert.
- Like an SQS redrive, a replayed item is a new attempt sequence; unlike SQS, which redrives
  every message, the store never hands back an item that may already have landed.
- **Resolve carries over to the key.** `POST /v1/dlq/{id}/resolve` also calls
  `KeyStore.resolve(scope, key, landed)` and returns `key_state`: `committed` (landed: never
  pushed again), `released` (not landed: the replay's claim reserves it), `not_reserved`
  (the loop already released it, or the workflow does not claim keys), `in_flight` (the row
  was `replayed` and the replay execution holds a fresh reservation: left to its holder).
  For an `open` row the key is resolved even while younger than `stale_after`: the loop that
  wrote the row has finished. A refused resolve (404 / 409 / 422) leaves the key alone.
  Unlike AWS Powertools idempotency, which lets an `INPROGRESS` record be re-run once its
  in-progress expiry passes, a stale reservation here is never re-run without a read-back.
- SQLite (`RELIABILITY_DLQ_DB`, default `RELIABILITY_DB`), payloads capped at 64 KB.

## n8n replay: `n8n/dlq-replay.json`

```
Every 30 minutes -> Config (reliability_url, scope = retry-loop's scope)
  -> Replay due rows (POST /v1/dlq/replay {scope}) -> One item per replayed row (Split Out)
       -> Lead from payload (Set, raw JSON) -> Push again (Execute Workflow: retry-loop, once, waited for)
  -> DLQ summary (GET /v1/dlq/summary?scope=) -> Needs a person? (open_unsafe > 0)
       true -> Alert: DLQ needs a read-back
```

- Only `exhausted` rows and `deferred` rows past `retry_at` come back, once each (they move
  to `replayed`); `unknown` / `failed` are refused and keep the alert firing until someone
  resolves (read-back) or discards (bad data) them.
- The payload re-enters `retry-loop.json`, so each lead claims its key again: a lead that
  landed in a later run is skipped as `committed`, a lead that fails again bumps the same DLQ
  row (`count` + 1, `replays` towards `max_replays`).
- An empty `replay` list gives Split Out no items, so the sub-workflow does not run on a quiet
  tick (n8n-nodes-base 2.48.4 `SplitOut`: a missing / empty field yields nothing; with
  `noOtherFields` an object element is spread into the item).

The replay test runs a tick against the same service after the demo: 901 s later it pushes
`fay` (deferred, now due) and `hal` (exhausted) once each, refuses `cara`, `eli` (unknown) and
`gus` (failed) and alerts with `open_unsafe: 3`; a second tick hands out nothing; once the
unknown rows are resolved and the failed one discarded, the tick is quiet.

## Rate budget: one per account, shared by every execution

n8n paces per execution (HTTP Request Batching, Wait), so two workflows on the same
Instantly workspace, or five queue-mode workers, each spend the whole limit. Most vendors
count the **account**, not the key: Instantly the workspace across keys and API versions,
Smartlead the admin-key pool, Attio the workspace, Pipedrive the company (research
[R9 2026-11-22](../../research/notes/2026-11-22-R9-vendor-rate-limits-table.md)).

```python
from reliability import RateBudget

rates = RateBudget("/data/rate.db")  # every worker opens the same file
rates.configure("apollo:team-1", "apollo_basic")  # 200 / min, 400 / h, 2,000 / day
rates.configure("pipedrive:co-7", [(86400, 30000)])  # tokens per day, set from your plan

g = rates.acquire("apollo:team-1")  # never sleeps
g.granted, g.wait_ms, g.blocked_by  # False, 3480000, "3600s": the hour window is full
rates.acquire("pipedrive:co-7", cost=40)  # a search costs more than a list
rates.penalize("apollo:team-1", 120)  # the vendor said 429 + retry-after: 120
```

- **All windows must pass.** A single rate passes the minute check and then hits Apollo's
  daily wall at noon; here the answer names the window that blocks (`60s`, `3600s`,
  `86400s`) and how long until enough old calls leave it.
- **Sliding log, at least as strict as the vendor's fixed windows:** at most `limit` cost
  units in any interval of that length, so a fixed-window vendor never sees more.
- **`Retry-After` overrides the windows** for the whole scope: the vendor's count includes
  callers this budget never saw. No header → the shortest window's length. A shorter hint
  never shortens a longer block.
- **Fails closed:** a scope that was never configured is a 404, not "unlimited"; a cost
  above a window's limit is a 422 (waiting would never help).
- **Cross-process:** each acquire runs in `BEGIN IMMEDIATE` on the shared file, so two
  workers cannot both take the last slot (tested with two processes: 40 + 40 tries, 50 granted).
- **Presets** (`GET /v1/rate/presets`): `instantly`, `smartlead_admin_pool`,
  `smartlead_client_key`, `apollo_basic` / `_professional` / `_free`, `hubspot_app`,
  `attio_reads` / `_writes`, `heyreach`, each with its scope, source and confidence. Every
  number is a web-search summary of the vendor's page; none of those pages could be opened
  from the run environment. Pipedrive has no preset: its token budget depends on the plan.

In n8n (`n8n/retry-loop.json` does exactly this): before each vendor call
`POST /v1/rate/acquire {scope, cost, max_wait_ms, key, attempt}` → Switch on `action`:
`go` → call; `wait` → Wait `wait_ms`, ask again; `stop` → the answer is a deferred stop with
the same fields as a `/v1/decide` stop (`outcome`, `retry_at`, `dlq_row` with `attempts` =
calls made), so it takes the loop's DLQ + key-release path. Without `max_wait_ms` the answer
is never `stop`. After a 429, send `rate_scope` with the attempt to `/v1/decide` (the answer
carries `rate_blocked_until`), or call `POST /v1/rate/penalty {scope, headers:
$json.headers}` yourself (the full-response headers; lists and lowercase names are read).

Config of the loop: `rate_scope` is the vendor **account** (`sequencer:workspace-demo`), not
the campaign `scope` the keys and DLQ rows use; every workflow pushing into that account must
use the same name, configured once with `POST /v1/rate/scopes` (an unconfigured scope is a
404 and the run fails before any push). `rate_max_wait_ms` defaults to 60,000: n8n keeps a
Wait under 65 s in the running process and offloads longer ones to the database (docs page
via a search summary; the Wait node's source always calls `putExecutionToWait`, so the
threshold lives in n8n core), and a budget wait of hours (a daily cap) belongs in the DLQ,
not in a parked execution.

The replay (`tests/test_n8n.py`) simulates time: a Wait parks its execution until the fake
clock reaches its end and the execution that is ready first runs next (round robin on ties).
Two executions with a 2-per-second budget push 10 leads with never more than 2 calls in any
second; a 429 with `Retry-After: 30` in one holds the other's next push back 30 s.

## Error-workflow alerts: `n8n/error-alert.json`

n8n runs a workflow's error workflow only when an execution or its trigger failed, and the
Error Trigger gets one item with no lead in it (`packages/cli/src/execution-lifecycle/
execute-error-workflow.ts`):

```json
{"execution": {"id": "231", "url": ".../workflow/7/executions/231", "error": {"message": "..."},
               "lastNodeExecuted": "Push to sequencer", "mode": "trigger", "retryOf": null},
 "workflow": {"id": "7", "name": "09 · retry loop"}}
```

or `{"trigger": {"error", "mode"}, "workflow"}` when the trigger itself failed (no execution
exists, so there is no link to open). Posting that straight into a chat has three problems
the service handles:

| problem | what `POST /v1/alerts/n8n-error` does |
|---|---|
| a schedule failing every 5 min posts 288 alerts a day | fingerprint = workflow + kind + node + HTTP code + message with ids / numbers / quoted values masked; first per window (`RELIABILITY_ALERT_WINDOW`, 900 s) is `send: true`, repeats are counted and reported on the next sent alert ("+5 similar not sent since …") |
| vendor errors echo the lead | e-mail addresses masked, message cut to 300 characters |
| the link opens nothing | trigger failures say so; the kit's workflows set `saveDataErrorExecution: all` (n8n fires the error workflow even when the failed execution is not saved) |

```text
Error Trigger -> Config -> POST /v1/alerts/n8n-error -> send? -> Alert (webhook / Telegram / Slack)
                                      \-- error output (service down) -> fallback alert:
                                           workflow name + execution URL, no error text
```

The route always answers 200, also for a payload it does not recognise (sent as
`unrecognised`, never folded): an error workflow that fails on its own input alerts nobody.
`retry-loop.json` and `dlq-replay.json` name it in Settings → Error workflow
(`REPLACE_error_alert_workflow_id`); the error workflow names none, so it never alerts on
itself. Manual runs (the Error Trigger's sample data) are tagged `[test]` and never fold
into live alerts. Per-lead failures stay in the DLQ; the alert points there.

## Watchdog: schedules that stopped without an error

The error workflow covers runs that **failed**. It says nothing when a schedule stops
running: the workflow was deactivated, an import or restart lost its trigger, a worker died
mid-run. That is the failure nobody notices for days. The watchdog is a dead man's switch,
with the period / grace model cron monitors use (Healthchecks: *period* = expected time
between pings, *grace* = extra wait before alerting):

```bash
curl -X PUT  $SVC/v1/watchdog/checks/dlq-replay -d '{"period_seconds": 1800, "grace_seconds": 600}'
curl -X POST $SVC/v1/watchdog/checks/dlq-replay/ping -d '{"signal": "ok", "execution": "123"}'
curl -X POST $SVC/v1/watchdog/check     # from n8n/watchdog.json every 10 minutes
```

| state | when |
|---|---|
| `new` | configured, no finished run yet, inside period + grace |
| `up` / `late` | last `ok` ping within period / within period + grace (no alert yet) |
| `down` | no `ok` ping for period + grace — **also when it never ran once** (counted from configuration); or a `start` ping with no `ok` within grace (hung / crashed mid-run) |
| `paused` | a person paused it for a known problem |

`POST /v1/watchdog/check` alerts `down` once per episode, `still_down` every 6 h
(`RELIABILITY_WATCHDOG_REALERT`) and `recovered` once; several watchdog runs on one SQLite
file alert once between them. The same call lists key reservations older than
`stale_after` (a run died between push and commit) per scope — counts only, no lead keys —
and alerts on each scope once per alert window.

`dlq-replay.json` pings as its last step on both IF outputs; its ping node continues on
error, so a ping failure never fails the run (the missing ping is what the watchdog reports).
A ping for a name never configured is a 404, not a new watch. `watchdog.json` itself has no
continue-on-error: if the service is down, the run fails and the error workflow alerts.

## Run it

```bash
python -m pip install -e ".[dev]"
python -m pytest -q
```

## Limits

- The rate budget has no queue: a refused caller asks again after `wait_ms`, so callers are
  not served in arrival order and a busy scope can starve one of them. It counts only calls
  that went through `acquire`; anything else on the same account is seen only through a
  429's `Retry-After`. Vendor limits in the presets are search summaries. A 429 penalises the
  whole scope, including a per-lead 429 a vendor might send for some other reason. Two
  executions were replayed offline on one simulated clock, never on a live n8n. `POST /v1/dlq/replay` does not consult the key store (the loop's claim does).
  The DLQ and the key store may be two SQLite files: a crash between the row update and the
  key resolve leaves the key reserved (call `POST /v1/keys/resolve`); a stale key with no
  DLQ row (the run died before intake) still needs `POST /v1/keys/resolve`. The key store is one SQLite file: several n8n workers share it only
  through one service instance (no Postgres / Redis backend). `run_items` waits in-process; for
  long waits use `deferred`.
- Releasing a `deferred` key lets the next scheduled run push that lead before `retry_at`
  (it will most likely be deferred again and bump the same DLQ row); a run that dies between
  push and commit leaves a reservation that turns `stale`, which is the intended "read back
  first" state, not a re-send.
- `retry-loop.json` was never imported into a live n8n; node parameters were checked
  against the n8n-nodes-base 2.48.4 package source (Wait, SplitInBatches v3, Aggregate,
  SplitOut, Switch v3, HTTP Request v3), and the replay's evaluator covers only the
  expression forms the workflow uses.
- Nothing watches the watchdog: if n8n itself is down, neither `watchdog.json` nor the error
  workflow runs. Ping an external cron monitor from `watchdog.json` for that. Watches are
  interval-based only (no cron expressions, no time zones), and only `dlq-replay.json`
  pings out of the box; `retry-loop.json` is a sub-workflow and is covered by its caller.
  `watchdog.json` was never imported into a live n8n.
- `error-alert.json` was never imported into a live n8n and never received a real Error
  Trigger item; the payload shape comes from n8n's `execute-error-workflow.ts` and the
  Error Trigger node on GitHub (master, 2026-11-25). Whether `error.node` / `error.httpCode`
  survive serialisation into that item was not checked, so the node falls back to
  `lastNodeExecuted`. Masking covers e-mail addresses only, not names or phone numbers.
- The n8n error-item shape (where the Node.js error `code` sits on a continue-on-error item)
  was not checked against a live n8n: map it into `error.code` explicitly.
- Offline tests with a scripted fake vendor; never run against a live vendor or a live n8n.
- Which vendors honour `Idempotency-Key`, and whether a vendor's 429 can follow a processed
  request, is per vendor and not checked here; the caller decides `idempotent` / `keyed`.
- DLQ redrive semantics compared with the Amazon SQS developer guide source on GitHub
  (redrive = new message, alarm on arrivals, retention measured from the original enqueue);
  docs.n8n.io and docs.aws.amazon.com did not resolve from the run environment (2026-11-20).
- RFC 9110, the IETF draft page and the AWS backoff article did not resolve from the run
  environment (2026-11-18); the rules come from the draft's source on GitHub and urllib3's
  `retry.py`.
