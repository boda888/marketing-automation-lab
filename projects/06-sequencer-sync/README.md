# 06 · Sequencer sync (Smartlead / Instantly / lemlist)

**The pain:** leads get pushed twice, unsubscribed people get contacted again, and nobody
can say how many of the 400 leads sent to a campaign were actually added. In real n8n
builds the campaign id was hard-coded and a missing campaign silently fell back to another
one; "this email exists somewhere in the sequencer" blocked a valid new campaign because
campaign dedupe and global suppression were the same switch; and every sequencer defaults
to "insert", with its cross-campaign check named differently (Smartlead `ignore_*`
settings, Instantly `skip_if_*`, lemlist `deduplicate`; research R4, 2026-10-25).

v0.0.3: the planning layer that runs before any HTTP call, request builders for
Smartlead, Instantly and lemlist that map one dedupe scope onto each vendor's own flags,
and a push executor with a retry rule that cannot create a lead twice, plus a
read-back reconciliation that reports *sent* vs *added* vs *skipped by the vendor*.

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

`SuppressionList.add_profile()` blocks a person by LinkedIn profile, for rows that carry
no email. HeyReach lead lists are not campaigns, so `workspace` is enforced as
`all_campaigns` (`effective_scope` says so in the report).

## Run it

```bash
python -m pip install -e ".[dev]"
python -m pytest -q
```

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
  run against a live Smartlead, Instantly or lemlist account; tests use `FakeTransport`.
- Whether a vendor itself refuses a lead already in the *same* campaign is not verified;
  the push log and `in_campaign` keep the lab from re-pushing either way.
- No vendor response bodies are parsed for counts (no first-party schema for them was
  found); `added` / `skipped_by_vendor` come only from `reconcile()` with a member read-back.
- HeyReach shapes (`AddLeadsToCampaign`, `GetCampaignsForLead`, `X-N8N-KEY`, 100 per
  page) come from HeyReach's n8n package `n8n-nodes-heyreach` 1.0.5, read 2026-11-02; the
  100-pair batch size for adds is the lab's own choice (no limit found). Not run live.
- Wiring LinkedIn pairs into the push log / `execute_plan` (profile URL as the key), a
  real HTTP transport, the member read-back calls and an n8n workflow come next
  (see [ROADMAP](../../ROADMAP.md)).
- `InMemoryPushLog` is per process; a persistent store is needed for real runs.
- Membership sets (`in_campaign`, …) are only as fresh as the caller's last read; the
  vendor's own flag must stay on as the second line of defence.
