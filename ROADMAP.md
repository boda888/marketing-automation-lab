# Roadmap

The pipeline this lab is building, left to right:

```
accounts ──► 03 enrichment ──► 01 ICP scoring ──► 04 verification ──► 05 personalisation ──► send
               ▲                    ▲                                         │
               │               06 intent signals                     02 deliverability preflight
               └──────────── needs_enrichment                                  │
                                                     07 CRM sync ◄── 09 reply handling
                                 08 orchestration (n8n + docker-compose) wires it together
                                 10 calibration feeds won/lost data back into 01
```

Order is a plan, not a contract: the daily agent may reorder when research
shows something is more urgent (e.g. a mailbox-provider rule change).

## 01 · ICP scoring — ✅ v0.1 (2026-10-10)
Explainable fit + decayed engagement → tier/action. FastAPI, CSV CLI, n8n.
Open follow-ups:
- [ ] run the n8n workflow against a real n8n instance (docker) in CI or a script
- [ ] per-account `as_of` in CSV + events from a JSONL file
- [ ] config diff endpoint / versioned configs

## 02 · Deliverability preflight — in progress (v0.0.1)
Check a sending setup and a message before it goes out, against current
Gmail / Yahoo / Microsoft bulk-sender requirements.
- [x] DNS checks: SPF (incl. 10-lookup and void-lookup limits), DMARC policy + `rua`, alignment
- [ ] DNS checks: DKIM selector key (`<s>._domainkey`), DMARCbis (RFC 9989) review
- [x] message checks: `List-Unsubscribe` + `List-Unsubscribe-Post` (RFC 8058), From/Return-Path alignment
- [ ] content lint (links, tracking domains, spammy patterns) with explainable findings
- [ ] complaint-rate guardrail (0.1% target / 0.3% hard) from provided stats
- [x] DNS via injectable resolver (offline tests)
- [ ] FastAPI + n8n "preflight before send" node

## 03 · Enrichment waterfall
- [ ] provider interface + fake providers; field-level merge with confidence and provenance
- [ ] cost budget per record, cache, stop-when-complete
- [ ] consumes `needs_enrichment` / `missing_fields` from 01

## 04 · Email verification & contact hygiene
- [ ] syntax, MX, disposable, role-based, catch-all heuristics; risk score, no SMTP probing by default

## 05 · LLM personalisation for cold email
- [ ] grounded first lines: every claim must cite an enrichment fact id; reject otherwise
- [ ] provider abstraction (Anthropic / OpenAI) + deterministic fake LLM for tests
- [ ] small eval set and scorer (groundedness, length, banned phrases)

## 06 · Intent signals
- [ ] normalise hiring, funding, tech-change and website signals into engagement events for 01

## 07 · CRM sync
- [ ] HubSpot / Pipedrive upsert with idempotency keys, dedupe, field mapping, dry-run; mocked HTTP tests

## 08 · Orchestration
- [ ] docker-compose: n8n + services; end-to-end n8n workflow; import test against live n8n

## 09 · Reply handling
- [ ] classify replies (interested / not now / OOO / unsubscribe / bounce), route and suppress

## 10 · Calibration & analytics
- [ ] learn ICP weights from closed-won/lost (logistic regression), propose config diff for 01
