# 03 · Signal engine — evidence-backed buying signals, not guesses

**The pain:** list-based outreach gets 2–4% replies, so teams switch to
"signal-based" outbound — and then the signals themselves go wrong. A funding
round found by two sources is counted twice. "No traffic data" is stored as `0`
and read as "stable". A model asked whether a round is "within 12 months" gets
the arithmetic wrong on a few rows, and every line built on it still ships. A
personalisation line cites the company homepage, which proves nothing. The
fix is to treat each signal as a typed event with its evidence, and to do
dates, windows and dedupe in code.

**Status: v0.0.1 — event model only.** Priority rules, dedupe, the API and the
n8n workflow are next (see [ROADMAP](../../ROADMAP.md#03--signal-engine)).

## What exists now

`SignalEvent` (pydantic) — one signal check for one company:

| field | rule |
|---|---|
| `type` | `hiring`, `funding`, `expansion`, `leadership`, `tech_change`, `site_visit` |
| `status` | `found`, `not_found_in_sample`, `unknown` — three states, never `0` / `False` |
| `source_url` | absolute http(s); for `found` required and **not a bare homepage** |
| `evidence` | required for `found` (≤500 chars), forbidden otherwise |
| `sample` | what was searched; required for `not_found_in_sample` ("careers page, 2026-10-19") |
| `occurred_on` | `EventDate` with precision (`2026`, `2026-03`, `2026-03-14`); not after `observed_at` |
| `observed_at` | when the source was read; must be timezone-aware |
| `source_name`, `source_event_id` | provider and its own event id, kept for cross-source dedupe |

Placeholder strings (`"Unknown"`, `"N/A"`, `"-"`, empty) in text fields become
`None`, so they can never pass as evidence or reach a paid provider. Records on
`.example` domains report `is_fixture` so later stages can refuse to enrich them.

`EventDate` keeps the source's precision: `2026-03` means *some day in March*,
so `age_days(as_of)` returns the youngest and oldest possible age. A recency
rule ("funding ≤ 12 months") will be applied to that whole interval instead of
pretending the round happened on the 1st.

```python
from datetime import UTC, datetime
from signal_engine import SignalEvent

event = SignalEvent(
    company_domain="northwind.example",
    type="funding",
    status="found",
    observed_at=datetime(2026, 10, 19, tzinfo=UTC),
    source_url="https://news.northwind.example/2026/03/series-a",
    evidence="Northwind raised a $12M Series A led by Fictive Ventures.",
    occurred_on="2026-03",
)
event.occurred_on.age_days(datetime(2026, 10, 19).date())  # (202, 232)
```

## Run the tests

```bash
cd projects/03-signal-engine
python -m pip install -e ".[dev]"
python -m pytest -q
```

## Limits (today)

- Model only: no source adapters, no dedupe, no scoring, no API or n8n workflow yet.
- Date strings follow ISO 8601 calendar dates at reduced precision (the format schema.org
  `Date` uses, e.g. `JobPosting.datePosted`); quarters ("Q1 2026") and week dates are
  rejected rather than guessed.
- The "not a homepage" rule only checks for an empty path; it cannot tell whether a deep
  link actually supports the claim.
