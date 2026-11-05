# R4 · Reply webhooks into n8n: payload fields, authentication, replays per sequencer (2026-11-05)

## Question
When Smartlead, Instantly or lemlist reports a reply (or bounce / unsubscribe / category
change) to n8n: which fields does the payload carry, can the receiver tell a genuine
delivery from a forged one, which key dedupes a replayed delivery, and what happens when the
receiver is down? (Open R4 question "Reply handling: webhook payload fields per vendor";
event names, replays and inbox APIs were covered in the
[2026-10-25 note](2026-10-25-R4-sequencer-dedupe-webhooks.md) and are not repeated.)

## Findings
Method: api.smartlead.ai, developer.instantly.ai, developer.lemlist.com, app.lemlist.com,
help.instantly.ai and the vendors' marketing sites were again unreachable from this run
(connection refused by the proxy / DNS); hookdeck.com DNS-blocked. Read instead, not run:
integration packages on registry.npmjs.org — Activepieces' Smartlead (0.2.0) and lemlist
(0.2.2, published 2026-11-04) pieces, which register and receive real webhooks;
Instantly's CLI + MCP server `instantly-cli` 0.1.22 (already a source on 2026-11-02; its
webhook and HTTP-client code is new here); Pipedream's Instantly components 0.1.2; lemlist's
official CLI 0.4.0. Nothing was sent to a live account.

**Per vendor:**

| | Smartlead | Instantly | lemlist |
|---|---|---|---|
| Register | `POST /campaigns/{id}/webhooks` `{id: null, name, webhook_url, event_types[]}` — **per campaign** | `POST /api/v2/webhooks` `{target_hook_url, event_type, campaign?, name?}` — workspace-wide or one campaign; **one event type per webhook** (or `all_events`); `custom_interest_value` for custom-label events | `POST /hooks` `{targetUrl, secret?, type?, campaignId?, isFirst?}` — team-wide |
| Event names | Activepieces 0.2.0 (Oct 2026): `EMAIL_SENT`, `EMAIL_OPEN`, `EMAIL_LINK_CLICK`, `EMAIL_REPLY`, `LEAD_UNSUBSCRIBED`, `LEAD_CATEGORY_UPDATED`. The third-party n8n node read on 2026-10-25 used `EMAIL_REPLIED`, `EMAIL_OPENED`, `EMAIL_UNSUBSCRIBED` … — **the two disagree**; Smartlead's own CLI passes `event_types` as free strings and lists none | `GET /webhooks/event-types` returns the list (incl. custom labels); CLI docs name `reply_received`, `email_sent`, `email_opened`, `link_clicked`, `email_bounced`, `email_unsubscribed`, `lead_interested`, `campaign_completed` | `emailsReplied`, `linkedinReplied`, `whatsappReplied`, `smsReplied`, interest events (`apiInterested`, `manualNotInterested` …); unsubscribes arrive under **five** types: `emailsUnsubscribed`, `entityUnsubscribed`, `variableUnsubscribed`, `apiUnsubscribed`, `manualUnsubscribed` |
| Payload (sample data in the integration) | flat: `event_type`, `campaign_id` (int), `campaign_name`, `sl_email_lead_id`, `to_email`, `to_name`, `from_email`, `subject`, `event_timestamp` | Activepieces sample (`campaign_completed`): `timestamp`, `event_type`, `workspace`, `campaign_id`, `campaign_name`; reply fields not in any package | `_id` (activity id, `act_…`), `type`, `teamId`, `createdAt`, `campaignId`, `sequenceId`, `sequenceStep`, `stepId`, `leadId`, `contactId`, `leadEmail`, `sendUserEmail`, `subject` … |
| Event id for dedupe | none in the sample → build one | none in the sample → build one | `_id` — the piece keeps the last 1,000 seen ids and drops repeats |
| Authenticity | no signature or secret anywhere in the piece or the CLI; the piece accepts any JSON object | `webhooks_create` / `_update` accept no secret or header field; search summaries (Hookdeck guide, not opened) also say deliveries are unsigned | `secret` set at `POST /hooks` is **echoed in every delivered body**; the piece generates 32 random bytes per hook, compares `body.secret` in constant time, strips it before passing the event on, and drops events whose `createdAt` is > 24 h old |
| Receiver down | `retrigger-failed-events` for a time window (CLI, 2026-10-25 note) → replays | delivery log `GET /webhook-events?success=false&from&to&search=<url or lead email>`, `/webhook-events/summary(-by-date)`; webhooks **disabled after repeated delivery failures**, re-enabled only by `POST /webhooks/{id}/resume` | not stated in the piece; 200 hooks per team (disabled ones count) — `POST /hooks` answers 409 when full |

**Other things the code shows:**
- Smartlead webhooks are bound to a campaign: the Activepieces trigger creates one on enable
  and deletes it on disable. A campaign cloned or created later has no webhook until someone
  adds one (same failure mode as the 2026-10-25 note, now confirmed from a second client).
