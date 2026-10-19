# 03 · Signal engine — evidence-backed buying signals, not guesses

**The pain:** list-based outreach gets 2–4% replies, so teams switch to
"signal-based" outbound — and then the signals themselves go wrong. A funding
round found by two sources is counted twice. "No traffic data" is stored as `0`
and read as "stable". A model asked whether a round is "within 12 months" gets
the arithmetic wrong on a few rows, and every line built on it still ships. A
personalisation line cites the company homepage, which proves nothing. The
fix is to treat each signal as a typed event with its evidence, and to do
dates, windows and dedupe in code.

**Status: v0.0.2 — event model + cross-source dedupe.** Priority rules, the API
and the n8n workflow are next (see [ROADMAP](../../ROADMAP.md#03--signal-engine)).

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
| `source_name`, `source_event_id` | provider and its own event id (unique within that source only) |
| `subject` | what the event is about (`Series A`, `Senior Data Engineer`); found only, used by dedupe |

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

## Dedupe: one event, many sightings

`dedupe(events)` merges `found` events about the same company and signal type when
one of these holds, and returns one `MergedSignal` per real event with every
sighting (URL, source, id, date, evidence) kept as provenance:

| reason | rule |
|---|---|
| `same_source_id` | same `source_name` + `source_event_id` (a re-scrape, even under a new URL) |
| `same_url` | same evidence URL, ignoring scheme, `www.`, trailing `/` and `#fragment` |
| `same_subject_and_date` | same normalised `subject` (case, accents, punctuation ignored) **and** overlapping `occurred_on` intervals; both dates required |

Guards: ids are only compared within one source (Lever posting ids, schema.org
`JobPosting.identifier` are unique per source/employer, not globally); two
different ids from the same source are never merged, not even through a third
sighting that matches both — two openings with the same title stay two. The
merged date is the most precise sighting's (overlapping day/month/year
intervals nest); sightings merged by id or URL whose dates do not overlap set
`date_conflict`. `not_found_in_sample` / `unknown` checks come back unchanged in
`checks`, so a failed check never cancels a found event. `duplicates_removed`
is counted against `input_found` (found events in), not all input rows.

```python
from signal_engine import dedupe

result = dedupe([press_release_event, funding_db_event])
result.duplicates_removed  # 1
result.signals[0].sightings  # both URLs, both observed_at
```

## Run the tests

```bash
cd projects/03-signal-engine
python -m pip install -e ".[dev]"
python -m pytest -q
```

## Limits (today)

- No source adapters, no scoring, no API or n8n workflow yet.
- Dedupe without a source id or shared URL needs a `subject` from the caller; it does not
  compare evidence text, so two differently worded sightings with no subject stay two.
  Events with dates that do not overlap (e.g. a round "announced 2026-03-14" vs
  "closed 2026-04-02") are not merged on subject alone.
- Date strings follow ISO 8601 calendar dates at reduced precision (the format schema.org
  `Date` uses, e.g. `JobPosting.datePosted`); quarters ("Q1 2026") and week dates are
  rejected rather than guessed.
- The "not a homepage" rule only checks for an empty path; it cannot tell whether a deep
  link actually supports the claim.
