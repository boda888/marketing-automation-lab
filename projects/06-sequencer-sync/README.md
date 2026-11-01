# 06 · Sequencer sync (Smartlead / Instantly / lemlist)

**The pain:** leads get pushed twice, unsubscribed people get contacted again, and nobody
can say how many of the 400 leads sent to a campaign were actually added. In real n8n
builds the campaign id was hard-coded and a missing campaign silently fell back to another
one; "this email exists somewhere in the sequencer" blocked a valid new campaign because
campaign dedupe and global suppression were the same switch; and every sequencer defaults
to "insert", with its cross-campaign check named differently (Smartlead `ignore_*`
settings, Instantly `skip_if_*`, lemlist `deduplicate`; research R4, 2026-10-25).

v0.0.1: the planning layer that runs before any HTTP call.

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

- **Dedupe scope** (`campaign`, `all_campaigns`, `workspace`) is a required argument;
  vendor adapters map it to each tool's own flag instead of trusting the tool's default.
- **Suppression is not dedupe.** Unsubscribed / bounced / complained / do-not-contact /
  customer entries block every campaign; no `dedupe_scope` turns them off.
- **Person key, never company.** One email = one lead; two people at one company never
  collide.
- **Idempotency key** = sha256(vendor, campaign id, email). A key is *reserved* before a
  push and *committed* once the vendor confirmed it; a reserved key on the next run means
  "outcome unknown" and the lead waits for reconciliation instead of being pushed again.

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
- No vendor calls yet: adapters (request shapes per vendor, batching, retries) come next
  (see [ROADMAP](../../ROADMAP.md#06--sequencer-sync)).
- `InMemoryPushLog` is per process; a persistent store is needed for real runs.
- Membership sets (`in_campaign`, …) are only as fresh as the caller's last read; the
  vendor's own flag must stay on as the second line of defence.
