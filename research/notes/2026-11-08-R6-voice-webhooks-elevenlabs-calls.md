# R6 · Retell webhook signing and redelivery, ElevenLabs Agents outbound calls (2026-11-08)

## Question
Two open R6 questions: (1) what is the header name on Retell's signed webhooks, what does
Retell do when a delivery fails, and how should a CRM write-back handle that; (2) how does
ElevenLabs Agents (Conversational AI) place outbound calls over the API — single and batch —
and what comes back for idempotency, retries, end reasons, cost and webhook signing?

## Findings
Method: docs.retellai.com, community.retellai.com, hookdeck.com and elevenlabs.io did not
resolve from this run (DNS, the same block as on 2026-10-28), so nothing on those hosts was
opened. I read, without running them, packages from registry.npmjs.org: Retell's official
`retell-sdk` 6.2.19 (published 2026-11-07), the third-party Retell → Twenty CRM sync app
`twenty-retell-sync` 0.1.25 (MikiDev Studio, MIT) and ElevenLabs' official
`@elevenlabs/elevenlabs-js` 2.77.0 (published 2026-11-06, generated from their API spec).
Retell's retry policy below comes from a web search only.

**Retell webhooks**
- **Header:** `x-retell-signature`, value `v=<ms timestamp>,d=<hex>`. The `twenty-retell-sync`
  receiver reads that header, computes HMAC-SHA256 over `rawBody + timestamp` keyed with the
  Retell API key, compares in constant time and rejects anything more than 5 minutes off in
  either direction. This matches the SDK's `verify` read on 2026-10-28. The header name is
  still not in Retell's own SDK, but it is in a working third-party receiver and in the search
  summary of Retell's docs page.
- **Which key signs:** the app's README says Retell signs every webhook with the single API
  key that carries the *Webhook* badge in the dashboard. Any other key still works for API
  calls, but verification then fails with 401 on every event. That is a silent setup failure:
  calls go on, CRM write-back stops.
- **Per-agent override:** `webhook_url` on an agent replaces the account-level webhook for
  that agent. `webhook_events` defaults to `call_started`, `call_ended`, `call_analyzed`.
  `webhook_timeout_ms` defaults to **10,000 ms** (SDK 6.2.19 `agent.d.ts`).
- **Redelivery (search summary, not opened):** a Retell community thread and a Hookdeck guide
  say a non-2xx answer or no answer within 10 s is retried **up to 3 times** with the same
  body and signature, and the payload has **no delivery id**. Backoff timing and what happens
  after the last attempt were not found.
