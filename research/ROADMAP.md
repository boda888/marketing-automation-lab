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
- [x] Airtable as the ops database behind Clay: API limits, upsert on duplicate keys, n8n node batching / polling, client retries → [note](notes/2026-11-02-R1-airtable-ops-db-sync.md) (behaviour from n8n + airtable.js source; limits only as search summary)
- [ ] Open Airtable's own API reference (blocked 2026-11-02): confirm 5 req/s per base, 30 s penalty, `performUpsert` multiple-match error text, records per base per plan, and what Airtable automations count against
- [x] Postgres / Supabase instead of Airtable as the ops DB: when practitioners switch (row counts, cost, concurrency), and how Clay writes to it (HTTP API column vs Supabase integration) → [note](notes/2026-11-13-R1-supabase-ops-db-clay-n8n.md) (upsert / transaction / paging behaviour from n8n + PostgREST + Postgres source; plan limits from Supabase docs source; Clay side only search summaries)
- [ ] Open supabase.com docs when reachable: Data API max-rows default, Free / Pro price page, any REST rate limit; confirm on a live project that a merge-duplicates bulk POST with a duplicate key returns 21000 and writes nothing
- [ ] Clay's own docs for HTTP API column → Supabase: retries on 5xx / 429, rows per second, and whether a failed request can be re-run per row without re-spending enrichment credits

## R2 · Signals & research
- [x] Apify actors for jobs, LinkedIn posts / reactions, company pages: cost per 1,000 results (pricing models + max charge; per-Actor prices only as search summary) → [note](notes/2026-10-23-R2-firecrawl-apify-signal-costs.md)
- [ ] Apify Actor reliability and ToS risk for LinkedIn posts / reactions / company pages; open live Store pages (apify.com blocked 2026-10-23) to confirm per-1,000 prices and whether platform usage is included
- [x] Firecrawl / site crawling for ICP and signal research: crawl vs scrape, cost, output shape for LLM prompts → [note](notes/2026-10-23-R2-firecrawl-apify-signal-costs.md)
- [x] Firecrawl `/monitor` vs n8n Cron + scrape with `changeTracking` for weekly site-change signals: credits per 100 accounts per month, webhook payload into n8n → [note](notes/2026-11-03-R2-firecrawl-monitor-vs-n8n-cron.md) (from firecrawl-docs + OpenAPI; not run live)
- [ ] Confirm on the live Firecrawl API what a `/monitor` page with HTTP 404 reports (`changed` vs `error`) and whether `monitor.page` events can arrive twice for one check (no event id dedupe documented)
- [ ] LinkedIn profile / company data per URL: Firecrawl provider routing (FullEnrich 5 … Apollo 30 credits) vs Apify Actors vs HarvestAPI — cost per 1,000 and freshness
- [ ] Website-visitor identification (RB2B and similar): how it works, match rates claimed vs observed, privacy limits (US-only, GDPR) — RB2B webhook fields / API scope only as search summary 2026-11-03 (support site DNS-blocked)
- [x] Hiring and funding signals: which sources are fresh enough to act on within a week → [note](notes/2026-11-14-R2-hiring-funding-signal-freshness.md) (ATS boards from Greenhouse docs source, Lever README and an npm adapter; Form D fields from edgartools; deadline only as search summary; nothing run live)
- [ ] Tech-change and "new leader" signals: which sources are fresh enough to act on within a week (BuiltWith / Wappalyzer-style diffs, job-change feeds in Apollo / PDL / Clay), and what date they carry
- [ ] Open the live ATS endpoints (blocked 2026-11-14): does Lever's live JSON carry `createdAt` despite the README, Ashby `publishedAt` vs an id field, Greenhouse list rate limits; and EDGAR full-text search for Form D by date (query shape, `User-Agent` rule, 10 req/s)
- [ ] Share of target accounts on a public Greenhouse / Lever / Ashby board vs Workday / custom careers pages, measured on the owner's own ICP list (run outside the repo); this decides whether ATS boards can be the main hiring source

