# R6 · Bland and Synthflow agents: call creation, DNC / consent, outcomes, webhooks (2026-11-18)

## Question
Open R6 question: how do Bland and Synthflow place outbound AI calls over the API (and from
n8n), what do they offer for idempotency, do-not-call and consent, how is the outcome of a
call reported, and how are webhooks signed? Second, partial: what does the FCC's 2024
ruling on AI voices (FCC 24-17) mean for automated B2B calls?

## Findings
Method: docs.bland.ai, docs.synthflow.ai, docs.fcc.gov and law-firm pages did not resolve
from this run (DNS, same block as earlier R6 runs), so nothing on those hosts was opened. I
read, without running them, packages from registry.npmjs.org: Bland's n8n node
`@blandsdk/n8n-nodes-bland` 0.3.0 (published 2026-09-04; npm maintainers all `@bland.ai`,
author finn@bland.ai, repo under `CINTELLILABS`), Bland's official CLI `bland-cli` 0.10.1
(2026-09-13), Synthflow's n8n node `@synthflow-ai/n8n-nodes-synthflow` 0.2.3 (2025-11-21;
maintainers and author `@synthflow.ai`) and a third-party Synthflow integration
`@slates-integrations/synthflow-ai` 0.2.2 (2026-10-06, with a `docs/SPEC.md` that cites
docs.synthflow.ai). Nothing was called live.

**Bland**

| | What the vendor packages show |
|---|---|
| Call creation | `POST /v1/calls` with `phone_number` (E.164) and exactly one of `task` (inline prompt), `pathway_id` or agent id; optional `from`, `voice`, `first_sentence`, `max_duration` (minutes), `amd`, `voicemail_message`, `wait_for_greeting`, `record`, `request_data` (prompt variables), `metadata` (returned on the call and in webhooks), `start_time` + `timezone` (scheduled call), `transfer_phone_number`, `webhook`, `persona_id` |
| Idempotency | **no idempotency field** in the node's Send operation or the CLI's `SendCallParams`; the CLI's HTTP client has no retry logic (nothing to switch off), but n8n Retry On Fail would re-send creation |
| Outcome | call object: `completed` (bool), `queue_status`, `status`, `answered_by` (free string or null), `error_message`, `call_length`, `batch_id`, `summary`, `analysis`, `variables`, `recording_url`, transcripts. No end-reason enum and **no price / cost field in the CLI types** (the API may return more than the CLI types). The CLI's own `waitForCallCompletion` polls `GET /v1/calls/{id}` every 2 s until `completed` |
| Batches / campaigns | `GET /v2/batches/{id}/logs`; batch: `status`, `total_calls`, `completed_calls`, `failed_calls`; campaigns create / get / list / stop under `/v1/campaigns` |
| DNC | **block rules**: `POST /v1/blocked_numbers` with up to **10 E.164 numbers per request**, `is_global` (all inbound numbers) or one `inbound_number` (the two are exclusive), `reason`; list / get / edit / delete. The descriptions speak of inbound numbers; whether a global block also stops *outbound* dials is not stated in either package |
| Consent / disclosure | **guard rails** `POST /v1/guard_rails` with preset types `tcpa:ai_disclosure`, `tcpa:recording_disclosure`, `tcpa:opt_out` or `custom` (detection prompt), action `end_call` or `transfer`, attached to a `PERSONA`, `PATHWAY` or `INBOUND` source. A call sent with an inline `task` has neither, so on my reading a guard rail cannot cover it — use a persona or pathway for outbound |
| Webhooks | one **account default webhook** (`/user/getCurrentWebhook`, `/user/updateCurrentWebhook`) plus per-call `webhook`; post-call report vs in-call events (`category`: call, citations, dynamic_data, latency, queue, tool, webhook). Post-call payloads can be re-sent (`/v1/postcall/webhooks/resend`) and read back by call id. Signature: header **`X-Webhook-Signature`** = hex HMAC-SHA256 of the **raw body** with a webhook signing secret; the n8n trigger compares in constant time. **No timestamp is signed**, so a captured request replays forever unless you dedupe; verification is **off by default** in the trigger |
| n8n gotchas | the trigger warns that an account has only one default webhook and that Bland **cannot clear it once set**; "Manage Account Default Webhook" overwrites it for every call that has no own `webhook` |

**Synthflow**

| | What the packages show |
|---|---|
| Call creation | `POST https://api.synthflow.ai/v2/calls` with `model_id` (agent), `phone`, `name`; optional `custom_variables` (key / value list), `prompt`, `greeting`, `lead_email`, `lead_timezone`, caller id. Answer (third-party schema): `call_id`, `eta` (seconds to start), `answer` (status message) |
| Regions | global `api.synthflow.ai/v2`, US `api.us.synthflow.ai/v2`, EU `api.eu.synthflow.ai/v2` (third-party SPEC.md); the vendor's n8n node hard-codes the global host |
| Idempotency | none in either package |
| Webhook URL | the vendor node (2025-11) still sends `external_webhook_url` per call; the 2026-10 third-party package calls it a **legacy field the current call API does not accept** and sets the post-call URL on the **agent** (`external_webhook_url` on `/v2/assistants`). Treat per-call webhook URLs as unreliable; configure it on the agent |
| Outcome | `GET /v2/calls/{id}`; list filter `call_status` "e.g. completed, failed, busy" (free string); `duration_min` / `duration_max`; time filters in epoch ms. No cost field in either package; usage is a separate `/analytics/` export (ranges ≤ 120 days) |
| DNC / consent | no DNC or blocklist operation in either package. The only consent field is the agent's `consent_message` — **"to ask the customer for consent to record the call"**, i.e. recording consent, not consent to be called |
| Webhook signing | nothing about signing in either package (not found ≠ absent) |
| Concurrency | sub-account `concurrency` limit (agency accounts) |

