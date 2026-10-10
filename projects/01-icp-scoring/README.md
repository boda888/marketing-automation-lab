# 01 · ICP scoring — explainable fit + engagement → tiered routing

A small, deterministic scoring service that answers two separate questions for
every account and only then combines them:

| Dimension | Question | Inputs |
|---|---|---|
| **Fit** (0–100) | Should we ever sell to them? | headcount, revenue, industry, country, tech stack |
| **Engagement** (0–100) | Is now a good time? | time-decayed events (demo request, pricing page, replies…) |

The pair maps to a tier and a concrete next action:

| Tier | Rule (default thresholds) | Action |
|---|---|---|
| 1 | fit ≥ 70 and engagement ≥ 50 | `ae_one_to_one` |
| 2 | fit ≥ 70, engagement < 50 | `nurture_sequence` |
| 3 | 45 ≤ fit < 70 and engagement ≥ 50 | `programmatic_outreach` |
| 4 | everything else, or disqualified | `deprioritize` |

## Design choices (and why)

These follow current account-fit practice (e.g. the 2026 frameworks from
[Abmatic](https://abmatic.ai/blog/account-fit-score)): keep fit and
engagement separate, score firmographics continuously rather than as binary
pass/fail, decay behavioural signals over time, publish each signal's
contribution, and tie every score to an action.

- **Continuous ranges, log scale.** Headcount and revenue use a trapezoid: full
  credit inside the ideal band, linear falloff (on log10) to hard bounds. A
  25-person company is a near miss for a 30–500 ICP, not a zero.
- **Explainable by construction.** Each criterion returns `match`, `points`,
  `max_points` and a reason. `sum(points) == fit_score`, so a rep (or an LLM
  writing the first line of an email) can see exactly why.
- **Unknown ≠ bad.** Missing fields get partial credit (`missing_value_credit`)
  and are listed in `missing_fields`. If enough weight is missing,
  `needs_enrichment=true` — the hook for the enrichment-waterfall project.
- **Hard disqualifiers.** Excluded industries, countries, domains and domain
  suffixes (`.gov`, `.edu`…) force fit to 0 and tier 4, whatever the engagement.
- **Engagement decay.** `raw = Σ weight · 0.5^(age/half_life)`, events older
  than `max_age_days` are dropped, multiple distinct contacts add a capped
  buying-committee multiplier, and the result saturates:
  `100 · (1 − e^(−raw/saturation))`.
- **ICP as data.** Everything tunable lives in a JSON config
  ([`default_icp.json`](src/icp_scoring/default_icp.json) is a fictional
  example for a B2B SaaS seller). Changing the ICP never touches code.

What it deliberately does **not** do yet: learn weights from closed-won /
closed-lost history. Hand-set weights are a starting point; calibration is on
the [roadmap](../../ROADMAP.md).

## Quick start

```bash
cd projects/01-icp-scoring
pip install -e ".[dev]"
pytest -q

# API
ICP_CONFIG_PATH=src/icp_scoring/default_icp.json uvicorn icp_scoring.api:app --port 8001
curl -s localhost:8001/v1/score -H 'content-type: application/json' -d '{
  "company": {"domain": "acme.io", "industry": "SaaS", "employee_count": 60,
              "country": "GB", "technologies": ["Pipedrive"]},
  "events": [{"type": "pricing_page_view", "occurred_at": "2026-10-09T10:00:00Z", "contact_id": "c1"}]
}'

# Offline CSV scoring (no engagement)
python -m icp_scoring examples/accounts.csv -o scored.csv
```

### Endpoints

| Method | Path | Notes |
|---|---|---|
| GET | `/healthz` | status + config version |
| GET | `/v1/config` | active ICP config |
| POST | `/v1/score` | one `ScoreRequest` → `ScoreResult` |
| POST | `/v1/score/batch` | up to 500 items, returns `tier_counts` |

## n8n

[`n8n/icp-scoring-webhook.json`](n8n/icp-scoring-webhook.json): Webhook →
Config (scoring URL) → HTTP Request `/v1/score` → Set (`owner_queue`,
`send_to_enrichment`) → Respond to Webhook. Import it via *Workflows → Import
from file* and change `scoring_url` in the Config node.

The URL lives in a Set node rather than `$env` because recent n8n versions
block env access in nodes by default. The workflow's structure is checked by
the test suite; it has **not yet been run against a live n8n instance** (that is
a roadmap item).

## Layout

```
src/icp_scoring/
  config.py       ICP config schema + loader
  scoring.py      fit, engagement, tiers (pure functions)
  models.py       request/response models
  api.py          FastAPI app
  cli.py          CSV scorer
tests/            unit, API, CLI and n8n-structure tests
examples/         sample accounts (fictional .example domains)
n8n/              importable workflow
```
