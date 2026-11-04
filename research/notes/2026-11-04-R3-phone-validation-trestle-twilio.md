# R3 · Phone validation before a call list: Trestle vs Twilio Lookup, what the fields mean (2026-11-04)

## Question
After a phone waterfall (Clay, Prospeo, Findymail, FullEnrich…), what does a validation step
actually return — line type, "activity", validity — and what does it cost per number, so a
calling list can drop dead lines and landlines before dialers or AI voice agents spend minutes
on them? Which tool fits an n8n flow?

## Findings
Method: every vendor docs and pricing host was blocked by this run's network policy
(trestle-api.redoc.ly and docs.trestleiq.com: ENOTFOUND; trestleiq.com, twilio.com,
activeprospect.com, deepline.com, github.com/TrestleIQ: proxy 403). Request / response shapes
come from the vendors' **own published npm packages**, downloaded from registry.npmjs.org and
read, not executed. Field meanings and prices come only from web-search summaries and are
labelled as such.

**Trestle — vendor n8n node `n8n-nodes-trestle` 0.2.8 (repo `github.com/TrestleIQ/…`, published
2026-07-02):**
- Two operations. *Phone Validation* (international): `GET
  https://api.trestleiq.com/3.0/phone_intel?phone=…`, optional `phone.country_hint` (ISO-3166
  alpha-2) and `addons=litigator_check`. *Real Contact* (US only): `POST
  https://api.trestleiq.com/1.1/real_contact` with `name` + `phone` required, optional email,
  IP, address, city, state, postal code; add-ons `email_deliverability`, `litigator_check`
  (README: each add-on "additional cost"), returns an A–F contact grade per the README.
- Auth is one header, `x-api-key`; the credential test calls `phone_intel` for a fixed number,
  so **testing the credential in n8n spends a lookup** (if lookups are billed per call).
- The node **replaces each item with the raw API response** (`json: phoneApiResponse`, only
  `pairedItem` kept): the lead id, name and company from the input item are gone after the node.
- An empty phone field **throws** (`Phone number not found in field …`) and, without "continue on
  fail", stops the whole batch; with it, the item becomes `{error: …}` — no phone field either.
