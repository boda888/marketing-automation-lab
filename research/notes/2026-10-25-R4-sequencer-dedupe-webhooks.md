# R4 · Smartlead vs Instantly vs lemlist: lead push, dedupe defaults, reply webhooks (2026-10-25)

## Question
When n8n (or an agent) pushes leads into Smartlead, Instantly or lemlist: what dedupe and
suppression does each tool apply by default, how are custom variables passed, and how do
reply / bounce / unsubscribe events come back out (webhooks, replays, inbox APIs)?

## Findings
Method: smartlead.ai, api.smartlead.ai, developer.instantly.ai, developer.lemlist.com and the
other vendor hosts were blocked by this run's network policy (connection rejected by the
proxy). Read instead, not executed: the vendors' **own** packages on registry.npmjs.org —
Smartlead's official CLI (repo under the `Smartlead-Public` GitHub org), Instantly's n8n node
(repo under `Instantly-ai`, author e-mail at instantly.ai), lemlist's official CLI
(`@lemlist-official`) — plus n8n's built-in lemlist node source on GitHub. Request-setting
defaults for Smartlead and Instantly bulk upload come only from web-search summaries and are
labelled as such.

**Pushing leads and dedupe:**

| Tool | Push call (first-party package) | Dedupe / suppression on push | Custom variables |
|---|---|---|---|
| Smartlead | `POST https://server.smartlead.ai/api/v1/campaigns/{id}/leads` with `lead_list` (+ optional settings); `POST /leads/push-to-campaign` copies or moves leads between campaigns (`@smartlead/cli` 0.1.0, 2026-03-17) | the CLI passes the body through unchanged. Search summary, not opened: settings `ignore_global_block_list`, `ignore_unsubscribe_list`, `ignore_duplicate_leads_in_other_campaign`, `ignore_community_bounce_list`, all default `false` (= the checks run), up to 400 leads per request; a Clay community thread reports leads silently not added because they sat in another campaign | `custom_fields` object per lead (lead schema in the CLI) |
| Instantly | vendor n8n node `n8n-nodes-instantly` 0.1.7547 (2025-11-09): **one lead per call**, `POST https://api.instantly.ai/api/v2/leads` with `campaign` (not `campaign_id`, per a "FIXED" comment in the code) | the node exposes **no skip / dedupe flags at all**. Search summary, not opened: the bulk `POST /api/v2/leads/add` (≤ 1,000 leads, exactly one of `campaign_id` / `list_id`) has `skip_if_in_workspace` (overrides the others), `skip_if_in_campaign`, `skip_if_in_list` | `custom_variables` object; `personalization` as its own field |
| lemlist | n8n built-in node: `POST /campaigns/{campaignId}/leads/{email}` with `deduplicate` as a query parameter (n8n `master`, `nodes/Lemlist/v2`) | `deduplicate` defaults to **false**; true means "do not insert if this email is already present in another campaign". Global unsubscribes are a separate resource (`POST /unsubscribes/{email}`) | extra lead fields in the body |

So all three default to "insert", and each names its cross-campaign check differently; none
of the first-party packages makes the caller choose explicitly.

**Events back out (replies, bounces, unsubscribes):**
- **Smartlead**: webhooks are **per campaign** (`GET/POST/DELETE /campaigns/{id}/webhooks`,
  body `name`, `webhook_url`, `event_types`, optional `categories` and `id` for upsert). The
  official CLI also exposes a delivery summary (`/webhooks/summary` → `total` / `success` /
  `failed` over a time range) and **`retrigger-failed-events`** for a time window — failed
  deliveries are replayed, so the receiver will see the same event twice and must be
  idempotent. Event names in a community n8n node (`@automateitplease/n8n-nodes-smartlead`
  1.0.4, third-party): `EMAIL_SENT`, `EMAIL_OPENED`, `EMAIL_CLICKED`, `EMAIL_REPLIED`,
  `EMAIL_BOUNCED`, `EMAIL_UNSUBSCRIBED`. Unified inbox (`inbox replies / unread`,
  `inbox reply`, `update-category`, `push-subsequence`) and lead categories
  (`/leads/fetch-categories`) are in the CLI.
- **lemlist**: one team-level `POST /hooks` with `targetUrl`, optional `type`, `campaignId`
  and `isFirst` (default false: fire on every occurrence, not just the first). n8n's trigger
  lists 48 event types — `emailsReplied`, `emailsBounced`, `emailsUnsubscribed`,
  `emailsInterested`, `linkedinReplied`, `linkedinInviteAccepted`, `aircallEnded`,
  `aircallInterested`, `apiFailed`, `sendLimitReached`, `connectionIssue`,
  `customDomainErrors`, `lemwarmPaused`, … — so one hook carries email, LinkedIn, Aircall-call
  and mailbox-health events. The n8n trigger hands `req.body` straight to the workflow: **no
  signature or secret check** in the node.
- **Instantly**: the vendor n8n node has **no trigger node**; replies are read through Unibox
  operations (list / get / unread count / mark read / reply). Lead interest status is an
  integer: Out of Office **0**, Interested 1, Meeting Booked 2, Meeting Completed 3, Closed 4,
  Not Interested −1, Wrong Person −2, Lost −3. API access needs the Hypergrowth plan or above
  (node README).

