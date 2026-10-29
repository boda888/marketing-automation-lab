# Research roadmap

One track per morning run, oldest `last_researched` first (tracked in `.agent/state.json` →
`research.tracks`). Each run answers 1–3 open questions of its track, ticks them here and
adds new questions it discovered. Every track ends in something usable: an insight the owner
can apply, a catalogue row, or a lab roadmap item.

## R1 · Account & contact sourcing
- [ ] Clay vs Apollo vs AI Ark vs MoltSets for company + people search: filters, coverage, credits per 1,000 rows, export / API limits
- [x] Which sourcing tools expose an MCP server or API that an agent (Claude) can drive directly; what that changes in the workflow → [note](notes/2026-10-22-R1-sourcing-mcp-servers.md)
- [ ] Does Clay ship a first-party MCP server or agent API (not found in the MCP registry on 2026-10-22); check Clay docs when reachable
- [ ] Apollo credits per MCP tool call (search vs match vs bulk match) and whether MCP calls count against API rate limits
- [ ] Lookalike sourcing from best customers: how tools do it, input format, quality checks
- [ ] Airtable (or Postgres / Supabase) as the ops database behind Clay: sync patterns, record limits, automations

## R2 · Signals & research
- [x] Apify actors for jobs, LinkedIn posts / reactions, company pages: cost per 1,000 results (pricing models + max charge; per-Actor prices only as search summary) → [note](notes/2026-10-23-R2-firecrawl-apify-signal-costs.md)
- [ ] Apify Actor reliability and ToS risk for LinkedIn posts / reactions / company pages; open live Store pages (apify.com blocked 2026-10-23) to confirm per-1,000 prices and whether platform usage is included
- [x] Firecrawl / site crawling for ICP and signal research: crawl vs scrape, cost, output shape for LLM prompts → [note](notes/2026-10-23-R2-firecrawl-apify-signal-costs.md)
- [ ] Firecrawl `/monitor` vs n8n Cron + scrape with `changeTracking` for weekly site-change signals: credits per 100 accounts per month, webhook payload into n8n
- [ ] LinkedIn profile / company data per URL: Firecrawl provider routing (FullEnrich 5 … Apollo 30 credits) vs Apify Actors vs HarvestAPI — cost per 1,000 and freshness
- [ ] Website-visitor identification (RB2B and similar): how it works, match rates claimed vs observed, privacy limits (US-only, GDPR)
- [ ] Hiring, funding, tech-change and "new leader" signals: which sources are fresh enough to act on within a week

## R3 · Enrichment & verification
- [x] Email waterfall order by cost and hit rate (Icypeas, Prospeo, LeadMagic, Findymail, Freckle, Clay); pay-per-found vs subscription → [note](notes/2026-10-24-R3-finder-verifier-billing-statuses.md) (billing rules first-party; hit rates only vendor-published search summaries)
- [x] Verification as a separate step (MillionVerifier, NeverBounce, ZeroBounce…): catch-all policy, bounce thresholds that stop a domain → [note](notes/2026-10-24-R3-finder-verifier-billing-statuses.md) (thresholds are vendor heuristics, no provider cutoff published)
- [ ] Measured waterfall on a 100-contact sample of the owner's own ICP (run outside the repo): hits, false positives (bounces) and cost per verified email per provider — no independent benchmark exists
- [ ] MillionVerifier API docs (blocked 2026-10-24): result codes, whether catch_all / unknown are free on the API, rate limits; Icypeas API statuses and billing
- [ ] Catch-all scoring (ZeroBounce AI scoring, activity data, LeadMagic / others): what the score predicts and what it costs per 1,000
- [ ] Phone enrichment and validation: mobile vs direct vs main line, cost per valid number, when it pays off

