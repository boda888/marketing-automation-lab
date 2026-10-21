# 04 · Waterfall enrichment + email verification + CRM dedupe

**The pain:** enrichment quietly burns credits and still misses. A waterfall
step had an empty input mapping, so a provider was called with nothing and
coverage sat near 45% until someone noticed. A provider was sent the string
`"Unknown"` as a LinkedIn URL and charged for it. Synthetic test rows reached a
paid provider. Afterwards, nobody could say which provider supplied which
email. The fix is a waterfall in code: an explicit run condition, cheap
providers first, stop at the first acceptable answer, placeholders that never
leave the building, and a record of every attempt.

v0.0.1: the provider interface, the waterfall runner and offline fakes. Input-mapping
preflight, coverage reports, the email status ladder, verification and CRM
dedupe are next (see [ROADMAP](../../ROADMAP.md#04--waterfall-enrichment--email-verification--crm-dedupe)).

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
- No per-record cost budget, cache or coverage report yet; cost is reported as
  provider credits, not money.
- Fixture detection looks at domain-like field values only; a fixture with no
  domain, email or URL field is not recognised.
- Nothing here has run against a live provider.
