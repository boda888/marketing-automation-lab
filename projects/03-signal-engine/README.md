# 03 · Signal engine — evidence-backed buying signals, not guesses

**The pain:** list-based outreach gets 2–4% replies, so teams switch to
"signal-based" outbound — and then the signals themselves go wrong. A funding
round found by two sources is counted twice. "No traffic data" is stored as `0`
and read as "stable". A model asked whether a round is "within 12 months" gets
the arithmetic wrong on a few rows, and every line built on it still ships. A
personalisation line cites the company homepage, which proves nothing. The
fix is to treat each signal as a typed event with its evidence, and to do
dates, windows and dedupe in code.

**Status: v0.0.4 — event model, cross-source dedupe, priority tiers, headcount growth.** The API,
the n8n workflow and a synthetic dataset are next (see [ROADMAP](../../ROADMAP.md#03--signal-engine)).

## What exists now

`SignalEvent` (pydantic) — one signal check for one company:

| field | rule |
|---|---|
| `type` | `hiring`, `funding`, `expansion`, `leadership`, `tech_change`, `site_visit`, `headcount_growth` |
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

## Priority: Tier 1 / 2 / 3 / Hold / Review, computed in code

`prioritise_events(events, as_of)` dedupes, then tiers each company. Every signal type
has a strong and a medium age window (defaults below, overridable via `windows=`; they
are starting points for calibration in project 12, not facts):

| type | strong | medium |
|---|---|---|
| funding | ≤ 365 days | 366–730 |
| hiring | ≤ 60 | 61–180 |
| expansion | ≤ 180 | 181–365 |
| leadership | ≤ 120 | 121–365 |
| tech_change | — (context, never strong) | ≤ 180 |
| site_visit | ≤ 14 | 15–30 |
| headcount_growth | ≤ 90 (latest reported month) | 91–180 |

A window holds only if the **whole** date interval is inside it: `2025` is 292–656
days old on 2026-10-19, so it is medium in the worst case and strong in the best case.
Such a signal is *pending* (as are undated signals, sightings whose dates disagree,
and `unknown` checks for a type with no found signal; `not_found_in_sample` is settled).

The tier is computed with every pending signal at its worst and at its best band:
Tier 1 = a strong signal + a qualifying signal of another type, Tier 2 = a strong
signal, Tier 3 = medium only, Hold = none. If both agree, that is the tier. If not,
the worst case stands and `upgrade_possible` is set — never send on an unsettled
signal — and a worst case of Hold becomes **Review**. Each account carries a
`best_signal` (≤ 30 words) and a `reason` (≤ 45 words) instead of an opaque score:

```text
Tier 2 | funding: Series A (2026-03-14), 219 days old, strong (<=365d)
       | Tier 2: 1 strong, 0 medium settled.
```

## Headcount growth from raw monthly counts

`assess_headcount(HeadcountSeries)` turns the employee counts **one source** reported per
month (`{"2026-04": 50, "2026-10": 62}`) into growth over 6 and 12 months back from the
latest reported month, and `.to_event()` into a `headcount_growth` check:

| state | rule (defaults, `HeadcountThresholds`) | event |
|---|---|---|
| `strong_growth` | any horizon ≥ +20% | `found`, figures as evidence, dated by the latest month |
| `contraction` | any horizon ≤ −10% (and none strong) | `not_found_in_sample`, figures as sample |
| `steady` | every horizon known, between the two | `not_found_in_sample` |
| `unknown` | a horizon is unknown and none is strong or contracting | `unknown` |
| `mixed` | one horizon strong, another contracting | `unknown` — check by hand |

A missing month, a `None` count or a base of 0 makes that horizon unknown: never 0, never
interpolated, never replaced by a neighbouring month. The 20% / −10% edges are compared
exactly (fractions, not floats); `pct` is rounded to 0.1 for display only. Two sources
reporting the same month merge in `dedupe` (subject `headcount`, same month), so one
growth spurt is not counted twice; their figures are never mixed into one percentage,
because providers count observed profiles and routinely disagree.

```text
strong_growth: 50 (2026-04) -> 62 (2026-10): +24.0% over 6 months;
               40 (2025-10) -> 62 (2026-10): +55.0% over 12 months
```

## Into 01's engagement score: one event per real signal

`to_engagement_events(dedupe(events))` turns deduplicated signals into rows with
01-icp-scoring's `EngagementEvent` fields (`type`, `occurred_at`, `contact_id`) plus
provenance (`source_url`, `sightings`, `signal_type`) that 01 ignores:

- **one event per `MergedSignal`**, never per sighting: a round seen by a press release
  and a funding database is one `signal_funding` event with `sightings=2`;
- `not_found_in_sample` / `unknown` checks never become events;
- `occurred_at` is the **oldest** day the date can mean (`2026-03` → 2026-03-01; with a
  date conflict, the oldest sighting), so decay never makes a signal look fresher;
- undated signals, and types mapped to `None` in `event_types=`, come back in `skipped`
  with a reason instead of being dated "now";
- `contact_id` is always `None`: signals are account-level and must not trigger 01's
  buying-committee multiplier.

Default types: `site_visit` → `site_visit` (already a 01 event), every other type →
`signal_<type>`; 01's `default_icp.json` carries hand-set weights for them.

## Run the tests

```bash
cd projects/03-signal-engine
python -m pip install -e ".[dev]"
python -m pytest -q
```

## Limits (today)

- No source adapters, no API or n8n workflow yet.
- The 01 mapping is one-way and offline: 01 still decays with its own 14-day half-life and
  drops events older than 90 days, so 01's engagement only sees recent signals.
- Headcount: no provider adapter yet (the caller supplies the monthly counts); thresholds
  20% / −10% are PRACTICE defaults, not calibrated; growth is total headcount, not per
  function; provider field shapes (e.g. counts 1/3/6/9/12 months back) come from search
  summaries only — the vendor docs were unreachable from the run environment.
- Window defaults are hand-set; the tier ignores how many sources confirmed a signal.
- Dedupe without a source id or shared URL needs a `subject` from the caller; it does not
  compare evidence text, so two differently worded sightings with no subject stay two.
  Events with dates that do not overlap (e.g. a round "announced 2026-03-14" vs
  "closed 2026-04-02") are not merged on subject alone.
- Date strings follow ISO 8601 calendar dates at reduced precision (the format schema.org
  `Date` uses, e.g. `JobPosting.datePosted`); quarters ("Q1 2026") and week dates are
  rejected rather than guessed.
- The "not a homepage" rule only checks for an empty path; it cannot tell whether a deep
  link actually supports the claim.