- Instantly's own CLI docs (`AGENTS.md`) show flags `--webhook-url` and `--webhook-id` that its
  code does not have (`--url`; the event log filters by `search=` URL or lead email, not by
  webhook id) — copying the docs' commands fails. Its HTTP client retries **5xx on writes too**
  (up to `maxRetries`, back-off 1 s doubling to 10 s, `Retry-After` on 429) but never retries a
  write that timed out: a 5xx after the lead was actually created gets sent again.
- Pipedream's Instantly "New Email Received" source polls `GET /emails?email_type=received`
  newest first, emits everything with `timestamp_created >= lastTs` (inclusive) and relies on
  platform dedupe by email `id` — the polling fallback for a disabled webhook needs the same
  id dedupe or the boundary email is processed twice.
- lemlist CLI 0.4.0 (2026-10-25) still only carries the MCP URL in its config; no tool list in
  the package, and the official MCP registry returns no lemlist server (queried 2026-11-05).
  Third-party lemlist MCP wrappers (StackOne, Vinkius) expose start / pause campaign tools —
  search summary, not opened.

## Apply
- n8n reply intake: compute an event key before anything else — lemlist `_id`; Smartlead
  `campaign_id + sl_email_lead_id + event_type + event_timestamp`; Instantly
  `campaign_id + lead email + event_type + timestamp` — and drop repeats (Remove Duplicates
  across executions, or a Postgres unique key). Smartlead replays and Instantly's resume /
  polling fallback both re-deliver.
- lemlist: create the hook with an HTTP Request (`POST /hooks` with a random `secret`)
  instead of relying on n8n's Lemlist Trigger, then IF `{{$json.body.secret}}` equals the
  stored value before routing, and remove `secret` before writing the event anywhere.
  Treat all five `*Unsubscribed` types as unsubscribe, not just `emailsUnsubscribed`.
- Smartlead / Instantly have no secret: put a long random token in the webhook path
  (`/webhook/<token>/smartlead`) and use one URL per vendor so a leaked URL can be rotated.
- Smartlead: when you build the n8n Switch on `event_type`, send yourself one real reply and
  read the actual name (`EMAIL_REPLY` vs `EMAIL_REPLIED`) — a wrong string routes every reply
  to the fallback branch silently. Add a fallback output that alerts on unknown event types.
- Instantly: schedule a daily n8n check of `GET /api/v2/webhooks` (or
  `/webhook-events/summary`) — a webhook auto-disabled after failures stops all replies
  until someone calls `/resume`; then replay the gap from `/webhook-events?success=false`.

## Lab impact
07 reply routing: the intake stage needs per-vendor event-key builders (lemlist `_id`;
derived keys for Smartlead / Instantly), a secret check that compares in constant time and
strips the secret (lemlist) or a path-token check (others), a stale-event cut-off, mapping of
both Smartlead event-name spellings and all five lemlist unsubscribe types, and an
"unknown event type → alert" branch. 09: watchdog for Instantly webhooks disabled after
failures. Root ROADMAP 07 item edited.

## Sources
- https://registry.npmjs.org/@activepieces/piece-smartlead/-/piece-smartlead-0.2.0.tgz — opened 2026-11-05 (`src/index.js`: event list, `createCampaignWebhook`, trigger sample data)
- https://registry.npmjs.org/@activepieces/piece-lemlist/-/piece-lemlist-0.2.2.tgz — opened 2026-11-05 (`src/index.js`: hook enable with `secret`, `secretsMatch`, 24 h cut-off, `_id` dedupe, unsubscribe types, 200-hook 409)
- https://registry.npmjs.org/@activepieces/piece-instantly-ai/-/piece-instantly-ai-0.2.3.tgz — opened 2026-11-05 (webhook trigger sample data)
- https://registry.npmjs.org/instantly-cli/-/instantly-cli-0.1.22.tgz — re-read 2026-11-05 (already a source 2026-11-02; new parts: `webhooks_*`, `webhook_events_*`, HTTP client retries, `AGENTS.md`)
- https://registry.npmjs.org/@pipedream/instantly/-/instantly-0.1.2.tgz — opened 2026-11-05 (`sources/common/base.mjs`, `new-email-received`, `common/constants.mjs`)
- https://registry.npmjs.org/@lemlist-official/cli/-/cli-0.4.0.tgz — opened 2026-11-05 (no MCP tool list)
- https://registry.modelcontextprotocol.io/v0/servers?search=lemlist — opened 2026-11-05 (empty)
- https://api.smartlead.ai/api-reference/webhooks/events, https://help.smartlead.ai/Lead-Category-Updated-22cab06ce4a0499ca417916cececa62c — search summary, not opened (`LEAD_CATEGORY_UPDATED` payload with `lead_data`, `category_id_map` filter)
- https://hookdeck.com/webhooks/platforms/guide-to-instantly-webhooks-features-and-best-practices — search summary, not opened (DNS-blocked): unsigned deliveries, no event id, auto-disable + resume
- https://stackone.com/connectors/lemlist/mcp/ — search summary, not opened