## R3 · Enrichment & verification
- [x] Email waterfall order by cost and hit rate (Icypeas, Prospeo, LeadMagic, Findymail, Freckle, Clay); pay-per-found vs subscription → [note](notes/2026-10-24-R3-finder-verifier-billing-statuses.md) (billing rules first-party; hit rates only vendor-published search summaries)
- [x] Verification as a separate step (MillionVerifier, NeverBounce, ZeroBounce…): catch-all policy, bounce thresholds that stop a domain → [note](notes/2026-10-24-R3-finder-verifier-billing-statuses.md) (thresholds are vendor heuristics, no provider cutoff published)
- [ ] Measured waterfall on a 100-contact sample of the owner's own ICP (run outside the repo): hits, false positives (bounces) and cost per verified email per provider — no independent benchmark exists
- [x] MillionVerifier API shape: endpoints, result values, error path, bulk flow → [note](notes/2026-11-15-R3-millionverifier-api-catch-all-scoring.md) (from the Activepieces piece; docs hosts still ENOTFOUND)
- [ ] MillionVerifier first-party (docs blocked 2026-10-24, 2026-11-15): full `resultcode` ↔ `result` table, `subresult` and `quality` values, rate limits, and confirm on a real key that catch_all / unknown leave `credits` unchanged; Icypeas API statuses and billing
- [x] Catch-all scoring (ZeroBounce AI scoring, activity data, catch-all rescue verifiers): output, call shape, cost per address → [note](notes/2026-11-15-R3-millionverifier-api-catch-all-scoring.md) (SDK-level facts first-party; score meaning and credits only search summaries)
- [ ] Measured bounce rate per ZeroBounce score band (0 / 1–4 / 5–7 / 8–10) and per `active_in_days` bucket on the owner's own catch-all cohort (run outside the repo) — the only way to set a defensible cut-off
- [ ] Catch-all rescue verifiers (Giggal.ai, LeadMagic, others): how they decide valid on a catch-all domain and whether independent tests show lower bounces than score-based cut-offs
- [x] Phone enrichment and validation: mobile vs direct vs main line, cost per valid number, when it pays off → [note](notes/2026-11-04-R3-phone-validation-trestle-twilio.md) (request shapes first-party from vendor packages; field meanings and prices only search summaries; no measured cost per callable number)
- [ ] Trestle `phone_intel` response and price first-party (docs hosts blocked 2026-11-04): full `line_type` enum, `warnings`, billing per call vs per valid result, rate limits
- [ ] Connect rate by activity-score band on the owner's own calling list (run outside the repo) to set the call / unknown / drop cut-offs

## R4 · Email infrastructure & sequencers
- [x] Mailbox infrastructure (Zapmail-style Google / Microsoft inbox providers): domains per mailbox, warm-up time, cost per inbox → [note](notes/2026-11-16-R4-mailbox-infra-warmup-placement.md) (API from npm packages; prices and warm-up timelines only as search summaries)
- [x] Smartlead vs Instantly vs Lemlist: APIs, webhooks, custom variables, campaign-level vs global dedupe → [note](notes/2026-10-25-R4-sequencer-dedupe-webhooks.md) (from vendor packages; upload-setting defaults only as search summary)
- [ ] Open the live Smartlead / Instantly API references (blocked 2026-10-25): placement and defaults of Smartlead `settings` ignore_* flags and Instantly `skip_if_*`, webhook payload fields, rate limits per plan; pricing tiers and subsequences
- [x] Inbox placement testing and warm-up: what is measurable, what is vendor theatre → [note](notes/2026-11-16-R4-mailbox-infra-warmup-placement.md)
- [ ] Open Zapmail / Mailforge / Instantly pricing and warm-up pages first-party (DNS-blocked 2026-11-16); confirm whether Zapmail mailboxes ship pre-warmed and Instantly's health-score threshold
- [ ] Warmforge / Instantly placement-test result fields (per-provider inbox / spam / missing counts) and warm-up stats fields — bodies untyped in the packages read 2026-11-16
- [x] Reply handling: webhook payload fields per vendor, authenticity, dedupe key, receiver-down behaviour → [note](notes/2026-11-05-R4-reply-webhook-payloads.md) (from integration packages; event names, replay and inbox APIs in the 2026-10-25 note)
- [ ] Smartlead webhook event names first-party (`EMAIL_REPLY` in Activepieces 0.2.0 vs `EMAIL_REPLIED` in a third-party n8n node) and the reply payload: does it carry the reply body / message id; open api.smartlead.ai/api-reference/webhooks/events (blocked 2026-11-05)
- [ ] Instantly `reply_received` payload fields and the failure count that auto-disables a webhook; whether retries happen before disable (no package documents either)
- [ ] lemlist MCP server (`app.lemlist.com/mcp`, found in the official CLI config): which tools it exposes and which can start sending — not in CLI 0.4.0 nor the MCP registry (2026-11-05); only third-party wrappers listed in search