- **Gaps a webhook never fills** (from the app's README): regenerating the analysis in Retell
  sends no webhook, and DNC changes from an SMS opt-out, a carrier opt-out or the dashboard
  send none either. The app therefore runs a pull sync (minimum every 5 minutes) that
  re-reads calls and compares do-not-call flags, newest change wins.
- **How the app stores calls:** `call_ended` and `call_analyzed` both go through one upsert
  keyed on `call_id`, so a redelivered or out-of-order event rewrites the same record instead
  of adding one. Outbound calls from the CRM pass `opportunity_id` in `metadata` so the call
  links back without a phone-number match.

**ElevenLabs Agents outbound calls** (`@elevenlabs/elevenlabs-js` 2.77.0)

| | Single call | Batch call |
|---|---|---|
| Endpoint | `POST /v1/convai/twilio/outbound-call` (also `sip-trunk`, `exotel`, `whatsapp` variants) with `agent_id`, `agent_phone_number_id`, `to_number`, `conversation_initiation_client_data` (dynamic variables), `call_recording_enabled`, `telephony_call_config` | `POST /v1/convai/batch-calling/submit` with `call_name`, `agent_id`, `recipients[]` (`id`, `phone_number`, per-recipient client data), `scheduled_time_unix`, `timezone`, `target_concurrency_limit` |
| Answer | `{success, message, conversation_id?, call_sid?}`: a `success` flag in the body, and both ids optional | batch id; `GET /batch-calling/{id}` lists recipients with status `pending / dispatched / initiated / in_progress / completed / failed / cancelled / voicemail` |
| Idempotency | **no idempotency field** on either request (no match for "idempotent" in any convai request type) | same; batch `retry` re-dials "failed and no-response recipients" |
| SDK retries | **every request, including call creation, retried 2× by default on 408 / 429 / 5xx** (`requestWithRetries.js`, 1 s initial delay, honours `Retry-After`) | same |
| Ringing / voicemail | `telephony_call_config.ringing_timeout_secs`; Twilio carrier AMD (`twilio_machine_detection`) delivers its verdict as a separate `answering_machine_detection` webhook, not in the post-call webhook, and Twilio bills it separately | batch status `voicemail` per recipient |

- **What comes back after the call:** conversation `status` is `initiated / in-progress /
  processing / done / failed`. Metadata has `call_duration_secs`, `queue_wait_secs`
  (excluded from billed time), `termination_reason` as a **free string** (no enum in the
  spec), `error`, `cost` (no unit in the spec; it sits next to `cost_fiat`, so it is likely
  credits) and **`cost_fiat` in USD** (LLM + platform). Evaluation results are `success /
  failure / unknown`.
- **Webhook events:** `transcript`, `audio`, `call_initiation_failure`,
  `answering_machine_detection` and others. A failed dial arrives as its own
  `call_initiation_failure` event, not as a transcript.
- **Webhook signing:** the header value is `t=<unix seconds>,v0=<hex>` with HMAC-SHA256 over
  `"<t>.<raw body>"` and a webhook secret (not the API key). The SDK's `constructEvent`
  **accepts timestamps up to 30 minutes old and any future timestamp**, and compares the
  digest with `!==`, not in constant time. The header name is not in the SDK.
- **DNC / consent:** no do-not-call or consent field on any convai request type in 2.77.0
  (grep for `dnc`, `do_not_call`). Suppression has to happen before the call is created.

## Apply
- **Retell → CRM in n8n:** Webhook node with "Raw Body" on, Code node that verifies
  `x-retell-signature` (HMAC-SHA256 of raw body + timestamp, API key **with the Webhook
  badge**, ±5 min), then answer 200 at once (Respond to Webhook first, work after) because
  Retell retries after 10 s. Upsert the CRM call record on `call_id`, so the 3 retries and the
  later `call_analyzed` update one row. Add a daily alert when no webhook arrived but calls
  were placed: a wrong key fails every event with 401 and nothing else looks broken.
- **Run a pull sync next to Retell webhooks** (every 15–60 min, `list-calls` since the last
  run): it catches regenerated analyses and DNC flags set by SMS / carrier opt-outs, which
  never send a webhook. Copy the DNC flag into your global suppression list, not only CRM.
- **ElevenLabs outbound from n8n:** turn Retry On Fail **off** on the outbound-call HTTP node
  (or set `maxRetries: 0` in code): there is no idempotency key and the SDK retries creation
  on 5xx / timeouts, so a slow answer can become two calls. Branch on `success`, not on HTTP
  200, and store `conversation_id` + `call_sid`; a 200 without them is `outcome_unknown`,
  check the conversation list before re-dialling.
- **ElevenLabs batch calls:** run the suppression / consent filter before `submit` *and*
  before `retry` — the batch has no DNC field and `retry` calls every failed and no-response
  recipient again. Put your lead id in `recipients[].id` so results map back.
- **ElevenLabs webhook check:** verify `t=…,v0=…` yourself in a Code node with a constant-time
  compare and a 5-minute window instead of copying the SDK's 30-minute, past-only check, and
  dedupe on `conversation_id` + event type. Log `cost_fiat` per conversation for cost per
  connected call.

## Lab impact
Project 14 (calls): the voice adapters must treat ElevenLabs like Vapi (no idempotency key,
vendor SDK retries creation → our own key + lookup before any redial), treat a 200 with
`success: false` or without `conversation_id` as `outcome_unknown`, map ElevenLabs
`termination_reason` (free text) and batch recipient status onto the shared outcome
vocabulary with an `unmapped` fallback, and take cost from `cost_fiat` (USD) next to Retell
cents. Webhook intake: Retell `x-retell-signature` ±5 min, ElevenLabs `t=,v0=` with our own
5-minute window, upsert on call / conversation id, plus a pull reconcile for events vendors
never send. Root ROADMAP 14 items edited.

## Sources
- https://registry.npmjs.org/retell-sdk/-/retell-sdk-6.2.19.tgz — opened 2026-11-08 (`CHANGELOG.md` 6.2.15–6.2.19, `resources/agent.d.ts` webhook fields)
- https://registry.npmjs.org/@retell-ai/retell-cli/-/retell-cli-0.54.0.tgz — opened 2026-11-08 (no webhook header or retry text found)
- https://registry.npmjs.org/twenty-retell-sync/latest and its tarball `twenty-retell-sync-0.1.25.tgz` — opened 2026-11-08 (README, `src/logic-functions/retell-webhook.ts`, bundled `src/lib/signature.ts`)
- https://registry.npmjs.org/@elevenlabs/elevenlabs-js/-/elevenlabs-js-2.77.0.tgz — opened 2026-11-08 (`twilio` and `batchCalls` clients and requests, `core/fetcher/requestWithRetries.js`, `wrapper/webhooks.js`, types `TelephonyCallConfigInput`, `TwilioOutboundCallResponse`, `BatchCallRecipientStatus`, `ConversationHistoryMetadataCommonModel`, `WebhookEventType`, `EvaluationSuccessResult`)
- https://registry.npmjs.org/-/v1/search?text=elevenlabs%20outbound%20call&size=10, https://registry.npmjs.org/-/v1/search?text=retell%20webhook&size=10 — opened 2026-11-08 (package discovery)
- https://docs.retellai.com/features/secure-webhook, https://community.retellai.com/t/duplicate-webhook-payloads/2573.md, https://hookdeck.com/webhooks/platforms/guide-to-retell-webhooks-features-and-best-practices.md — tried 2026-11-08, DNS / no connection, not opened; their content above is a **search summary, not opened** (10 s timeout, 3 retries, no delivery id)
