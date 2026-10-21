# Research roadmap

One track per morning run, oldest `last_researched` first (tracked in `.agent/state.json` →
`research.tracks`). Each run answers 1–3 open questions of its track, ticks them here and
adds new questions it discovered. Every track ends in something usable: an insight the owner
can apply, a catalogue row, or a lab roadmap item.

## R1 · Account & contact sourcing
- [ ] Clay vs Apollo vs AI Ark vs MoltSets for company + people search: filters, coverage, credits per 1,000 rows, export / API limits
- [ ] Which sourcing tools expose an MCP server or API that an agent (Claude) can drive directly; what that changes in the workflow
- [ ] Lookalike sourcing from best customers: how tools do it, input format, quality checks
- [ ] Airtable (or Postgres / Supabase) as the ops database behind Clay: sync patterns, record limits, automations

## R2 · Signals & research
- [ ] Apify actors for jobs, LinkedIn posts / reactions, company pages: cost per 1,000 results, reliability, ToS risk
- [ ] Firecrawl / site crawling for ICP and signal research: crawl vs scrape, cost, output shape for LLM prompts
- [ ] Website-visitor identification (RB2B and similar): how it works, match rates claimed vs observed, privacy limits (US-only, GDPR)
- [ ] Hiring, funding, tech-change and "new leader" signals: which sources are fresh enough to act on within a week

## R3 · Enrichment & verification
- [ ] Email waterfall order by cost and hit rate (Icypeas, Prospeo, LeadMagic, Findymail, Freckle, Clay); pay-per-found vs subscription
- [ ] Verification as a separate step (MillionVerifier, NeverBounce, ZeroBounce…): catch-all policy, bounce thresholds that stop a domain
- [ ] Phone enrichment and validation: mobile vs direct vs main line, cost per valid number, when it pays off

## R4 · Email infrastructure & sequencers
- [ ] Mailbox infrastructure (Zapmail-style Google / Microsoft inbox providers): domains per mailbox, warm-up time, cost per inbox
- [ ] Smartlead vs Instantly vs Lemlist: APIs, webhooks, custom variables, subsequences, campaign-level vs global dedupe, pricing tiers
- [ ] Inbox placement testing and warm-up: what is measurable, what is vendor theatre
- [ ] Reply handling: webhook payloads, unified inbox, how to push replies to CRM / Slack / Telegram

## R5 · LinkedIn automation
- [ ] HeyReach vs Aimfox vs Expandi / Waalaxy: multi-sender, limits per account, withdrawing invites, API / webhooks, n8n integration
- [ ] Safe volume and account warm-up; renting sender profiles — risks and what practitioners report
- [ ] Combining LinkedIn and email in one sequence: who orchestrates (sequencer, n8n, Clay), dedupe across channels

## R6 · Calls & AI voice
- [ ] Dialers and call tasks (JustCall, Aircall, Kixie…): API, click-to-call from CRM, call-state sync back to the sequence
- [ ] AI voice agents (Vapi, Retell, Bland, Synthflow, ElevenLabs agents): outbound vs inbound qualification, latency, cost per minute, CRM write-back
- [ ] Compliance for automated calls (TCPA / consent in the US, UK / EU rules): what an AI caller may and may not do
- [ ] "Call when engaged" patterns: trigger a call task from email opens / replies / site visits without blocking the sequence

## R7 · CRM, pipeline & reporting
- [ ] HubSpot vs Attio vs Twenty vs Pipedrive for outbound teams: custom objects, API limits, dedupe, stage design
- [ ] Funnel reporting from raw events (sent → replied → meeting → deal) by segment / signal; tools vs a sheet / Notion
- [ ] Cost tracking: tool spend per hypothesis, cost per meeting

## R8 · AI agents & MCP over the stack
- [ ] Which GTM tools ship MCP servers (Clay, HubSpot, Apollo, Smartlead, Airtable…); what an agent can safely do with them
- [ ] Claude skills / agents for research, first lines and reply drafting: guardrails, review steps, eval practice
- [ ] Agent-run workflows vs n8n: when each is the right tool

## R9 · Orchestration & reliability
- [ ] n8n patterns used in real outbound stacks: queues, per-item loops, error workflows, idempotency, rate limits
- [ ] Make / Zapier / Clay native automations vs n8n: cost and failure modes at 2,000+ records
- [ ] End-to-end reference architectures practitioners publish (diagrams, templates) — what they skip