## R5 · LinkedIn automation
- [x] HeyReach vs Aimfox vs Expandi / Waalaxy: multi-sender, limits per account, withdrawing invites, API / webhooks, n8n integration → [note](notes/2026-10-26-R5-linkedin-tools-api-limits.md) (from vendor n8n packages; Aimfox node third-party; Expandi has no package; prices only as search summary)
- [ ] Open HeyReach API docs / pricing (DNS-blocked 2026-10-26, Postman documenter blocked 2026-11-06): does `MESSAGE_REPLY_RECEIVED` fire once per lead, webhook payload fields and signing; whether a lead already in the campaign counts as `updatedLeadsCount` in `AddLeadsToCampaignV2` (300 req/min and the V2 counts seen in a third-party CLI 2026-11-06)
- [ ] Smartlead ↔ HeyReach / Instantly ↔ HeyReach native integrations: open the Smartlead help-center guide and check whether a reply on one channel pauses the other (vendor pages claim it, agency guides say no; all blocked 2026-11-06)
- [x] Pending-invite withdrawal: which tools withdraw over an API and whether withdrawing frees weekly quota → [note](notes/2026-11-17-R5-invite-withdrawal-limits-unipile.md) (Unipile v2 list / cancel from the vendor SDK; HeyReach / Aimfox expose none; "does not free quota" and the 3-week re-invite block only as search summaries)
- [ ] Open LinkedIn Help + Unipile provider-limits / manage-invitations docs (DNS-blocked 2026-11-17): exact 422 `cannot_resend_yet` body, whether the re-invite block after a withdrawal is 3 weeks, and whether HeyReach / Aimfox auto-withdraw settings count withdrawals anywhere
- [ ] Expandi API / webhooks (no npm package 2026-10-26)
- [ ] Safe volume and account warm-up; renting sender profiles — risks and what practitioners report (2026-11-17: only vendor ranges ~100–200 / week found, as search summaries; need first-hand ramp data or LinkedIn's own wording)
- [x] Combining LinkedIn and email in one sequence: who orchestrates (sequencer, n8n, Clay), dedupe across channels → [note](notes/2026-11-06-R5-heyreach-public-api-cross-channel.md) (native sync moves leads, not reply stops — search summary only; HeyReach campaign-level exclusions from a third-party CLI)

## R6 · Calls & AI voice
- [x] Dialers and call tasks: JustCall API, AI-agent calls, Sales Dialer, call events and webhook signing → [note](notes/2026-10-28-R6-voice-agents-dialer-api.md) (from the vendor n8n node)
- [ ] Aircall / Kixie APIs (no API client package on npm 2026-10-28): click-to-call from CRM, call-state webhooks, disposition sync back to the sequence
- [x] AI voice agents (Vapi, Retell): outbound call creation, idempotency and retries, DNC switches, end reasons, cost per call, CRM write-back events → [note](notes/2026-10-28-R6-voice-agents-dialer-api.md) (vendor SDKs; prices only as search summary; latency not covered)
- [x] ElevenLabs Agents: outbound call creation (single + batch), idempotency, SDK retries, end reasons, cost fields, webhook signing → [note](notes/2026-11-08-R6-voice-webhooks-elevenlabs-calls.md) (vendor SDK 2.77.0; no DNC field found)
- [x] Bland and Synthflow agents: call creation, DNC / consent fields, end reasons, cost fields → [note](notes/2026-11-18-R6-bland-synthflow-call-creation-dnc.md) (vendor n8n nodes + Bland CLI; no cost field found; Synthflow webhook signing not found)
- [ ] Open live Retell / Vapi / ElevenLabs / Bland / Synthflow pricing pages (blocked 2026-10-28, 2026-11-08, 2026-11-18); confirm whether a Bland global block rule also stops outbound dials and whether Synthflow signs webhooks
- [x] Retell webhook signature header name and retry policy for failed webhook deliveries → [note](notes/2026-11-08-R6-voice-webhooks-elevenlabs-calls.md) (`x-retell-signature` from a third-party receiver; 10 s / 3 retries / no delivery id only as search summary)
- [ ] Open docs.retellai.com / elevenlabs.io when reachable: Retell retry backoff and what happens after the last failed delivery; ElevenLabs webhook header name, retry policy and `termination_reason` values; whether ElevenLabs `cost` is credits
- [ ] Compliance for automated calls (TCPA / consent in the US, UK / EU rules): what an AI caller may and may not do — FCC 24-17 (2024-02-08, AI voice = artificial voice, prior express consent) known only from search summaries (2026-10-28, 2026-11-18); open FCC 24-17 / 47 CFR 64.1200 and check whether B2B calls to business lines need written consent
- [ ] "Call when engaged" patterns: trigger a call task from email opens / replies / site visits without blocking the sequence

## R7 · CRM, pipeline & reporting
- [x] HubSpot vs Attio vs Twenty vs Pipedrive over the API: upsert keys, duplicate handling, partial batch failures, merges, client retries, rate limits → [note](notes/2026-10-29-R7-crm-upsert-dedupe-limits.md) (from vendor SDKs; Attio from a third-party copy of its OpenAPI; limits only as search summary)
- [ ] Open live HubSpot / Attio / Pipedrive / Twenty docs (DNS-blocked 2026-10-29): HubSpot batch size and upsert behaviour on duplicate keys inside one batch, Attio current spec + webhook signing, Pipedrive token costs per endpoint and burst numbers, Twenty REST upsert parameter
- [x] CRM webhooks: HubSpot / Attio / Pipedrive signing, redelivery and what silently stops deliveries → [note](notes/2026-11-10-R7-crm-webhook-auth-retries.md) (signing from vendor SDKs; retry policies only as search summaries)
- [ ] Custom objects and stage design for outbound in each CRM (signals, sequences, meetings as objects vs properties)
- [ ] Confirm on live docs or a delivery: HubSpot v3 header names and Webhooks API timeout / batch size, Attio signature header + whether a timestamp is signed + retry schedule, Pipedrive retry schedule and auto-removal window
- [x] Funnel reporting from raw events (sent → replied → meeting → deal) by segment / signal → [note](notes/2026-11-19-R7-funnel-reporting-raw-events.md) (email half from vendor CLIs: Smartlead event-date vs sent-time routes, Instantly meeting = lead label; docs hosts ENOTFOUND)
- [ ] Funnel for LinkedIn and calls: what HeyReach / Unipile and the dialers expose per lead (send, accept, reply, meeting) and how a cross-channel meeting is attributed to one touch
- [ ] Open Smartlead / Instantly API references when reachable: is Smartlead `reply_count` per lead or per message, does it include auto-replies, and what Instantly `expand_crm_events` changes in the overview
- [ ] Cost tracking: tool spend per hypothesis, cost per meeting (formula only so far — (retainer + data / tool + labour) ÷ meetings, search summaries 2026-11-19; open: Clay credit usage per table via API, sequencer seat / mailbox cost per campaign)

## R8 · AI agents & MCP over the stack
- [x] Which GTM tools ship MCP servers (Clay, HubSpot, Apollo, Smartlead, Airtable…); what an agent can safely do with them → [note](notes/2026-10-31-R8-gtm-mcp-servers-write-safety.md) (sourcing tools in the R1 note)
- [ ] HubSpot's remote MCP server (used by Claude's connector): open the vendor docs when reachable — tool list per scope, whether writes are upserts, how partial failures and the per-write confirmation work (endpoint / OAuth / GA date as search summaries and the dev server's elicitation gate in the [2026-11-11 note](notes/2026-11-11-R8-agent-write-confirmation-elicitation.md); docs still ENOTFOUND)
- [ ] Clay as an MCP / connector: confirm on clay.com or docs.clay.com what an agent can do (read tables, run enrichments, spend credits) and how credits are capped per call (npm `@clayhq/clay-mcp` is clay.earth, not Clay.com; connector facts so far search summaries only — 2026-11-11 note)
- [x] How an MCP server makes a human confirm a write (elicitation), and whether that holds when the agent runs in n8n → [note](notes/2026-11-11-R8-agent-write-confirmation-elicitation.md) (n8n's client declares no elicitation: confirmations degrade to a model-settable flag)
- [x] Claude skills / agents for research, first lines and reply drafting: guardrails, review steps, eval practice → [note](notes/2026-11-21-R8-outbound-skills-guardrails-evals.md) (Anthropic skill docs + a community cold-email skill; prose guardrails, LLM-judged evals without prospect data)
- [ ] Score a community cold-email skill (or the lab's 05 prompts) against a no-signal / stale-signal / injected-text set with a live model — how often does it invent a hook?
- [ ] Agent-run workflows vs n8n: when each is the right tool

## R9 · Orchestration & reliability
- [x] n8n patterns used in real outbound stacks: queues, per-item loops, error workflows, idempotency, rate limits → [note](notes/2026-11-01-R9-n8n-retry-dedupe-error-workflow.md) (from n8n source; behaviour read, not run)
- [ ] Confirm on a live n8n (docker) what the source reading predicts: Remove Duplicates failing at a full history, Retry On Fail re-sending earlier items, error workflow payload with execution data saving off
- [x] How practitioners rate-limit vendor calls in n8n (HTTP Request batching, Wait, Redis / Postgres counters) and run queue mode for outbound volumes → [note](notes/2026-11-12-R9-n8n-rate-limits-queue-mode.md) (from n8n source + docs repo; docs.n8n.io ENOTFOUND, practitioner guides only as search summaries)
- [ ] Published n8n outbound templates (n8n.io/workflows, Clay / Smartlead / Instantly templates): which set Batching, continue-on-fail and dedupe, and what they skip
- [ ] Confirm on a live n8n (docker) that HTTP Request without Batching sends all items concurrently and that queue-mode executions with Wait < 65 s stay on the worker (the Wait node source read 2026-11-12 parks every timed wait via `putExecutionToWait`; any short-wait shortcut would live in core, not read)
- [x] Vendor rate limits in one table (Smartlead, Instantly, HeyReach, HubSpot, Attio, Pipedrive, Clay webhook, Apollo): per key vs per account, window, `Retry-After` / reset headers — to set the lab's rate budgets from published numbers → [note](notes/2026-11-22-R9-vendor-rate-limits-table.md) (numbers from search summaries only; vendor doc hosts ENOTFOUND; client retry behaviour from `@lemlist-official/cli` 0.4.0 and `pipedrive` 36.3.1; Clay webhook limit not found)
- [ ] Open the vendors' rate-limit pages (Instantly, Smartlead, Apollo, Pipedrive, Attio, lemlist, Clay webhook) and confirm or correct the 2026-11-22 table, incl. which return `Retry-After` and whether Pipedrive's daily-token headers exist
- [ ] Make / Zapier / Clay native automations vs n8n: cost and failure modes at 2,000+ records
- [ ] End-to-end reference architectures practitioners publish (diagrams, templates) — what they skip