## R4 · Email infrastructure & sequencers
- [ ] Mailbox infrastructure (Zapmail-style Google / Microsoft inbox providers): domains per mailbox, warm-up time, cost per inbox
- [x] Smartlead vs Instantly vs Lemlist: APIs, webhooks, custom variables, campaign-level vs global dedupe → [note](notes/2026-10-25-R4-sequencer-dedupe-webhooks.md) (from vendor packages; upload-setting defaults only as search summary)
- [ ] Open the live Smartlead / Instantly API references (blocked 2026-10-25): placement and defaults of Smartlead `settings` ignore_* flags and Instantly `skip_if_*`, webhook payload fields, rate limits per plan; pricing tiers and subsequences
- [ ] Inbox placement testing and warm-up: what is measurable, what is vendor theatre
- [ ] Reply handling: webhook payload fields per vendor and how to push replies to CRM / Slack / Telegram (event names, replay and inbox APIs covered in the 2026-10-25 note)
- [ ] lemlist MCP server (`app.lemlist.com/mcp`, found in the official CLI config): which tools it exposes and which can start sending

## R5 · LinkedIn automation
- [x] HeyReach vs Aimfox vs Expandi / Waalaxy: multi-sender, limits per account, withdrawing invites, API / webhooks, n8n integration → [note](notes/2026-10-26-R5-linkedin-tools-api-limits.md) (from vendor n8n packages; Aimfox node third-party; Expandi has no package; prices only as search summary)
- [ ] Open HeyReach API docs / pricing (DNS-blocked 2026-10-26): does `MESSAGE_REPLY_RECEIVED` fire once per lead, rate limits, webhook payload fields and signing; what `AddLeadsToCampaign` does with a lead already in the campaign
- [ ] Pending-invite withdrawal: which tools withdraw automatically (Aimfox, HeyReach settings) and whether withdrawing frees weekly quota — no API call found in any package
- [ ] Expandi API / webhooks (no npm package 2026-10-26)
- [ ] Safe volume and account warm-up; renting sender profiles — risks and what practitioners report
- [ ] Combining LinkedIn and email in one sequence: who orchestrates (sequencer, n8n, Clay), dedupe across channels

## R6 · Calls & AI voice
- [x] Dialers and call tasks: JustCall API, AI-agent calls, Sales Dialer, call events and webhook signing → [note](notes/2026-10-28-R6-voice-agents-dialer-api.md) (from the vendor n8n node)
- [ ] Aircall / Kixie APIs (no API client package on npm 2026-10-28): click-to-call from CRM, call-state webhooks, disposition sync back to the sequence
- [x] AI voice agents (Vapi, Retell): outbound call creation, idempotency and retries, DNC switches, end reasons, cost per call, CRM write-back events → [note](notes/2026-10-28-R6-voice-agents-dialer-api.md) (vendor SDKs; prices only as search summary; latency not covered)
- [ ] Bland, Synthflow and ElevenLabs agents: call creation, DNC / consent fields, end reasons, cost fields (packages found 2026-10-28, not read); open live Retell / Vapi pricing pages (blocked 2026-10-28)
- [ ] Retell webhook signature header name and retry policy for failed webhook deliveries (not in the SDK)
- [ ] Compliance for automated calls (TCPA / consent in the US, UK / EU rules): what an AI caller may and may not do — FCC 2024 ruling known only from search summaries; open FCC 24-17 and check whether B2B calls to business lines need written consent
- [ ] "Call when engaged" patterns: trigger a call task from email opens / replies / site visits without blocking the sequence

## R7 · CRM, pipeline & reporting
- [x] HubSpot vs Attio vs Twenty vs Pipedrive over the API: upsert keys, duplicate handling, partial batch failures, merges, client retries, rate limits → [note](notes/2026-10-29-R7-crm-upsert-dedupe-limits.md) (from vendor SDKs; Attio from a third-party copy of its OpenAPI; limits only as search summary)
- [ ] Open live HubSpot / Attio / Pipedrive / Twenty docs (DNS-blocked 2026-10-29): HubSpot batch size and upsert behaviour on duplicate keys inside one batch, Attio current spec + webhook signing, Pipedrive token costs per endpoint and burst numbers, Twenty REST upsert parameter
- [ ] Custom objects and stage design for outbound in each CRM (signals, sequences, meetings as objects vs properties); HubSpot webhooks and their retry policy
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