**Retries and agent access:**
- Smartlead CLI: 429 handling is **off by default** (`--retry`: 3 retries, back-off from
  `Retry-After` doubling per attempt); the API key travels as an `api_key` query parameter
  (the CLI masks it in error URLs). Commands include `campaigns set-status START|PAUSE|STOP`
  and `campaigns delete --confirm`.
- lemlist CLI 0.3.2 (2026-10-23): OAuth browser login or API key, one profile per team
  ("agency pattern"), retries network errors, 5xx and rate limits **automatically**, refuses
  `DELETE` in non-interactive use unless `--yes`; config points at an MCP server
  `https://app.lemlist.com/mcp` and the live spec `developer.lemlist.com/api-reference/openapi/v2.json`
  (not reachable this run); ships a Claude Code plugin skill (`agent-plugin/`).
- An automatic retry of a lead-creating POST after a 5xx can insert the lead twice when the
  first call actually succeeded — which is why the dedupe flag matters even for one push.

## Apply
- In every n8n push node set the dedupe flags explicitly, never by default: Smartlead
  `settings` with `ignore_unsubscribe_list: false`, `ignore_global_block_list: false` and
  `ignore_duplicate_leads_in_other_campaign` set on purpose; lemlist `deduplicate` on purpose;
  Instantly use bulk `/api/v2/leads/add` with `skip_if_in_campaign` (or `_in_workspace`)
  rather than the vendor node's one-by-one create, which has no skip option.
- After each Smartlead upload, compare leads sent vs leads added and log the difference per
  campaign — silently skipped duplicates and block-list hits are otherwise invisible.
- Instantly: branch on lead interest status with an explicit `=== 0` / "is not empty" check;
  Out of Office is `0`, so a truthy IF in n8n treats it like "no status".
- Reply webhooks into n8n: dedupe on an event key (campaign + lead + event type + message id
  / timestamp) before alerting or writing to CRM — Smartlead replays failed deliveries and
  lemlist fires on every occurrence unless `isFirst` is set. Add a secret path segment or
  header check yourself; the lemlist trigger does not verify the sender.
- Keep sequencer webhooks per campaign in Smartlead in sync with campaign creation (a new
  campaign without a webhook sends replies nowhere); check `webhooks summary` failures weekly.

## Lab impact
06 sequencer sync: adapters must take an explicit `dedupe_scope` (campaign / other
campaigns / workspace) and map it to each vendor's flag, report `sent` vs `added` vs
`skipped` per push, and treat a retried create as an idempotent operation. 07 reply routing:
an event-id dedupe stage before classification, and Instantly's numeric interest codes
mapped to the lab's classes as context only. Root ROADMAP 06 / 07 items edited accordingly.

## Sources
- https://registry.npmjs.org/@smartlead/cli/-/cli-0.1.0.tgz — opened 2026-10-25 (README, `dist/client.js`, `dist/commands/leads.js`, `dist/commands/webhooks.js`, `dist/schemas/*`)
- https://registry.npmjs.org/n8n-nodes-instantly/-/n8n-nodes-instantly-0.1.7547.tgz — opened 2026-10-25 (README, `LeadOperations.js`, `LeadParameters.js`, `generic.functions.js`)
- https://registry.npmjs.org/@lemlist-official/cli/-/cli-0.3.2.tgz — opened 2026-10-25 (README, `agent-plugin/skills/lemlist-cli/SKILL.md`, `dist/lemlist-cli-conf.js`)
- https://raw.githubusercontent.com/n8n-io/n8n/master/packages/nodes-base/nodes/Lemlist/LemlistTrigger.node.ts — opened 2026-10-25
- https://raw.githubusercontent.com/n8n-io/n8n/master/packages/nodes-base/nodes/Lemlist/GenericFunctions.ts — opened 2026-10-25
- https://raw.githubusercontent.com/n8n-io/n8n/master/packages/nodes-base/nodes/Lemlist/v2/LemlistV2.node.ts — opened 2026-10-25
- https://raw.githubusercontent.com/n8n-io/n8n/master/packages/nodes-base/nodes/Lemlist/v2/descriptions/LeadDescription.ts — opened 2026-10-25
- https://registry.npmjs.org/@automateitplease/n8n-nodes-smartlead/-/n8n-nodes-smartlead-1.0.4.tgz — opened 2026-10-25 (third-party; webhook event names only)
- https://registry.npmjs.org/instantly-mcp/-/instantly-mcp-1.0.5.tgz — opened 2026-10-25 (no skip flags found)
- https://api.smartlead.ai/api-reference/leads/add-to-campaign.md, https://api.smartlead.ai/guides/lead-management, https://community.clay.com/x/support/d801uqfoar33/why-are-some-leads-not-being-added-in-smartlead — search summary, not opened (proxy rejected)
- https://deepline.com/docs/providers/instantly/add-to-campaign.md, https://docs.getcargo.ai/integration/instantly-v2 — search summary, not opened (third-party mirrors of Instantly docs)
