# R6 · Retell vs Vapi vs JustCall: placing calls, outcomes, cost and consent over the API (2026-10-28)

## Question
When n8n (or an agent) starts calls from engagement signals: how does an outbound call get
placed through an AI voice agent (Retell, Vapi) or a dialer (JustCall), what comes back as
the call outcome, where cost per call and a do-not-call request show up, and what stops one
lead from being dialled twice?

## Findings
Method: docs.vapi.ai and docs.retellai.com did not resolve from this run (`ENOTFOUND`), and
the proxy refused the other vendor hosts (403 on CONNECT). So I read, without running them,
the vendors' own packages from registry.npmjs.org: **Retell's** official TypeScript SDK
(`retell-sdk` 6.2.14, published 2026-10-27, generated from Retell's OpenAPI), **Vapi's**
server SDK (`@vapi-ai/server-sdk` 3.0.0, maintainer on vapi.ai) and **JustCall's** n8n node
(`n8n-nodes-justcall` 1.0.4, author `dev@justcall.io`). Bland (`@blandsdk/n8n-nodes-bland`
0.3.0, publisher on bland.ai, repo under a third-party org) and Synthflow
(`@synthflow-ai/n8n-nodes-synthflow` 0.2.3) were downloaded but only skimmed. Aircall
publishes UI and embed packages only, with no API client, so it was not covered. Prices and
the legal summary below come from web searches only. I did not open the pages behind them.

**Placing a call:**

| | Retell (`retell-sdk`) | Vapi (`@vapi-ai/server-sdk`) | JustCall (vendor n8n node) |
|---|---|---|---|
| Endpoint | `POST /v2/create-phone-call`: `from_number`, `to_number` (E.164), `override_agent_id` / `agent_override`, `retell_llm_dynamic_variables` (string map), `metadata` (yours, echoed on the call) | `POST /call`: `assistantId` / `squadId` / `workflowId`, `phoneNumberId`, `customer` or `customers[]` (batch), `assistantOverrides` (template variables), `schedulePlan {earliestAt, latestAt}` | AI agent: `POST /v2.1/voice-agents/calls` with `ai_agent_id`, `contact_number`, `dynamic_variables`, **`has_consent` (default false)**. Human dialer: `POST /v2.1/sales_dialer/campaigns/contact` (campaign + phone or contact id, custom fields) |
| Double-dial guard | `idempotency_key`: retrying with the same key for **up to one hour** creates no second call. SDK **does not retry** this call (`maxRetries: 0`, changelog 6.2.11 on 2026-10-23: "disable retries for outbound creation methods"), while other calls retry twice | **no idempotency field**; the SDK's `calls.create` uses the default **2 retries on 408 / 429 / 5xx**, so a slow `/call` that timed out can be retried into a second dial | none found in the node |
| DNC / consent | `honor_internal_dnc` (default **false**): if true, the call is rejected with 400 when the contact is marked `do_not_call`. Agent setting `enable_dnc_detection` (default false): the agent confirms a stop request once, then ends with `user_requested_dnc` and marks the contact `do_not_call` | Campaigns: optional **`campaign.predial`** webhook before every contact is dialled. Your server returns `{eligible: true}` to dial. A missing / non-boolean answer, an error, a timeout or an unreachable server records a pre-dial failure and **the call is not placed** (fails closed) | `has_consent` flag on AI calls; what JustCall does with it is not documented in the node |
| Scheduling | batch calls: `call_time_window` (minutes since local midnight, no cross-midnight windows, `day[]`, IANA timezone, **default `America/Los_Angeles`**), `trigger_timestamp`, `reserved_concurrency` for inbound | `schedulePlan` per call; campaigns | Sales Dialer campaigns run in the app |
| Destinations | numbers bought from Retell dial **US numbers only**; imported / SIP numbers otherwise | Twilio / Vonage / SIP numbers | JustCall numbers |

**What comes back:**
- **Retell** `call_status` takes the values `registered / not_connected / ongoing / ended / error`.
  `disconnection_reason` has 39 values, including `dial_no_answer`, `dial_busy`,
  `voicemail_reached`, `ivr_reached`, `user_requested_dnc`, `user_requested_callback`,
  `invalid_destination`, `marked_as_spam`, `concurrency_limit_reached`, `credit_exhausted`,
  `budget_reached` and a family of `error_*`. `call_analysis` holds `call_successful`,
  `call_summary`, `in_voicemail`, `user_sentiment` (`Negative / Positive / Neutral /
  Unknown`) and `custom_analysis_data` (your post-call schema). `call_cost` holds
  `combined_cost` **in cents**, `product_costs[]` (cents per second for each product,
  transfer legs flagged) and `total_duration_seconds`. Webhook events: `call_started`,
  `call_ended`, `call_analyzed`, `transcript_updated` and `transfer_*`. The analysis arrives
  in a later event than `call_ended`. Signature format: `v=<ms timestamp>,d=<hex>` =
  HMAC-SHA256 of the raw body + timestamp, keyed with the API key. The SDK's `verify`
  rejects anything older than 5 minutes. The header name is not in the SDK.
- **Vapi** has `endedReason` with about 600 values, most of them provider and pipeline
  errors. The outcome ones are `customer-did-not-answer`, `customer-busy`, `voicemail`,
  `customer-ended-call`, `assistant-ended-call*`, `silence-timed-out`,
  `exceeded-max-duration`, `twilio-reported-customer-misdialed`,
  `sip-outbound-unallocated-number` and `manually-canceled`. The cost breakdown has
  `transport`, `stt`, `llm`, `tts`, `vapi`, `total` plus token / TTS-character counts.
  Server messages include `status-update`, `end-of-call-report`, `transcript`,
  `tool-calls` and `transfer-update`.
- **JustCall** has trigger events for call initiated / answered / completed / missed /
  updated, incoming call, SMS and voicemail. A call can be updated with `notes`,
  `disposition` and `rating` (`PUT /v2.1/calls/{id}`), and there are reads for call
  journey, recording, voice-agent data and JustCall AI summaries. **The webhook signature
  covers `secret|url-encoded webhook_url|event_type|timestamp`, not the body**
  (`x-justcall-signature`, `x-justcall-request-timestamp`), and the node does not check how
  old the timestamp is. A valid signature proves who sent the event, not what the body
  says. Auth: `Authorization: <key>:<secret>`, base `https://api.justcall.io`.

**Pricing (web-search summaries only, 2026-10-28):** Retell lists voice infra at about
$0.055 / min. LLM, TTS and telephony are billed on top, and blogs put the total at about
$0.07–0.31 / min. 20 concurrent calls are included, with extra lines reportedly $8 / month
each. Numbers cost about $2 / month. Vapi charges a $0.05 / min platform fee, with models
and telephony billed at cost (about $0.10–0.30 / min all in, per blogs). Self-serve
includes 10 concurrent lines at $10 / line / month. One July 2026 page claims the entry
tier dropped to 4 lines, which is unconfirmed. Because both SDKs return cost per call,
measure it from the call objects instead of relying on these figures.

**Consent (search summary, not legal advice):** in February 2024 the FCC ruled that
AI-generated voices count as "artificial or prerecorded voice" under the TCPA. That means
prior express **written** consent for marketing calls to residential or mobile numbers.
The search turned up no ruling on B2B calls to business lines, and one vendor blog claims
written consent applies to any commercial AI call. I treat this as open. UK / EU rules were
not researched this run.

## Apply
- **Never let a retry dial twice.** Retell: send a stable `idempotency_key` (for example
  `lead_id + call_task_id`), which holds for an hour. Vapi: set `maxRetries: 0` on
  `calls.create`, or in n8n turn Retry On Fail off on that HTTP node. Record the call as
  `outcome_unknown` on a timeout and look it up with `GET /call` before trying again.
- **Turn on the vendor's DNC switches explicitly:** Retell `honor_internal_dnc: true` on
  every create, plus `enable_dnc_detection: true` on the agent. Push `user_requested_dnc`
  into your global suppression list (CRM + sequencer + LinkedIn), not only Retell's contact
  list. In Vapi campaigns, use `campaign.predial` as the consent / suppression gate. It fails
  closed, so a slow n8n webhook skips calls instead of placing them. Watch the pre-dial
  failure count.
- **Route on the end reason, not on "call ended":** Retell `dial_no_answer` / `dial_busy` /
  `voicemail_reached` and Vapi `customer-did-not-answer` / `customer-busy` / `voicemail` →
  callback task, sequence keeps going. `user_requested_callback` → task with the time.
  `invalid_destination` / `sip-outbound-unallocated-number` / misdialed → bad-phone data fix,
  no retry. `marked_as_spam` → stop that caller number. `credit_exhausted` /
  `budget_reached` / `concurrency_limit_reached` → alert, not a lead outcome.
- **CRM write-back from `call_analyzed`, not `call_ended`,** if you need the summary,
  sentiment or custom fields. Log `call_cost.combined_cost` (cents) and Vapi `costs.total`
  for each call to get real cost per connected conversation.
- **JustCall into n8n:** keep the node's signature check on, but before writing to the
  CRM re-read the call (`GET /v2.1/calls/{id}`) and dedupe on call id + event, because the
  signature does not cover the body. Set `has_consent` on AI calls deliberately from your
  consent field, not from the default `false`.

## Lab impact
Project 14 (calls): the call-task engine and voice adapters must (1) create calls with
vendor retries off and an idempotency key (Retell's native key, otherwise our own key plus
a lookup before any retry), (2) map Retell `disconnection_reason` and Vapi `endedReason`
onto one outcome vocabulary (connected / no answer / busy / voicemail / callback / DNC /
bad number / provider fault → `outcome_unknown`), (3) block any automated call unless
consent and suppression checks pass (fail closed, like Vapi's pre-dial), and (4) take cost
in cents from the call object. A DNC heard on a call feeds the 06 suppression list. Root
ROADMAP 14 items edited.

## Sources
- https://registry.npmjs.org/retell-sdk/-/retell-sdk-6.2.14.tgz — opened 2026-10-28 (`resources/call.d.ts`, `call.js`, `agent.d.ts`, `batch-call.d.ts`, `lib/webhook_auth.mjs`, `client.js`, `CHANGELOG.md`)
- https://registry.npmjs.org/@vapi-ai/server-sdk/-/server-sdk-3.0.0.tgz — opened 2026-10-28 (`CreateCallDto`, `SchedulePlan`, `CostBreakdown`, `ServerMessageEndOfCallReportEndedReason`, `AssistantServerMessagesItem`, `ServerMessageCampaignPredial`, `ServerMessageResponseCampaignPredial`, `CampaignPredialPlan`, `calls/client/Client.js`, `core/fetcher/requestWithRetries.js`)
- https://registry.npmjs.org/n8n-nodes-justcall/-/n8n-nodes-justcall-1.0.4.tgz — opened 2026-10-28 (README, `GenericFunctions.js`, `handlers/*`, `utils/webhookHelpers.js`, `descriptions/AiVoiceAgentDescription.js`)
- https://registry.npmjs.org/@blandsdk/n8n-nodes-bland/-/n8n-nodes-bland-0.3.0.tgz — opened 2026-10-28 (package.json, credentials base URL only)
- https://registry.npmjs.org/-/v1/search?text=aircall — opened 2026-10-28 (only UI / embed packages from aircall.io)
- https://docs.vapi.ai/pricing, https://docs.retellai.com/api-references/create-phone-call — tried 2026-10-28, DNS ENOTFOUND, not opened; docs.retellai.com, retellai.com, vapi.ai, bland.ai, developer.aircall.io, developer.justcall.io, synthflow.ai, elevenlabs.io, fcc.gov — proxy 403, not opened
- https://www.retellai.com/it/pricing, https://krispcall.com/general/retell-ai-pricing/, https://www.cloudtalk.io/retell-ai-pricing/ — search summary, not opened (Retell pricing)
- https://www.vapi.ai/pricing, https://flexprice.io/pricing-index/vapi, https://emitrr.com/blog/vapi-pricing/ — search summary, not opened (Vapi pricing)
- https://www.orrick.com/en/Insights/2024/02/Did-the-FCC-Really-Ban-AI-Generated-Voice-Calls-No-But-Restrictions-Apply, https://www.kelleydrye.com/viewpoints/blogs/ad-law-access/fcc-declares-tcpa-restrictions-apply-to-ai-generated-calls — search summary, not opened (FCC ruling on AI voices)
