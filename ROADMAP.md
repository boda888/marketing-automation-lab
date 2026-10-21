# Roadmap

Project line-up and rules come from [`.agent/FOCUS.md`](.agent/FOCUS.md): every
project removes a concrete GTM failure mode, targets the stack people hire for
(Clay, Apollo, Instantly, Smartlead, HeyReach, HubSpot, Pipedrive, Attio, n8n,
Supabase/Postgres, OpenAI/Claude) behind offline fakes, and ships a demo-able v0.1.

The signal-based outbound pipeline, left to right:

```
03 signals ──► 04 enrich + verify + dedupe ──► 01 ICP scoring ──► 05 research + personalise
                                                                         │
                                       02 deliverability preflight ◄─────┘
                                                   │
                                       06 sequencer sync (Instantly / Smartlead / HeyReach)
                                                   │
                         08 CRM sync ◄── 07 reply intent & routing
                                                   │
                         10 reporting (from raw events) ──► 12 calibration ──► 01, 03
  09 n8n reliability kit · 11 end-to-end docker-compose · 13 GTM MCP server wrap the whole line
```

Order is a plan, not a contract: the daily agent may reorder when research
shows something is more urgent (e.g. a mailbox-provider rule change).
A project's v0.1 = FastAPI service and/or CLI, n8n workflow, synthetic `.example`
dataset, README with problem → architecture → run it → limits.

## 01 · ICP scoring — ✅ v0.1 (2026-10-10)
Explainable fit + decayed engagement → tier/action. FastAPI, CSV CLI, n8n.
Open follow-ups:
- [ ] run the n8n workflow against a real n8n instance (docker) in CI or a script
- [ ] per-account `as_of` in CSV + events from a JSONL file
- [ ] config diff endpoint / versioned configs

## 02 · Deliverability preflight → + sending-capacity & warmup planner — ✅ v0.1 (2026-10-18)
Check a sending setup and a message before it goes out, against current
Gmail / Yahoo / Microsoft bulk-sender requirements.
- [x] DNS checks: SPF (incl. 10-lookup and void-lookup limits), DMARC policy + `rua`, alignment (org domain via Public Suffix List file or DMARCbis tree walk; API: `PREFLIGHT_ORG_DOMAIN`)
- [x] DNS checks: DKIM selector key (`<s>._domainkey`): revoked, RSA length, sha256, Ed25519; DKIM-Signature header (required tags, From signed, `i=`/`d=`, expiry, `l=`) with `d=`/`s=` taken from it
- [ ] DMARCbis (RFC 9989) review against the RFC text — DNS Tree Walk discovery, `psd`, tree-walk org domain, `t` testing flag (`pct` historic), `np` and invalid-`p` → `p=none` fallback done from draft-41; check against the published RFC text still open
- [x] message checks: `List-Unsubscribe` + `List-Unsubscribe-Post` (RFC 8058), From/Return-Path alignment
- [x] content lint (links, tracking domains, spammy patterns) with explainable findings
- [x] complaint-rate guardrail (0.1% target / 0.3% hard) from provided stats
- [x] `preflight()` aggregator: one report, `pass` / `review` / `block` verdict
- [x] DNS via injectable resolver (offline tests); live adapter `DnsPythonResolver` (dnspython, `[live]` extra, `PREFLIGHT_DNS=live`) with NXDOMAIN-aware `np`
- [x] FastAPI + n8n "preflight before send" node
- [x] sending-capacity & warmup planner: mailboxes × domains × daily caps → safe volume
      and ramp schedule, using current provider limits (link the vendor docs)