**Across Retell, Vapi, ElevenLabs, Bland, Synthflow** (with the 2026-10-28 and 2026-11-08
notes): only Retell has a native idempotency key; only Retell (`honor_internal_dnc`), Vapi
(`campaign.predial`) and Bland (block rules, guard rails) have a DNC / consent hook; none of
them knows *your* suppression list. Outcome strings differ per vendor and Bland / Synthflow
use free strings, so an `unmapped` fallback is required.

**FCC 24-17 (search summary, not opened):** the FCC's Declaratory Ruling of 2024-02-08
(CG Docket 23-362) holds that AI-generated voices, including voice cloning, are "artificial"
voices under the TCPA, so such calls need the called party's prior express consent. Whether
and how that applies to calls to business lines / B2B was **not** answered by any result;
the ruling text and 47 CFR 64.1200 (DNS-blocked) still need reading. Bland's preset guard
rails (AI disclosure, recording disclosure, opt-out) show which in-call duties the vendor
expects customers to care about; they are not legal advice and not proof of compliance.

## Apply
- **Bland from n8n:** call with a `persona_id` or `pathway_id`, not an inline prompt, and
  attach the three `tcpa:*` guard rails to that persona / pathway with `end_call` on opt-out;
  put your lead id and call-task id in `metadata` so the webhook maps back without a phone
  match. Keep Retry On Fail **off** on Send Call (no idempotency key); on a timeout, list
  recent calls to that number before dialling again.
- **Bland webhook:** turn **Verify Signature on** in the Bland Trigger and set the signing
  secret on the credential (default is off), then dedupe on `call_id` + event type because
  the signature has no timestamp. Do **not** tick "Manage Account Default Webhook" on a
  shared account — it replaces the one default webhook for every call and Bland cannot
  clear it; pass a per-call `webhook` instead.
- **Synthflow:** set the post-call webhook on the agent (`/v2/assistants/{id}`), not on the
  call; pick the regional base URL (EU workspace → `api.eu.synthflow.ai`) in an HTTP Request
  node if the vendor node's global host fails. Its `consent_message` covers recording only:
  run your own consent / DNC filter before `/v2/calls`.
- **One suppression list for all voice vendors:** filter in n8n / Clay before any call
  creation (none of the five vendors reads it); when a call ends with an opt-out, push the
  number into Bland block rules (≤ 10 per request) *and* your global email / LinkedIn
  suppression.
- Measure: share of calls whose outcome string hit your `unmapped` bucket per vendor, and
  calls created vs webhooks received per day (a missing or unsigned webhook shows up there).

## Lab impact
Project 14 adapters: add Bland and Synthflow shapes — no idempotency key on either (own key
+ lookup before any redial), Bland `completed` / `answered_by` / `error_message` and
Synthflow `call_status` free strings mapped with an `unmapped` fallback, no cost on either
(cost stays `None`, never 0). Webhook intake: Bland `X-Webhook-Signature` (HMAC-SHA256 hex
of the raw body, no timestamp) → mandatory dedupe on call id + event; Synthflow unsigned as
far as known → shared-secret path / header plus re-read of the call by id before writing.
Consent gate: recording consent (Synthflow `consent_message`) is not call consent. Root
ROADMAP 14 adapter item edited.

## Sources
- https://registry.npmjs.org/@blandsdk/n8n-nodes-bland/-/n8n-nodes-bland-0.3.0.tgz — opened 2026-11-18 (`nodes/Bland/resources/call/send.js` and `call/index.js`, `generated/blockedNumber/createBlockRules.js`, `generated/batch`, `generated/campaign`, `nodes/BlandTrigger/BlandTrigger.node.js`, `credentials/BlandApi.credentials.js`, `package.json`)
- https://registry.npmjs.org/@blandsdk/n8n-nodes-bland — opened 2026-11-18 (maintainers, publish date)
- https://registry.npmjs.org/bland-cli/-/bland-cli-0.10.1.tgz — opened 2026-11-18 (`types/api.d.ts` Call / CallDetail / SendCallParams / Batch / GuardRail, `lib/callWait.js`, `lib/api.js`, `commands/guard.js`)
- https://registry.npmjs.org/@synthflow-ai/n8n-nodes-synthflow/-/n8n-nodes-synthflow-0.2.3.tgz — opened 2026-11-18 (`nodes/Synthflow/Synthflow.node.ts`, `package.json`) and https://registry.npmjs.org/@synthflow-ai/n8n-nodes-synthflow (maintainers)
- https://registry.npmjs.org/@slates-integrations/synthflow-ai/-/synthflow-ai-0.2.2.tgz — opened 2026-11-18 (third-party; `docs/SPEC.md`, `dist/index.js` make_call schema)
- https://registry.npmjs.org/-/v1/search?text=bland%20ai&size=10 and `?text=synthflow`, `?text=aircall`, `?text=kixie` — opened 2026-11-18 (package discovery; no Aircall API client or Kixie package found)
- web search "FCC 24-17 declaratory ruling AI-generated voice TCPA …" — 2026-11-18, **search summary, not opened** (ruling date, docket, AI voice = artificial, prior express consent; nothing on business lines)
- web search "Bland AI API send call do_not_call voicemail webhook call_id docs" — 2026-11-18, **search summary, not opened** (postcall webhook create / get / resend endpoints)
- docs.fcc.gov/public/attachments/FCC-24-17A1.pdf, www.mayerbrown.com (FCC 24-17 article), docs.bland.ai, docs.synthflow.ai, developer.aircall.io, www.ecfr.gov, www.law.cornell.edu, ico.org.uk, www.legislation.gov.uk — tried 2026-11-18, DNS / no connection, not opened
