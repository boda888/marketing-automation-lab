# marketing-automation-lab

A public GTM-engineering lab: production-style building blocks for signal-based
B2B outbound — source → enrich → qualify → personalise → send → handle replies →
CRM → report — as small, tested services glued together with n8n. Focus and
rules: [`.agent/FOCUS.md`](.agent/FOCUS.md).

## How this repo is built (read this first)

This is an **AI-assisted lab by [Lunaris Tech](https://lunaris.tech)**.

- Most code here is written by an AI coding agent (Claude) that runs on a
  daily schedule, directed and reviewed by Bohdan Khokhlov (Lunaris Tech).
- Each run reads [`ROADMAP.md`](ROADMAP.md) and
  [`.agent/state.json`](.agent/state.json), researches current information
  (API changes, mailbox-provider rules, n8n releases), then makes 0–4 small
  commits — each one tested. A run with nothing worthwhile to add makes no
  commits. The agent's rules are in [`.agent/RUN_PROTOCOL.md`](.agent/RUN_PROTOCOL.md).
- Commit dates are real; history is not backfilled.
- There is no client code or client data here. Examples use fictional
  companies on `.example` domains.
- Things that have not been verified against a live system (a real n8n
  instance, a real CRM, a real ESP) are labelled as such in each README.

## Projects

| # | Project | Status | What it does |
|---|---|---|---|
| 01 | [icp-scoring](projects/01-icp-scoring) | ✅ v0.1 | Explainable ICP fit + time-decayed engagement → tier + next action. FastAPI, CSV CLI, n8n workflow. |
| 02 | [deliverability-preflight](projects/02-deliverability-preflight) | ✅ v0.1 | SPF lookup budget, DMARC (DMARCbis tree walk, t/np/p=none fallback) + alignment with a selectable org-domain rule (last-two / PSL / tree walk), DKIM keys + DKIM-Signature headers, RFC 8058 one-click unsubscribe, content lint, complaint guardrail → one `pass`/`review`/`block` verdict; sending-capacity & warmup planner and today's batch per mailbox. FastAPI (`/v1/preflight`, `/v1/capacity`, `/v1/schedule`) + two n8n workflows; offline DNS fake + optional live dnspython adapter. |
| 03 | [signal-engine](projects/03-signal-engine) | ✅ v0.1 | Evidence-backed signal events (`found` needs a non-homepage source URL + evidence; `not_found_in_sample` / `unknown` separate), cross-source dedupe with provenance, Tier 1–3 / Hold / Review computed over whole date intervals with a short best signal and reason, headcount growth from raw monthly counts, one engagement event per real signal for 01. FastAPI (`/v1/signals/prioritise`), n8n workflow (Switch on tier; Review never reaches the sequencer), synthetic dataset. |
| 04 | [enrichment-verification](projects/04-enrichment-verification) | ✅ v0.1 | Waterfall enrichment: provider interface with offline fakes, explicit run condition, first accepted answer wins with `answered_by` and a per-attempt log, placeholders (`"Unknown"`) and `.example` fixtures never reach a paid provider; per-record credit budget checked against each provider's `max_cost`, result cache (same lookup never paid twice), timeouts kept as `outcome_unknown`; input-mapping preflight before any call and coverage/cost reports where every ratio names its denominator; email status ladder found → checked → verified / invalid from dated, per-address checks (ZeroBounce / NeverBounce results mapped); email precheck without SMTP probing (syntax, null / implicit MX, disposable, role, known catch-all → risk score), never `valid` on its own; contact dedupe LinkedIn → email → name + company into one person record with per-field provenance and an identity check; Apollo People Match adapter (old-employer matches rejected, bad key stops the run) and bulk match (10 per call, answers used only when they align to the request, faults kept per record); Clay table adapter (push a row with a correlation ref, take the HTTP API column's callback, unknown refs refused, 50,000-submission webhook cap checked before the POST, pending rows reviewed, never re-pushed); one-pass batch pipeline dedupe → waterfall → precheck → ladder → one action per person (`send` / `verify` / `review` / `suppress` / `no_email`) behind a FastAPI batch endpoint; offline demo dataset and an n8n workflow that routes each person on its action (only `send` reaches the sequencer, no retry on the billable call). |
| 05–13 | see [ROADMAP.md](ROADMAP.md) | planned | AI personalisation, sequencer sync, reply routing, CRM sync, n8n reliability, reporting, end-to-end, calibration, GTM MCP server |

## Conventions

- Python 3.11+, FastAPI + Pydantic v2, pytest, ruff (config in `ruff.toml`).
- One folder per project under `projects/NN-name/`, each installable on its own
  (`pip install -e ".[dev]"`) with its own `README.md` and `tests/`.
- n8n workflows live in `projects/*/n8n/*.json` and are structure-checked in CI.
- External APIs (LLMs, enrichment vendors, CRMs) are behind interfaces with
  offline fakes, so the whole test suite runs without keys or network.

## CI

[`.github/workflows/ci.yml`](.github/workflows/ci.yml): ruff, repo checks
([`scripts/check_repo.py`](scripts/check_repo.py): project layout, agent
state, n8n JSON), then pytest for every project on Python 3.11 and 3.13.

## Run locally

```bash
pip install ruff && ruff check . && python scripts/check_repo.py
cd projects/01-icp-scoring && pip install -e ".[dev]" && pytest -q
cd ../02-deliverability-preflight && pip install -e ".[dev]" && pytest -q
```
