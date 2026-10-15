# Focus: what this lab builds and why

The lab is a public portfolio of **GTM engineering**: production-style building blocks for
signal-based B2B outbound — source → enrich → qualify → personalise → send → handle replies →
CRM → report. Every project must be something a B2B team, agency or lead-gen shop would
actually pay to have built, and must show engineering they can't get from a no-code template.

## Market signals behind this focus (checked 2026-10-10)
- Signal-based outbound is overtaking list-based outbound; buying committees keep growing, so
  multi-stakeholder, evidence-backed outreach matters more than volume
  ([SyncGTM GTM report 2026](https://syncgtm.com/blog/gtm-report-2026), vendor survey — directional).
- Standard GTM-engineering build order: clean/dedupe/enrich (waterfall) → fit + behaviour
  scoring → activation with human review → validate on 50–100 real records before scaling
  ([Cleverly, Sep 2026](https://www.cleverly.co/blog/gtm-engineering)).
- Recurring asks in current freelance job posts (Oct 2026): Clay + n8n signal-based
  personalisation, hiring-signal extraction from job posts, transferable outbound systems on
  n8n + Supabase + Instantly + HeyReach, AI lead qualification with instant reply, CRM
  automation with A/B testing, cold-email infrastructure and deliverability.

## Owner's real work
[`PRACTICE.md`](PRACTICE.md) distils the owner's Clay / n8n client projects into failure
modes and patterns (anonymised). Projects should make those failures impossible in code.

## Rules for choosing what to build
1. **Real pain first.** Each project starts from a concrete failure mode ("follow-ups get
   missed", "one bad LLM output ships to 400 people", "CRM no longer matches reality",
   "works at 200 records, falls over at 2,000") and its README opens with it.
2. **Stack people hire for.** Adapters and examples target Clay, Apollo, Instantly,
   Smartlead, HeyReach, HubSpot, Pipedrive, Attio, GoHighLevel, n8n, Supabase/Postgres,
   OpenAI/Claude. Vendor calls sit behind interfaces with offline fakes; real API shapes come
   from the vendor's current docs (link them).
3. **Engineering that no-code skips:** schema-validated LLM output, evidence for every claim,
   idempotency, retries/backoff, dead-letter queues, dedupe, rate limits, reconciliation,
   cost per record.
4. **Evidence over invention.** Personalisation and signals must carry a source URL and a
   timestamp; no signal → no personalisation line, never a made-up one.
5. **Demo-able.** Each v0.1 ships: FastAPI service and/or CLI, n8n workflow, synthetic dataset
   on `.example` domains, a README with problem → architecture → run it → limits.
6. **Trends are inputs, not content.** A trend only earns a commit if it changes code or the
   roadmap. No trend-summary filler files.

## Project line-up (order can change if research says so)
| # | Project | Pain it removes |
|---|---|---|
| 01 | ICP scoring ✅ | "scoring lives in someone's head" |
| 02 | Deliverability preflight (in progress) → + sending-capacity & warmup planner | deliverability drops quietly at volume |
| 03 | Signal engine: hiring, funding, new office/expansion, tech change, site visits → evidence-backed signal events, priority rules | list-based outreach with 2–4% replies |
| 04 | Waterfall enrichment + email verification + CRM dedupe | 40–60% match rates, bounces, duplicates |
| 05 | AI research + first-line personalisation: generator → reviewer → deterministic QA, JSON-schema validation, citation guard, eval set | one bad model response ships to the whole list |
| 06 | Sequencer sync: Instantly / Smartlead / HeyReach adapters, custom variables, suppression, idempotent pushes | leads pushed twice, unsubscribed people re-contacted |
| 07 | Reply intent classification & routing (interested, objection, referral, OOO + return date, unsubscribe, bounce) | replies triaged by hand, hot leads go cold |
| 08 | CRM sync: HubSpot / Pipedrive / Attio upsert, lifecycle stages, assignment/territory rules, field-level ownership | CRM no longer matches reality |
| 09 | n8n reliability kit: error workflow, retries/backoff, idempotency keys, DLQ service, alerts | branches that fail silently |
| 10 | Reporting that reconciles: funnel by segment/signal from raw events, A/B test significance | dashboard numbers nobody trusts |
| 11 | End-to-end: docker-compose (n8n + services + Postgres), full synthetic run, live n8n import test | parts that never ran together |
| 12 | Calibration: learn ICP and signal weights from won/lost | hand-set weights drift |
| 13 | GTM MCP server: expose the lab's tools (score, enrich, preflight, classify) to AI agents | agents with no safe access to the pipeline |
