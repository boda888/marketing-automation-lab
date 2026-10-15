# Practice notes: what the owner builds for clients (anonymised)

Distilled from the owner's own handoff of ~13 Clay / n8n outbound projects (Aug–Oct 2026):
calling lists, signal-based account research, data recovery of old contact bases,
Clay → n8n → CRM → sequencer pipelines, reply routing. Client names, people, numbers per
contract and links are deliberately left out — **never add them to this repo**; use fictional
`.example` companies. This file tells the daily agent what real work looks like so the lab
builds the parts that matter. The lab's direction stays end-to-end GTM automation (FOCUS.md).

## The pipeline that keeps recurring
company source → normalise + dedupe → ICP gate (cheap) → signal research → tier / priority →
find people → role + current-employment + identity check → contact waterfall (email, phone) →
validation → QA + export → CRM upsert + sequencer push → reply classification + routing.
Not every project has every stage, but the order is the cost model: **cheap gates before
paid enrichment**, and research facts kept separate from their commercial interpretation.

Stack seen in real work: Clay (Find Companies / Find People, Claygent web research, AI
columns, waterfalls), n8n, Google Sheets as a hand-off queue, Smartlead, Lemlist, HubSpot,
Attio, a recruiting CRM, Apify (LinkedIn data), Trestle (phone validation), many email
providers in waterfalls (LeadMagic, Prospeo, Findymail, Hunter, Wiza, Enrow, Icypeas,
Dropcontact, FullEnrich…), OpenAI / Claude for classification. Telegram for alerts.

## Failure modes found in real projects → what the lab should make impossible
| Failure seen | Rule the code should enforce | Lab project |
|---|---|---|
| Copy named the wrong country: it read the contact's or HQ country instead of the campaign/asset country | One explicit "campaign geography" field; never derived from HQ or person location | 05, 06 |
| Company passed as "NY" from a provider HQ flag; the real HQ was abroad | Verify HQ from first-party sources; provider fields are hints, not proof | 01, 04 |
| Waterfall providers had empty input mappings → ~45% email coverage, ~75% after the fix | Validate provider inputs before a run; report coverage per provider | 04 |
| A provider got the string `"Unknown"` as a LinkedIn URL → invalid input, wasted credits | Blank, never placeholder strings, in fields fed to paid calls | 04 |
| QA found the right current employer, later nodes still read the old employer → wrong domain/email | One merged, identity-consistent person record feeds every downstream step | 04 |
| A removed input (ICP fit) made the tier model output false "Hold" | Prompt inputs and JSON schema checked together; required inputs asserted | 05 |
| LLM did signal arithmetic wrong (6 of 59 rows) and every personalisation line was rated "Strong" | Arithmetic, thresholds, dedupe in code; LLM only interprets text | 03, 05 |
| Same funding/job event counted twice from two sources | Dedupe signal events by source + id/date before scoring | 03 |
| "Missing data" stored as 0 → read as "stable traffic" | `Unknown` / `Not found in sample` are first-class states, never 0 or "No" | 01, 03 |
| "Found email" reported as "valid"; leftovers after removing invalid called "verified" | Status ladder: found → checked (provider + date) → verified; never promote by arithmetic | 04, 10 |
| Cost per lead quoted with the wrong denominator | Every cost/coverage number names its denominator (input row, qualified company, contact, exported row) | 04, 10 |
| n8n: campaign lookup returned 5 items per lead → 555 items, leads mixed up | Process one lead per item (loop / batch 1); keep stable ids through the flow | 06, 09 |
| n8n: "Stop and Error" on a duplicate killed the whole batch | Duplicate / not-ready = skip + log; only real faults stop or go to a DLQ | 09 |
| Sheet "Row Added" trigger fired on half-filled rows; "Row Updated" re-triggered on its own write | Only hand off fully-ready rows; idempotency key per lead | 06, 09 |
| Campaign id hard-coded; a missing campaign silently fell back | Resolve campaign by explicit name; missing or duplicate name → loud error | 06 |
| "Email exists somewhere in the sequencer" blocked a valid new campaign | Dedupe at campaign level; global suppression is a separate, explicit list | 06 |
| Sheet update matched by company domain → several people of one company overwritten | Update by person key (email / record id), never by company | 06, 08 |
| Synthetic test rows could reach paid providers | Fixtures flagged and gated (`.example` domain → enrichment not allowed) | all |

## Patterns worth turning into code
- **Status vocabularies** used across projects: `Qualified / Review / Disqualified`;
  research `Found / Not Found / Unclear`; signal `Found / Not Found in Sample / Unknown`;
  operational `Review` = do not send. Tiers `Tier 1 / Tier 2 / Tier 3 / Hold` with a
  ≤30-word best signal and ≤45-word reason, instead of an opaque numeric score.
- **Evidence**: every signal/criterion carries the URL that supports *that* claim (not a
  homepage) plus a date and date precision. No evidence → no personalisation line.
- **Signals that recur**: hiring (international / role-specific, from careers page or ATS),
  leadership change, funding (≤12 / 13–24 months), headcount growth (6/12-month %, e.g.
  ≥20% strong, ≤−10% contraction), new office / expansion, projects / RFPs, organic-traffic
  change (raw months kept, missing ≠ decline), tech stack. A job post proves recruiting, not
  a hire; legal entities and tool usage are context, not intent.
- **Contacts**: merge candidates from several sources, dedupe LinkedIn URL → email →
  normalised name + company, keep provenance; one primary + one independent fallback, used
  sequentially, never both at once. Function beats seniority; company size changes who to
  pick (founder at 10–49, functional owner at 50–199, specialist ops at 200+). Location of
  the *person* matters when the meeting is in person.
- **Identity gate** before paid enrichment for old or uncertain records: hard vs supporting
  proofs → grade A/B/C/D/X; review queue with a reason code and next step.
- **Waterfalls**: run only on an explicit condition, stop at the first acceptable result,
  cheap providers first, and record which provider answered.
- **Copy rules** seen in production: short plain sentences, subject < 6 words, first touch
  ~85–95 words, follow-ups shorter, no links/signature/buzzwords, JSON out with the exact
  keys the next node reads, never invent a "fresh hook".
- **Reply classes** used in a real webhook → classifier → alert flow: Interested,
  Maybe/Question, Not Interested, Referral, Out of Office, Unsubscribe, Wrong Person,
  Other/no meaningful reply; plus sentiment, needsHumanAction, confidence, suggested next
  action. The sequencer's own category is context, not truth.
- **Routing for calls**: sequences must not wait on a manual call step that callers cannot
  keep up with; call state (CALL / Done / Callback) lives beside the sequence, not in front.

## How the agent should use this
- When picking or designing a step, prefer one that removes a failure mode from the table.
- Encode these as tests with fictional data: e.g. a fixture where HQ country ≠ asset
  country ≠ contact country, a duplicate event from two sources, a provider fed `"Unknown"`.
- Do not copy prompts or numbers from client work; describe the rule, build it generically.
