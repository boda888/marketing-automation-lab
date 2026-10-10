# marketing-automation-lab

End-to-end marketing and lead-generation automation, built in public as small,
tested, composable services: enrichment → ICP scoring → LLM-personalised cold
email → intent signals → CRM sync → deliverability, glued together with n8n.

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
| 02+ | see [ROADMAP.md](ROADMAP.md) | planned | deliverability preflight, enrichment waterfall, email verification, LLM personalisation, intent signals, CRM sync, orchestration |

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
```
