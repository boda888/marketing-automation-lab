# 04 · Waterfall enrichment + email verification + CRM dedupe

**The pain:** enrichment quietly burns credits and still misses. A waterfall
step had an empty input mapping, so a provider was called with nothing and
coverage sat near 45% until someone noticed. A provider was sent the string
`"Unknown"` as a LinkedIn URL and charged for it. Synthetic test rows reached a
paid provider. Afterwards, nobody could say which provider supplied which
email. The fix is a waterfall in code: an explicit run condition, cheap
providers first, stop at the first acceptable answer, placeholders that never
leave the building, and a record of every attempt.

v0.0.4: the provider interface, the waterfall runner, offline fakes, an
input-mapping preflight, a coverage report and the email status ladder with
ZeroBounce and NeverBounce result mappings.
Verification checks, per-record budgets and CRM dedupe are next (see [ROADMAP](../../ROADMAP.md#04--waterfall-enrichment--email-verification--crm-dedupe)).

## Architecture

```
LeadRecord(record_id, fields)          placeholders ("Unknown", "N/A", "-", "") → None
   │
   ▼
Waterfall(field, providers, run_if, accept)
   ├─ run_if false (default: field already filled) ──► NOT_RUN, no calls
   └─ for each provider, in order:
        paid provider + fixture record        ──► skipped_fixture
        no complete input set                 ──► skipped_missing_inputs
        call with only that set's fields      ──► found → stop, answered_by
                                                  not_found / rejected / error → next
   ▼
WaterfallOutcome: status, value, answered_by, attempts[provider, status, inputs_used, cost]
   │
   ▼
coverage(outcomes) ──► found / run records, found / input records, per-provider hits / calls, cost

check_mappings(waterfall, records)   dry run before any paid call: who can be fed, what is missing
```

## What exists now

| piece | rule |
|---|---|
| `LeadRecord` | stable `record_id` (not a placeholder) + named fields; placeholders become `None` |
| `is_fixture` | any field on a reserved test domain (`.example`, `.test`, `.invalid`, `.localhost`, `example.com/net/org`, RFC 2606 / 6761) |
| `Provider` protocol | `name`, `output_field`, `input_sets` (alternative required-field sets, preferred first), `paid`, `lookup(inputs) → ProviderResult` |
| `Waterfall` | one field, ordered providers; misconfiguration (no providers, duplicate names, wrong output field, empty input set) fails at construction |
| run condition | explicit `run_if`; default: run only while the field is empty |
| acceptance | optional `accept(value)`; a rejected answer (e.g. a role address) moves on to the next provider |
| fixtures | a `paid=True` provider never receives a fixture record |
| attempts | every provider gets one entry: status, the input *field names* used (never values), credits charged, the provider's own status as context |
| outcome | `found` / `not_found` (something was called) / `not_run` / `no_eligible_provider` (every provider skipped) |
| `run_many` | refuses duplicate `record_id`s: ids are the keys later steps update by |

**Before a run — `check_mappings(waterfall, records)`** calls nothing. Per
provider it reports how many records the waterfall would run on have a complete
input set, how many a paid provider must skip as fixtures, and which required
fields are missing. Findings:

| code | severity | when |
|---|---|---|
| `INPUT_FIELD_EMPTY_EVERYWHERE` | error (warning if another input set of the provider still works) | a required field is empty or a placeholder in every record: unmapped column or typo |
| `NO_ELIGIBLE_RECORDS` | error | the provider would never be called |
| `LOW_ELIGIBILITY` | warning | fewer than `min_eligible` (default 50%) of run records can be fed |
| `RECORDS_WITHOUT_PROVIDER` | warning | some run records fit no provider at all |

`report.ok` is false on any error, so an n8n step can stop before spending credits.

**After a run — `coverage(outcomes)`**: every ratio is a `Ratio` carrying its
denominator name (`records the waterfall runs on`, `input records`, `calls to
this provider`), and `0/0` is `None`, not 0%. Per provider: calls (skips
excluded), accepted answers, rejections, errors, credits; overall credits and
credits per record found.

**Email status ladder — `assess_email` / `assess_outcome`.** A found address
is not a valid address, and the rows left after removing the invalid ones are
not "verified". Each address earns its rung from explicit checks of *that*
address:

| status | when | reason codes |
|---|---|---|
| `found` | an address + the provider that found it, no usable check | `no_check`, `only_stale_checks` |
| `checked` | a named checker looked at it on a known date, verdict not a clean independent "valid" | `catch_all`, `unknown`, `checked_by_finder_only` |
| `verified` | deciding check says valid, within `max_age` (default 90 days), checker ≠ finder | `valid` |
| `invalid` | deciding check says invalid or do-not-mail (spam trap, abuse, ...) | `invalid`, `do_not_mail` |

- `EmailCheck` = address, checker, timezone-aware `checked_at`, normalised
  `result`, the checker's own code as `provider_status` (context). A check
  dated after `now` raises; a placeholder checker name is rejected.
- Checks of another address (e.g. the old employer's email) are ignored and
  counted in `checks_ignored`.
- Deciding check: the newest fresh conclusive one; `unknown` decides only
  when nothing else is fresh; same timestamp → the most cautious result.
- The finder's own label never verifies its own answer unless
  `require_independent=False` is passed on purpose.
- `EmailCheck.from_zerobounce(email, status, sub_status, checked_at=...)` maps
  ZeroBounce's `status` values (`valid`, `invalid`, `catch-all`, `unknown`,
  `spamtrap`, `abuse`, `do_not_mail`, from `ZBValidateStatus` in
  zerobouncesdk 2.2.4); an unmapped status raises instead of guessing.
- `EmailCheck.from_neverbounce(email, result, flags, checked_at=...)` maps
  NeverBounce's single-check `result` by name or code (`valid` 0, `invalid` 1,
  `disposable` 2 → do-not-mail, `catchall` 3, `unknown` 4, from NeverBounce's
  own n8n node 1.0.20); flags are kept as context. Map `result`, never the
  node's `email_verified`: that flag is true for every address it checked.
- `ladder_summary(assessments)` counts each rung as a `Ratio` over
  `emails found` (an address assessed twice raises); `sendable` = verified.

`FakeProvider` answers from a lookup table, can simulate faults (`ProviderError`)
and billing only on hits (`charge_on_miss=False`), and records every call so
tests can assert what was — and was not — sent.

## Run it

```bash
python -m pip install -e ".[dev]"
python -m pytest -q
```

```python
from enrichment import FakeProvider, LeadRecord, Waterfall

li = (("linkedin_url",),)
cheap = FakeProvider("cheap", "email", li, answers={}, cost_per_call=0.5)
pricey = FakeProvider(
    "pricey", "email", li, answers={"https://www.linkedin.com/in/ada": "ada@northwind.example"}
)
out = Waterfall("email", [cheap, pricey]).run(
    LeadRecord(record_id="lead-1", fields={"linkedin_url": "https://www.linkedin.com/in/ada"})
)
out.status, out.answered_by, out.cost  # found, 'pricey', 1.5
```

## Limits

- No real provider adapters yet: the shapes of Clay / Apollo requests were not
  checked against vendor docs (docs.apollo.io and university.clay.com were
  unreachable from the run environment on 2026-10-21).
- The ladder consumes checks; it does not run them. No verifier adapter
  calls an API yet; only ZeroBounce's and NeverBounce's result names are
  mapped (taken from their published SDK / n8n node packages; the vendors'
  docs pages were unreachable on 2026-10-22 and 2026-10-24). No syntax / MX / disposable checks of our own yet.
- No per-record cost budget or cache yet; cost is reported as provider
  credits, not money.
- Fixture detection looks at domain-like field values only; a fixture with no
  domain, email or URL field is not recognised.
- Nothing here has run against a live provider.
