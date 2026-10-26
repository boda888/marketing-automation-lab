# 04 · Waterfall enrichment + email verification + CRM dedupe

**The pain:** enrichment quietly burns credits and still misses. A waterfall
step had an empty input mapping, so a provider was called with nothing and
coverage sat near 45% until someone noticed. A provider was sent the string
`"Unknown"` as a LinkedIn URL and charged for it. Synthetic test rows reached a
paid provider. Afterwards, nobody could say which provider supplied which
email. The fix is a waterfall in code: an explicit run condition, cheap
providers first, stop at the first acceptable answer, placeholders that never
leave the building, and a record of every attempt.

v0.0.7: the provider interface, the waterfall runner, offline fakes, an
input-mapping preflight, a coverage report, per-record credit budgets with a
result cache and timeouts kept as "outcome unknown", the email status ladder
with ZeroBounce and NeverBounce result mappings, and an email precheck without
SMTP probing that feeds the ladder, and contact dedupe into one
identity-consistent person record with provenance, and the first real provider adapter
(Apollo People Match). The Clay adapter and CRM dedupe are next (see [ROADMAP](../../ROADMAP.md#04--waterfall-enrichment--email-verification--crm-dedupe)).

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
| `Provider` protocol | `name`, `output_field`, `input_sets` (alternative required-field sets, preferred first), `paid`, `max_cost` (most one call can bill), `lookup(inputs) → ProviderResult` |
| `Waterfall` | one field, ordered providers; misconfiguration (no providers, duplicate names, wrong output field, empty input set) fails at construction |
| run condition | explicit `run_if`; default: run only while the field is empty |
| acceptance | optional `accept(value)`; a rejected answer (e.g. a role address) moves on to the next provider |
| fixtures | a `paid=True` provider never receives a fixture record |
| attempts | every provider gets one entry: status, the input *field names* used (never values), credits charged, the provider's own status as context |
| outcome | `found` / `not_found` (something was called) / `not_run` / `no_eligible_provider` (every provider skipped) / `budget_spent` (nothing found, a provider skipped for budget) / `outcome_unknown` (nothing found, a call timed out) |
| budget | `run(record, budget=CreditBudget(5))`: one budget per record, shared by its email / phone / ... waterfalls; a provider whose `max_cost` no longer fits is `skipped_budget` and a cheaper one after it may still run; `run_many(..., budget_per_record=5)` |
| cache | `ResultCache`: a value or a clean miss is stored per provider + output field + exact inputs; the same lookup again (duplicate rows, re-runs) is replayed at 0 credits, marked `cached`, and still goes through this waterfall's `accept`; errors and timeouts are never cached |
| timeouts | `ProviderTimeout` → `outcome_unknown`: the call may have been processed and billed, so the budget is booked at `max_cost` and nothing is cached — inspect before replaying, never treat it as a free miss |
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
credits per record found. Cache replays (`cached`) and timed-out calls (`unknown`)
are counted apart from calls.

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

**Email precheck — `EmailPrecheck(resolver).check(email)`**, no SMTP. It can
rule an address out, never in: its best result is `unknown`, so on its own it
lifts an address to `checked`, never `verified`. `report.to_check(checked_at=...)`
turns it into an `EmailCheck` by checker `lab-precheck`.

| finding | severity | result | rule |
|---|---|---|---|
| `syntax_invalid` | block | invalid | dot-atom local part ≤ 64, LDH labels ≤ 63, a dot, non-numeric TLD, address ≤ 254; quoted or non-ASCII local parts refused; no DNS query spent |
| `domain_not_found` | block | invalid | NXDOMAIN |
| `null_mx` | block | invalid | MX `0 .` (RFC 7505: accepts no mail), also when mixed with other MX records |
| `no_mail_host` | block | invalid | no MX and no A/AAAA |
| `disposable` | block | do not mail | domain or a parent domain on the blocklist; checked before DNS |
| `implicit_mx` | risk 20 | unknown | no MX but an A/AAAA host (RFC 5321 fallback) |
| `dns_temp_error` | risk 30 | unknown | timeout / SERVFAIL / unparseable MX: never read as "no mail host" |
| `role_address` | risk 30 | unknown | `info@`, `sales@`, `support+eu@`, ... |
| `catch_all_domain` | risk 40 | catch-all | the caller passes domains it knows are catch-all; `catch_all_domains(checks)` collects them from an independent verifier's earlier checks (never from the precheck itself) |
| `non_ascii` | risk 20 | unknown | IDN domain, looked up as its A-label |

Any block → risk 100; otherwise risks add up, capped at 100. DNS answers are cached per
domain in one `EmailPrecheck`, so 500 contacts at one company cost one MX query. The
resolver is injectable (`FakeResolver` for tests); the disposable and role lists are
small built-in samples, replaceable: `load_domain_list(path)` reads the one-domain-per-line
format of the CC0 [disposable-email-domains](https://github.com/disposable-email-domains/disposable-email-domains)
blocklist (~9,900 domains on 2026-10-25), and the role sample is a subset of the MIT
`role-based-email-addresses` 3.1.0 package.

**Contact dedupe — `dedupe_contacts(candidates, source_priority=...)`.** Candidates for
one person arrive from several sources; downstream steps must all read the *same* merged
record, or one of them builds an email on the old employer's domain.

| rule | detail |
|---|---|
| strong keys | LinkedIn profile (`linkedin.com/in/<slug>` from any `*.linkedin.com` host, query / trailing path dropped; company pages are not keys) → email (case-insensitive); either joins, and joins chain |
| weak key | normalised name (case, accents, punctuation) + company domain (`www.` and URL dropped); joins only when the rows sharing it carry at most one LinkedIn profile |
| same name, two profiles | two people; a profile-less row with that name joins neither: `ambiguous_name_match` |
| field values | newest `observed_at` wins; same date → `source_priority`, then input order; each field's `provenance` = source, candidate id, date |
| conflicts | a field seen with values that differ after normalisation lists them all (old employer kept, not lost) |
| identity check | email domain ≠ chosen company domain (subdomains allowed): `email_domain_mismatch`; two profiles joined by one email: `conflicting_linkedin_profiles`; any finding → `review` |

Placeholders never match (`"Unknown"` is not a key), duplicate `candidate_id`s raise,
`person_id` is the group's first candidate in input order, and matching is union-find over
key indexes, so 2,000 rows of one company stay fast.

**Apollo adapter — `ApolloPeopleMatch(transport)`**, a `Provider` for `email` over
`POST https://api.apollo.io/api/v1/people/match` (header `x-api-key`, JSON body).

| rule | detail |
|---|---|
| inputs | `linkedin_url`, else `first_name` + `last_name` + `company_domain` (sent as `domain`, `www.` / URL stripped); any other field or a placeholder raises |
| reveals | `reveal_personal_emails` and `reveal_phone_number` always `false`: work email only; a phone reveal needs a `webhook_url` and answers asynchronously |
| old employer | Apollo matches `domain` against a current *or previous* employer; a person whose `organization.primary_domain` differs from the domain sent is no answer (`apollo:employer_mismatch:<domain>`) |
| label | Apollo's `email_status` is kept as `provider_status` (`apollo:verified`, ...): context only, the ladder never lets the finder verify its own email |
| no email | `person: null`, no email, a placeholder or a locked `email_not_unlocked@...` value → no value, 0 credits (`charge_on_miss=True` if your plan bills misses) |
| errors | 401 / 403 → `ApolloAuthError`, which stops the run instead of skipping Apollo on every record; 429 / 5xx / network → `ProviderError` (next provider); timeout → `ProviderTimeout` (`outcome_unknown`, booked at `max_cost`) |
| test data | `UrllibTransport` (stdlib, live) refuses a body on a reserved test domain before any request; `FakeApolloTransport` answers offline by LinkedIn URL or domain and records every request |

`FakeProvider` answers from a lookup table, can simulate faults (`ProviderError`),
timeouts (`ProviderTimeout`) and billing only on hits (`charge_on_miss=False`), and records every call so
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

- The Apollo request shape comes from open-source integrations of the endpoint
  (Activepieces `@activepieces/piece-apollo` 0.2.1, Pipedream `@pipedream/apollo_io`
  0.7.1) and Apollo's own MCP plugin skills; docs.apollo.io was unreachable from the run
  environment (2026-10-21, 2026-10-26). The response fields read (`person.email`,
  `email_status`, `organization.primary_domain`), the locked-email placeholder and
  "credits only when data comes back" are from knowledge and search summaries, not a
  page opened; `UrllibTransport` has never called Apollo. No Clay adapter yet, no bulk
  match (`/people/bulk_match`, 10 per call).
- `ResultCache` is in memory with no expiry: a long-lived store needs a freshness
  window (a contact's email changes when they change jobs) — not built yet.
- The ladder consumes checks; it does not run them. No verifier adapter
  calls an API yet; only ZeroBounce's and NeverBounce's result names are
  mapped (taken from their published SDK / n8n node packages; the vendors'
  docs pages were unreachable on 2026-10-22 and 2026-10-24).
- The precheck has no live DNS adapter yet (only `FakeResolver`), and no catch-all
  detection of its own: that needs an SMTP probe, deliberately not built. RFC 7505 /
  5321 rules come from knowledge plus a search summary; rfc-editor.org and
  datatracker.ietf.org were unreachable on 2026-10-25.
- Cost is reported as provider credits, not money.
- Fixture detection looks at domain-like field values only; a fixture with no
  domain, email or URL field is not recognised.
- Nothing here has run against a live provider.