- [x] today's batch: leads capped at capacity and assigned per mailbox (`POST /v1/schedule`) + n8n "capacity before schedule"
Open follow-ups:
- [ ] import both n8n workflows into a real n8n instance (with 01's)
- [ ] opt-in live-DNS smoke test (env-gated)

## 03 · Signal engine — v0.1.0 (2026-10-21)
Pain: list-based outreach with 2–4% replies.
- [ ] signal sources: hiring (job posts), funding, new office/expansion, tech change, site visits
- [x] headcount growth from raw monthly counts (6/12-month %, ≥20% strong, ≤−10% contraction as its own state, missing month → unknown, one source per figure) → `headcount_growth` event
- [x] evidence-backed signal events: every event carries source URL + observed-at timestamp (`SignalEvent`: non-homepage URL + evidence for `found`, `EventDate` with day/month/year precision)
- [x] priority rules: per-type strong/medium windows over the whole date interval, worst/best case → Tier 1–3 / Hold / Review with a ≤30-word best signal and ≤45-word reason instead of an opaque score
- [x] mapping into engagement events for 01 (one event per merged signal, oldest possible date, no contact id)
- [x] FastAPI `POST /v1/signals/prioritise` + synthetic `.example` dataset + n8n workflow 'prioritise before research' (Switch on tier, Review → human review)
- [x] dedupe the same event seen in several sources (source id within a source, evidence URL, subject + overlapping dates; provenance kept); `Unknown` / `Not found in sample` kept apart from "no signal"

## 04 · Waterfall enrichment + email verification + CRM dedupe
Pain: 40–60% match rates, bounces, duplicates.
- [ ] provider interface (Clay / Apollo shapes) + fake providers; field-level merge with confidence and provenance
- [ ] cost budget per record, cache, stop-when-complete; consumes `needs_enrichment` / `missing_fields` from 01
- [ ] verification: syntax, MX, disposable, role-based, catch-all heuristics; risk score, no SMTP probing by default
- [ ] dedupe / match against existing CRM records
- [ ] status ladder found → checked (provider + date) → verified; validate provider inputs (no `"Unknown"` placeholders) and report coverage/cost with an explicit denominator

## 05 · AI research + first-line personalisation
Pain: one bad model response ships to the whole list.
- [ ] generator → reviewer → deterministic QA; JSON-schema-validated output
- [ ] citation guard: every claim cites a signal/enrichment fact with source URL; no evidence → no line
- [ ] provider abstraction (Anthropic / OpenAI) + deterministic fake LLM for tests
- [ ] eval set and scorer (groundedness, length, banned phrases)
- [ ] prompt ↔ JSON-schema consistency check; campaign geography passed explicitly, never inferred from HQ or contact location

## 06 · Sequencer sync
Pain: leads pushed twice, unsubscribed people re-contacted.
- [ ] Instantly / Smartlead / HeyReach adapters (shapes from current vendor docs), custom variables
- [ ] suppression list, idempotent pushes, rate limits, mocked HTTP tests
- [ ] resolve campaign by explicit name (missing/duplicate → loud error); campaign-level dedup separate from global suppression; update rows by person key, never by company

## 07 · Reply intent classification & routing
Pain: replies triaged by hand, hot leads go cold.
- [ ] classes: interested, objection, referral, OOO + return date, unsubscribe, bounce, wrong person, no meaningful reply; sequencer's own label is context only
- [ ] routing + suppression actions, eval set

## 08 · CRM sync
Pain: CRM no longer matches reality.
- [ ] HubSpot / Pipedrive / Attio upsert with idempotency keys, dedupe, field mapping, dry-run
- [ ] lifecycle stages, assignment/territory rules, field-level ownership; mocked HTTP tests

## 09 · n8n reliability kit
Pain: branches that fail silently.
- [ ] error workflow, retries/backoff, idempotency keys, dead-letter-queue service, alerts
- [ ] one item per lead through fan-out lookups; duplicate / not-ready = skip + log, not Stop and Error; only fully-ready rows trigger hand-off

## 10 · Reporting that reconciles
Pain: dashboard numbers nobody trusts.
- [ ] funnel by segment/signal computed from raw events; reconciliation checks
- [ ] A/B test significance

## 11 · End-to-end
Pain: parts that never ran together.
- [ ] docker-compose: n8n + services + Postgres; full synthetic run; live n8n import test

## 12 · Calibration
Pain: hand-set weights drift.
- [ ] learn ICP and signal weights from closed-won/lost (logistic regression), propose config diff for 01/03

## 13 · GTM MCP server
Pain: agents with no safe access to the pipeline.
- [ ] expose score, enrich, preflight, classify as MCP tools with input validation and dry-run
