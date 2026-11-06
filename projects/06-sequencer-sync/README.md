# 06 · Sequencer sync (Smartlead / Instantly / lemlist)

**The pain:** leads get pushed twice, unsubscribed people get contacted again, and nobody
can say how many of the 400 leads sent to a campaign were actually added. In real n8n
builds the campaign id was hard-coded and a missing campaign silently fell back to another
one; "this email exists somewhere in the sequencer" blocked a valid new campaign because
campaign dedupe and global suppression were the same switch; and every sequencer defaults
to "insert", with its cross-campaign check named differently (Smartlead `ignore_*`
settings, Instantly `skip_if_*`, lemlist `deduplicate`; research R4, 2026-10-25).

v0.1.0: the planning layer that runs before any HTTP call, request builders for
Smartlead, Instantly and lemlist that map one dedupe scope onto each vendor's own flags,
a push executor with a retry rule that cannot create a lead twice, a read-back
reconciliation that reports *sent* vs *added* vs *skipped by the vendor*, a stdlib HTTP
transport, a SQLite push log, and a FastAPI service with an n8n workflow and an offline
demo (fake Smartlead) on top.

## Architecture

```
SequencerLead(lead_id, email, names, custom_variables)   email = person key, placeholders → None
   │
resolve_campaign("Q4 Logistics DE", listed campaigns)    exact name; missing / duplicate → error
   │
plan_push(vendor, campaign, leads, dedupe_scope=…)       dedupe_scope has no default
   │   1 missing_email         no usable address
   │   2 fixture               reserved test domain (.example …) unless allow_fixtures
   │   3 suppressed            SuppressionList: addresses + domains (with subdomains)
   │   4 duplicate_in_batch    same email earlier in this batch
   │   5 already_pushed        push log: committed (vendor, campaign, email) key
   │   6 push_pending          push log: reserved, earlier outcome unknown → reconcile
   │   7 already_in_campaign   membership the caller read from the vendor
   │   8 in_other_campaign     only when dedupe_scope ≥ all_campaigns
   │   9 in_workspace          only when dedupe_scope = workspace
   │  10 missing_variables     a required merge tag is blank → never send the template
   ▼
PushPlan(to_send=[PlannedLead(lead, idempotency_key)], skipped=[Skipped(reason, detail)])
```

```
execute_plan(plan, adapter, transport, push_log)         reserve keys → batches → commit / release
   │   2xx            accepted          keys committed
   │   429            retried           Retry-After (seconds or HTTP-date), capped; then rate_limited
   │   4xx            rejected          keys released, error text kept
   │   5xx / timeout  outcome_unknown   NOT retried; keys stay reserved
   ▼
reconcile(report, push_log, members_after, members_before)
       outcome_unknown present → committed, absent → released (safe next run)
       added = new in campaign; skipped_by_vendor = accepted but absent
```