- One HTTP call per item, no batching, no retries, no rate limiting in the node.
- Response fields are not in the package. Per search summaries of Trestle's docs (not opened):
  `is_valid` (boolean **or null**), `activity_score` 0–100 (100 = consistent activity in the last
  12 months, 0 = disconnected / no activity in a year, **50 = not enough signals to tell**),
  `line_type` (Mobile, Landline, FixedVOIP, NonFixedVOIP, TollFree, Voicemail, Other — the
  summary's own doc excerpt lists fewer values), `carrier`, `is_prepaid`, `country_code`,
  `country_name`. Clay's phone guide (search summary) says every number its waterfall finds is
  run through Trestle, and a failed validation sends the row to the next provider.
- Price: not found first-party. One reseller (ActiveProspect) charges $0.015 per lookup (search
  summary, reseller rate, not Trestle's list price).

**Twilio Lookup v2 — official `twilio` SDK 6.2.0 (npm, read 2026-11-04):**
- `GET /v2/PhoneNumbers/{number}?Fields=…` with packages `validation`, `caller_name`,
  `sim_swap`, `call_forwarding`, `line_status`, `line_type_intelligence`, `identity_match`,
  `reassigned_number`, `sms_pumping_risk`, `phone_number_quality_score`, `pre_fill`; `CountryCode`
  for national-format input. Basic `valid` + `validation_errors` (`TOO_SHORT`, `TOO_LONG`,
  `INVALID_BUT_POSSIBLE`, `INVALID_COUNTRY_CODE`, `INVALID_LENGTH`, `NOT_A_NUMBER`) — validity is
  numbering-plan validity ("in a valid range that can be freely assigned"), **not** "connected".
- `line_type_intelligence` → `{type, carrier_name, mobile_country_code, mobile_network_code,
  error_code}`; `line_status` → `{status, error_code}`; `reassigned_number` →
  `{is_number_reassigned (string), last_verified_date, error_code}` and takes `LastVerifiedDate`
  = the date consent was obtained. Every package carries **its own `error_code`**: one request can
  return a valid number with a failed line-type package.
- Batch: `POST /v2/batch/query` with `phone_numbers[]` each carrying a `correlation_id`; each
  result echoes `correlation_id` and may carry a `twilio_error_code` — the join key for matching
  answers to leads.
- The SDK's 429 auto-retry is **off by default** (`autoRetry`, `maxRetries` 3, `maxRetryDelay`
  3,000 ms when on).
- Prices (search summary of Twilio's pricing page, not opened): line type intelligence
  $0.008 / request, line status $0.007, reassigned number listed both as $0.02 and as volume tiers
  down to $0.0015 — conflicting, recheck before buying. Line type values per summaries: mobile,
  landline, fixedVoip, nonFixedVoip, tollFree, personal, payphone, voicemail, unknown.

**Phone waterfall cost (search summaries, not opened):** Clay community: phone enrichments 2 to
25 credits per number by provider; a third-party comparison says Clay charges a found number
only when it validates, landlines free (unconfirmed). Prospeo bills 10 credits for email +
mobile (first-party, R3 note 2026-10-24); Findymail 10 credits per found phone (same note).
Coverage claims run from Clay's "30 % → 80 %+" (vendor) down to 34.5 % in one independent test
of a PDL + Clearbit waterfall on 2,400 contacts (dev.to post). No measured cost per *callable*
number was found. Mobile vs direct dial: mobile for executives / field roles, desk line for
inside roles (one guide, search summary).

## Apply
- **Validate before the dialer, and gate on activity, not on `is_valid`:** `is_valid` / Twilio
  `valid` only say the number is well-formed in an assignable range. For a calling list keep
  the line type that fits the role (mobile for executives / field roles, landline or fixed VoIP
  for desk roles) with activity ≥ ~70 as "call", treat 50 (and
  `is_valid: null`) as **Unknown — not half-good** (call last or re-check in a month), drop ≤ 30.
  The 70 / 30 cut-offs are the owner's to calibrate on connect rates; no vendor publishes one.
- **In n8n, keep the lead next to the Trestle answer:** the vendor node replaces the item with the
  API response; put a Merge node (combine by position, or by `pairedItem`) after it, or call
  `phone_intel` from an HTTP Request node with "include input" so lead id and phone stay on the
  item. Filter empty phones *before* the node and enable "continue on fail", or one blank cell
  stops the run.
- **Twilio batch: always send `correlation_id` = your lead id** and check each package's
  `error_code`; never assume answer order equals request order.
- **Reassigned-number check only where consent exists:** Twilio's `reassigned_number` needs the
  consent date (`LastVerifiedDate`); for cold B2B lists without one it has nothing to compare.
- **Measure what a callable number costs:** (finder credits + validation fee) ÷ numbers with
  activity ≥ cut-off — denominator "callable numbers", not "numbers found" (PRACTICE: name the
  denominator).

## Lab impact
- 04: a phone ladder next to the email ladder — `found` (finder + date) → `checked` (validator,
  line type, activity score, date) → `callable` only above the activity cut-off; activity 50 /
  `is_valid: null` / any package `error_code` = `unknown`, never promoted; landline vs mobile
  kept as data for role-based routing. Root ROADMAP 04 item added.
- 14: the call-task engine should consume only `callable` numbers and record the validation date.

## Sources
- https://registry.npmjs.org/n8n-nodes-trestle/-/n8n-nodes-trestle-0.2.8.tgz — opened 2026-11-04 (README, `dist/nodes/Trestle/Trestle.node.js`, `dist/credentials/TrestleApi.credentials.js`)
- https://registry.npmjs.org/twilio/-/twilio-6.2.0.tgz — opened 2026-11-04 (`lib/rest/lookups/v2/phoneNumber.d.ts`, `query.d.ts`, `query.js`, `lib/base/RequestClient.d.ts`)
- https://docs.trestleiq.com/api-reference/phone-validation-api.md — search summary, not opened (ENOTFOUND)
- https://activeprospect.com/integrations/trestle/phone_intelligence — search summary, not opened
- https://www.twilio.com/en-us/lookup/pricing — search summary, not opened
- https://www.clay.com/guides/how-to-find-work-phone-number — search summary, not opened
- https://community.clay.com/x/support/sdxo8q0owy58/waterfall-enrichment-billing-for-phone-numbers-exp — search summary, not opened
- https://dev.to/zackrag/clay-vs-pdl-vs-clearbit-which-actually-wins-on-b2b-contact-enrichment-phone-number-fill-rate-for-36h1 — search summary, not opened
