# R7 · CRM webhooks: how HubSpot, Attio and Pipedrive sign, retry and switch off deliveries (2026-11-10)

## Question
The lab's next 08 step is `POST /v1/webhooks/{crm}` (merge events → shared IdMap). Before
writing it: how does each CRM prove a delivery is genuine, what does it retry and for how
long, and what silently stops deliveries? Answers the R7 open question "HubSpot webhooks and
their retry policy" and adds Attio and Pipedrive.

## Findings
Method: developers.hubspot.com, attio.mintlify.app, pipedrive.readme.io and webhookrelay.com
did not resolve from this run (`ENOTFOUND`). I read, without running them, the vendor
packages on registry.npmjs.org (`@hubspot/api-client` 14.1.3, `pipedrive` 36.3.1), the
third-party `attio-ts-sdk` 2.3.3 (types generated from Attio's OpenAPI), and n8n's
`HubspotTrigger.node.ts` / `PipedriveTrigger.node.ts` on GitHub master. Retry policies come
from web-search summaries only (labelled below).

**HubSpot (app webhooks, `/webhooks/v3/{appId}`):**
- Signing, from the SDK's `Signature` util (`lib/src/utils/signature.js`):
  - v1: `sha256_hex(client_secret + body)`; v2: `sha256_hex(client_secret + method + url + body)`;
  - **v3: `base64(HMAC-SHA256(client_secret, method + url + body + timestamp))`**, rejected
    when `now - timestamp > 300,000 ms`. The secret is the **app's client secret**, not a
    private-app token.
  - The SDK compares with `===` (not constant-time) and only checks age in one direction, so a
    timestamp in the future passes. A server should reject both directions.
  - Header names `X-HubSpot-Signature-v3` + `X-HubSpot-Request-Timestamp` (ms): search
    summary, the SDK does not name headers. Third-party posts: sign the exact URI HubSpot
    called; a `+` in the base64 header turned into a space by a proxy breaks validation.
- Payload: a JSON **array** of events; the SDK's own v1 fixture shows each event carries
  `eventId`, `subscriptionId`, `portalId`, `occurredAt`, `subscriptionType`,
  **`attemptNumber`**, `objectId`, `changeSource`, `changeFlag`, `appId`.
- Settings: only `targetUrl` and `throttling.maxConcurrentRequests` (SDK `SettingsResponse`).
- Retries (search summary, not opened): up to 10 retries spread over 24 h with randomised
  delay (2018 changelog title + legacy error-handling page); a HubSpot rep on the community
  forum says 429 / 5xx are retried (429 honours `Retry-After`), 400 / 401 / 403 / 404 / 405
  are not. So a redelivery is the **same `eventId` with a higher `attemptNumber`**.
- **n8n HubSpot Trigger** (`HubspotTrigger.node.ts`):
  - verifies **only v1**, over `JSON.stringify(parsed body)`, not the raw body. Any
    serialisation difference makes a genuine event fail, and a failed or missing signature
    returns `{}` (n8n answers without starting the workflow). HubSpot sees a delivered event
    and does not retry: **the event is dropped silently**;
  - on activate it `PUT`s app settings with `maxConcurrentRequests` 5 by default;
  - on **deactivate it deletes every subscription of the developer app and the app's webhook
    settings**, not only its own. One app shared by two workflows or systems loses all its
    webhooks when either one is switched off.

**Attio (`/v2/webhooks`, from `attio-ts-sdk` 2.3.3 types):**
- Create returns `secret`: "the key which is used to sign the webhook events. This is only
  shown when setting up the webhook initially." Store it at creation, or delete and re-create.
- `status: active | degraded | inactive`: "If a webhook remains in the degraded state for 7
  days, it will be marked inactive". Inactive webhooks receive nothing.
- Subscriptions take a per-event `filter` (`$and` / `$or` of `equals` / `not_equals`).
  Events include `record.merged` and `list-entry.*`.
- Signature (search summary, not opened): header `Attio-Signature` (also `X-Attio-Signature`),
  hex HMAC-SHA256 of the **raw body** keyed with the secret as-is, no timestamp. A Ruby gem
  signs `timestamp.payload` instead, so sources conflict. No official retry policy found.

**Pipedrive (`/v1/webhooks`, from `pipedrive` 36.3.1):**
- No signature. Only optional HTTP Basic (`http_auth_user` / `http_auth_password`) sent on
  every delivery. **n8n's Pipedrive Trigger defaults Incoming Authentication to None**, so the
  public URL accepts anyone's POST; with Basic set it answers 401 / 403.
- `user_id`: "each webhook event is checked against a user's permissions … If you want to
  receive notifications for all events, please use a top-level admin user's `user_id`".
  A webhook made with a sales rep's token misses every object that rep can't see.
- `subscription_url` "must not redirect to another link". v2 is the default since
  2025-03-17 (field description, links the changelog).
- `GET /v1/webhooks` returns `is_active`, `remove_time`, **`remove_reason`**,
  `last_delivery_time`, `last_http_status`, which is enough for a watchdog.
- Retries / auto-removal (search summary, not opened): any non-2xx is a failure and starts the
  retry policy. The community asks about deletion after 3 days without a 2xx. The exact
  schedule was not confirmed.

| | HubSpot | Attio | Pipedrive |
|---|---|---|---|
| Proof of origin | HMAC v3 (client secret, method + URI + body + ts, base64, 5 min) | HMAC of raw body (header name: search summary) | HTTP Basic only (optional) |
| Redelivery id | `eventId` + `attemptNumber` | `event_id` (search summary) | none in what was read |
| What stops deliveries | n8n trigger deactivate wipes all app subscriptions | `degraded` 7 days → `inactive` | `is_active` false + `remove_reason`; webhook owner's permissions filter events |

## Apply
- **HubSpot → n8n:** don't use the HubSpot Trigger for anything shared. Use a Webhook node
  with **Raw Body** on, check `X-HubSpot-Signature-v3` in a Code node (base64 HMAC of
  `POST` + exact public URL + raw body + timestamp, reject if older than 5 min *or* in the
  future), answer 200 fast, then dedupe on `eventId`. A redelivery is the same `eventId`
  with a higher `attemptNumber`. Keep one developer app per consumer, because deactivating
  the n8n trigger deletes every subscription of the app.
- **Pipedrive:** always set Basic auth on the webhook (n8n defaults to None), create it
  with an admin user's `user_id`, and run a daily `GET /v1/webhooks` check that alerts on
  `is_active` false, a `remove_reason`, or a `last_http_status` other than 2xx.
- **Attio:** save the webhook `secret` when you create it (it's never shown again), and
  alert when `GET /v2/webhooks` shows `degraded`. After 7 days it becomes `inactive` and
  merges / updates stop arriving without any error on your side.

## Lab impact
- 08's planned `POST /v1/webhooks/{crm}` gets per-CRM checks:
  - HubSpot v3: raw body, exact URL, ±5 min in both directions, constant-time compare;
  - Attio: HMAC of the raw body, header configurable until a live delivery confirms it;
  - Pipedrive: Basic auth.
- Dedupe HubSpot on `eventId`, never on `attemptNumber`.
- 09 watchdog: add Pipedrive `/v1/webhooks` status and Attio `degraded` checks next to the
  Instantly one.

## Sources
- https://registry.npmjs.org/@hubspot/api-client/-/api-client-14.1.3.tgz (`lib/src/utils/signature.js`, `test/unit/signature.spec.js`, `codegen/webhooks/models/SettingsResponse.d.ts`, `ThrottlingSettings.d.ts`) — opened 2026-11-10
- https://registry.npmjs.org/attio-ts-sdk/-/attio-ts-sdk-2.3.3.tgz (`dist/index.d.mts`, `/v2/webhooks` types: `secret`, `status`, `filter`) — opened 2026-11-10
- https://registry.npmjs.org/pipedrive/-/pipedrive-36.3.1.tgz (`versions/v1/models/add-webhook-request.d.ts`, `base-webhook.d.ts`) — opened 2026-11-10
- https://raw.githubusercontent.com/n8n-io/n8n/master/packages/nodes-base/nodes/Hubspot/HubspotTrigger.node.ts — opened 2026-11-10
- https://raw.githubusercontent.com/n8n-io/n8n/master/packages/nodes-base/nodes/Pipedrive/PipedriveTrigger.node.ts — opened 2026-11-10
- https://developers.hubspot.com/changelog/2018-12-10-updated-webhook-retry-logic — search summary, not opened (host ENOTFOUND)
- https://developers.hubspot.com/docs/api-reference/legacy/error-handling — search summary, not opened
- https://community.hubspot.com/t5/APIs-Integrations/HTTP-Status-Code-400-Still-Retries-Request-Contrary-to/m-p/310955 — search summary, not opened
- https://developers.hubspot.com/docs/api/webhooks/validating-requests — search summary, not opened
- https://eshopman-com.pages.dev/insights/hubspot-development/decoding-hubspot-v3-signature-validation-when-spaces-become-plus-signs/ — search summary, not opened
- https://hookdeck.com/webhooks/platforms/guide-to-attio-webhooks-features-and-best-practices.md — search summary, not opened
- https://rubydoc.info/gems/attio-ruby/Attio/Util/WebhookSignature — search summary, not opened
- https://pipedrive.readme.io/docs/guide-for-webhooks — search summary, not opened (host ENOTFOUND)
- https://devcommunity.pipedrive.com/t/webhooks-policy/4309 — search summary, not opened