| scope | Smartlead `settings` | Instantly `/leads/add` | lemlist |
|---|---|---|---|
| `campaign` | `ignore_duplicate_leads_in_other_campaign: true` | `skip_if_in_campaign: true`, `skip_if_in_workspace: false` | `deduplicate=false` |
| `all_campaigns` | `…_in_other_campaign: false` | `skip_if_in_workspace: true` (no narrower flag → effective `workspace`) | `deduplicate=true` |
| `workspace` | `…_in_other_campaign: false` (no list scope → effective `all_campaigns`; the lab's own `in_workspace` check still applies) | `skip_if_in_workspace: true` | `deduplicate=true` (effective `all_campaigns`) |

Smartlead also always sends `ignore_global_block_list`, `ignore_unsubscribe_list` and
`ignore_community_bounce_list` as `false` (block lists stay on). lemlist pushes always send
`findEmail`, `findPhone`, `verifyEmail`, `linkedinEnrichment` as `false`, so a push never
starts paid lookups; a custom variable that would shadow a lemlist field is refused. The
report carries `requested_scope` and `effective_scope`, and URLs with the Smartlead
`api_key` query parameter are masked.

- **Dedupe scope** (`campaign`, `all_campaigns`, `workspace`) is a required argument;
  vendor adapters map it to each tool's own flag instead of trusting the tool's default.
- **Suppression is not dedupe.** Unsubscribed / bounced / complained / do-not-contact /
  customer entries block every campaign; no `dedupe_scope` turns them off.
- **Person key, never company.** One email = one lead; two people at one company never
  collide.
- **Idempotency key** = sha256(vendor, campaign id, email). A key is *reserved* before a
  push and *committed* once the vendor confirmed it; a reserved key on the next run means
  "outcome unknown" and the lead waits for reconciliation instead of being pushed again.

### HTTP transport (`UrllibTransport`, standard library only)

```
UrllibTransport(timeout=30).send(HttpRequest)  →  HttpResponse(status, body, headers)
   2xx / 4xx / 5xx        a response (HTTPError unwrapped: 429 Retry-After still read)
   3xx                    returned, never followed → push: outcome_unknown, read: IncompleteRead
   DNS / refused / TLS    TransportError(reached_vendor=False) → not_sent, keys released
   timeout / reset        TransportError(reached_vendor=True)  → outcome_unknown, keys kept
```

Redirects are off on purpose: `urllib` re-sends a redirected POST (301 / 302 / 303) as a
body-less GET, which can return 200 while no lead was created (CPython
`Lib/urllib/request.py`, `redirect_request`, read 2026-11-04). A connect timeout counts as
"may have reached the vendor" because `urllib` reports connect and send errors the same
way; reconcile settles it. Error texts carry the redacted URL only. Proxy settings come
from the environment (`HTTPS_PROXY`); bodies over 10 MB raise.

### Persistent push log (`SqlitePushLog`)

```python
with SqlitePushLog("push-log.sqlite") as log:  # one file, WAL, autocommit
    plan = plan_push(..., push_log=log)  # reserved key → push_pending
    report = execute_plan(plan, adapter, UrllibTransport(), log)
    log.pending(older_than=3600)  # outcome_unknown > 1 h → alert
```

`reserve` is a single `INSERT OR IGNORE` on the key, so two runs with their own
connections can never both win one lead (tested with 8 racing writers); a crash after a
5xx leaves the keys reserved on disk and the next run skips those leads as `push_pending`
until `reconcile` settles them. Only sha256 keys are stored, no emails or profile URLs.
Tested on SQLite 3.45 (Python's bundled build); a Postgres store with the same contract is
not built.

### Member read-back (feeds `plan_push(in_campaign=…)` and `reconcile`)

`reconcile` releases every `outcome_unknown` key whose lead is missing from the read, so a
truncated member list would let the next run push leads that already exist.
`read_members(reader, campaign, transport)` reads every page or raises `IncompleteRead`:

| reader | request | pages | guard |
|---|---|---|---|
| `SmartleadMembers` | `GET /campaigns/{id}/leads?offset&limit=100` | offset until a short page or `total_leads` | bare-list body also accepted |
| `InstantlyMembers` | `POST /leads/list {campaign, limit: 100, starting_after}` | `next_starting_after` until an empty page | items naming another `campaign` are dropped (`dropped` count) |
| HeyReach | `HeyReachAdapter.campaign_leads(campaign, offset=)` → `GetLeadsFromCampaign` | caller pages | response shape unknown; caller extracts profile URLs |

Reads are idempotent, so 429 (Retry-After), 5xx and timeouts are retried there (never for
a push); a 4xx, an odd body, a repeated cursor or more than `max_pages` raise instead of
returning a partial set. lemlist read-back is not built yet.

### LinkedIn (HeyReach): person key, sender per lead, pre-check

LinkedIn tools key a person by profile URL, and HeyReach's `AddLeadsToCampaign` makes the
caller pair every lead with a sending account and has no dedupe flag (research R5).
`sequencer.linkedin` makes those choices explicit:

```
normalise_linkedin_url(url)            https://www.linkedin.com/in/<slug>; company / Sales Nav → None
   │
assign_senders(leads, senders, owners=, in_campaign=)
   │   no_profile_url / duplicate_profile / already_in_campaign (GetCampaignsForLead read)
   │   pinned owner → only that sender: unknown_owner / owner_over_budget, never reassigned
   │   otherwise the active sender with most weekly budget left (ties: lowest id)
   │   no_sender_budget → held, never pushed over a cap
   ▼
HeyReachAdapter.add_leads(campaign, pairs)   ≤ 100 pairs per call, linkedInAccountId per lead
HeyReachAdapter.campaigns_for_lead(url)      membership pre-check (paged, limit 100)
```

Weekly budgets are your own caps per sender (LinkedIn publishes none); an inactive
(logged-out) sender gets nothing.

`plan_linkedin_push` runs the same pipeline as `plan_push` with the profile URL as the
person key, so `execute_plan` and `reconcile` work unchanged for HeyReach:

```
plan_linkedin_push(campaign, leads, senders, dedupe_scope=…, memberships={url: [campaign ids]})
   │   no_profile_url → fixture → suppressed (profile, email or domain) → duplicate_in_batch
   │   → already_pushed / push_pending (key = sha256(vendor, campaign, profile URL))
   │   → membership_unchecked (no GetCampaignsForLead read: HeyReach has no dedupe flag,
   │     so an unread person is held, never pushed blind) → already_in_campaign
   │   → in_other_campaign (scope ≥ all_campaigns) → missing_variables
   │   → assign_senders on the survivors only (skipped leads never spend a sender's budget)
   ▼
PushPlan(person_key="linkedin_url", to_send=[PlannedLead(…, person_key=url, sender_id=…)])
execute_plan(plan, HeyReachAdapter(key), transport, log)   linkedInAccountId per lead
reconcile(report, log, members_after=[profile URLs in any form])
```

**Public API (`HeyReachAdapter.public(key)`).** Same pairs, sent to
`api/public/campaign/AddLeadsToCampaignV2` with `X-API-KEY` and `resumeFinishedCampaign` /
`resumePausedCampaign` set to `false` (a push never restarts a stopped campaign). V2 answers
only `{addedLeadsCount, updatedLeadsCount, failedLeadsCount}`, so a 2xx batch is accepted
only when `failed == 0` and `added + updated` equals the leads sent (`updated` = already in
the campaign). Any failed count, a short or excess total, or a body without counts makes the
batch `outcome_unknown`: keys stay reserved and `reconcile()` settles which leads landed —
the answer never says which one failed. Counts land in `batches[].detail`.

`SuppressionList.add_profile()` blocks a person by LinkedIn profile, for rows that carry
no email. HeyReach lead lists are not campaigns, so `workspace` is enforced as
`all_campaigns` (`effective_scope` says so in the report).

## Service and n8n workflow

```
POST /v1/push       {vendor, campaign_name, dedupe_scope, leads, required_variables}
   campaign by exact name (missing 404, duplicate name 409) → member read (partial → 502,
   nothing pushed) → plan_push → execute_plan → one outcome per lead:
   sent | skipped (reason) | outcome_unknown | failed
POST /v1/reconcile  {vendor, campaign_name, leads: the outcome_unknown ones}
   member read → found: committed (confirmed) / absent: released (cleared) / no reserved
   key: not_pending. Keys are recomputed from (vendor, campaign id, email): no server-side
   report has to survive between the calls.
GET  /healthz
```

`create_app(service, api_token=...)` requires `Authorization: Bearer <token>` on `/v1`.
lemlist has no member reader yet: its pushes run without a membership read
(`membership_checked: false`) and `/v1/reconcile` answers 501.

[`n8n/push-and-reconcile.json`](n8n/push-and-reconcile.json):

```
Webhook (ready rows only) → Config (vendor, campaign_name, dedupe_scope — no ids)
  → Push batch (no n8n retry) ─┬→ Respond: summary
                               ├→ One item per lead → Route by outcome
                               │     sent → log · skipped → skip + log (never Stop and Error)
                               │     failed / anything new → DLQ
                               │     outcome_unknown → Wait 60 s → Collect → Reconcile
                               │        (retried: reads only) → log / alert (keys stay reserved)
                               └→ (error output) Respond 502: nothing sent
```

The workflow is structure-tested against the API models (`tests/test_n8n.py`); it was
not imported into a live n8n instance.

## Run it

```bash
python -m pip install -e ".[dev]"
python -m pytest -q
uvicorn sequencer.demo:app --port 8006      # offline demo: fake Smartlead, in-memory log
curl -s localhost:8006/v1/push -H 'content-type: application/json' \
     -d @examples/demo-push.json
```

The demo batch has 7 leads: 2 sent, 1 duplicate, 2 suppressed (unsubscribed address,
do-not-contact domain), 1 with a blank required `first_line`, 1 placeholder email; posting
it again skips all as `already_pushed`. Emails starting `lost+` / `down+` trigger a lost
response / a 503 to show the `outcome_unknown` → reconcile path.

```python
from sequencer import Campaign, DedupeScope, SequencerLead, SuppressionList, plan_push

campaign = Campaign(campaign_id="101", name="Q4 Logistics DE")
plan = plan_push(
    "smartlead",
    campaign,
    [SequencerLead(lead_id="l1", email="ana@northwind.example")],
    dedupe_scope=DedupeScope.ALL_CAMPAIGNS,
    suppression=SuppressionList(),
    allow_fixtures=True,
)
plan.summary()
```

## Limits
- Request shapes are taken from vendor-published packages read on 2026-11-01 and
  2026-10-25 (`@smartlead/cli` 0.1.0, `instantly-cli` 0.1.22, n8n's built-in lemlist
  node) — not from the vendors' API reference pages, which were unreachable from the run
  environment. Smartlead's `settings` flag names and its 400-lead and Instantly's
  1,000-lead batch limits come from web-search summaries (research R4). Nothing here was
  run against a live Smartlead, Instantly or lemlist account; tests use `FakeTransport`,
  the demo's `FakeSmartlead` and a local `http.server`.
- Whether a vendor itself refuses a lead already in the *same* campaign is not verified;
  the push log and `in_campaign` keep the lab from re-pushing either way.
- Vendor response bodies are parsed for counts only for HeyReach V2, and only to *refuse*
  confirmation (failed / missing counts → `outcome_unknown`); `added` / `skipped_by_vendor`
  still come only from `reconcile()` with a member read-back.
- HeyReach public-API shapes (`api/public`, `X-API-KEY`, `AddLeadsToCampaignV2`, its three
  counts, the resume flags) come from the third-party `heyreach-cli` 0.2.2, read 2026-11-06
  (research R5); HeyReach's own docs were unreachable. That `updatedLeadsCount` means
  "already in the campaign" is read from the field name. Not run live.
- HeyReach shapes (`AddLeadsToCampaign`, `GetCampaignsForLead`, `X-N8N-KEY`, 100 per
  page) come from HeyReach's n8n package `n8n-nodes-heyreach` 1.0.5, read 2026-11-02; the
  100-pair batch size for adds is the lab's own choice (no limit found). Not run live.
- `UrllibTransport` is tested against a local `http.server` on 127.0.0.1 only; it has
  never called a vendor host. An n8n workflow and an HTTP API come next
  (see [ROADMAP](../../ROADMAP.md)).
- `InMemoryPushLog` is per process; use `SqlitePushLog` for real runs (one host; several
  hosts need a shared database store, not built).
- Membership sets (`in_campaign`, …) are only as fresh as the caller's last read; the
  vendor's own flag must stay on as the second line of defence.
