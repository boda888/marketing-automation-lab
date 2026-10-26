# R5 · HeyReach vs Aimfox vs Waalaxy: sender assignment, limits, dedupe and events over the API (2026-10-26)

## Question
When n8n (or an agent) drives LinkedIn outreach through HeyReach, Aimfox or Waalaxy: who picks
the sender account per lead, which limits can be set over the API, how duplicates are
handled on import, can pending invites be withdrawn, and which events come back out?

## Findings
Method: heyreach.io, aimfox.com, waalaxy.com, linkedin.com and the third-party blogs returned
by search did not resolve from this run (DNS `ENOTFOUND`). Read instead, not executed, the n8n
packages on registry.npmjs.org: **HeyReach's own** node (`n8n-nodes-heyreach` 1.0.5, author
"HeyReach <contact@heyreach.io>", maintainers on heyreach.io), **Waalaxy's own** node
(`@waapi/n8n-nodes-waalaxy` 1.2.2, author "Waalaxy developers", support address on
waalaxy.com) and a **third-party** Aimfox node (`n8n-nodes-aimfox` 1.0.21, homepage
aimfox.com but the author is not on an aimfox domain — treat its API shapes as unconfirmed).
Expandi has no package on npm (search 2026-10-26) and was not researched. Prices and LinkedIn
limits below are search summaries only.

**API surface per tool (from the packages):**

| | HeyReach (vendor node) | Aimfox (third-party node) | Waalaxy (vendor node) |
|---|---|---|---|
| Base / auth | `https://api.heyreach.io/api/n8n`, header `X-N8N-KEY` (key made from the n8n card in Integrations) | `https://api.aimfox.com/api/v2`, `Authorization: Bearer` | `https://developers.waalaxy.com/`, access token from Settings → CRM sync |
| Add leads | `POST /campaign/AddLeadsToCampaign` with `campaignId` + `accountLeadPairs[]`: **every lead is paired with a `linkedInAccountId`** — the caller picks the sender | `POST /campaigns/{id}/audience` (one `profile_url`) and `/audience/multiple`; custom variables per target | `POST prospects/addProspectFromIntegration`: `prospects[]` (LinkedIn `url` required) + `prospectListId` + optional `campaignId` — the only write operation |
| Dedupe on import | **no flag**; `POST /campaign/GetCampaignsForLead` (by profile URL, email or LinkedIn id) lets you check first | not in the node | three explicit booleans, all default `false`: `moveDuplicatesToOtherList`, `canCreateDuplicates` (teammate's CRM), `addDuplicateProspectsToCampaign` — so by default an existing prospect is **not** enrolled |
| Stop / suppress | `POST /campaign/StopLeadInCampaign` (member id or profile URL); `DELETE /list/DeleteLeadsFromListByProfileUrl` | profile and company **blacklist** (`/blacklist`, `/blacklist-companies`, by URN); `DELETE /campaigns/{id}/audience/{urn}` | none in the node |
| Limits over API | none in the node | `GET/PATCH /accounts/{id}/limits`: weekly `connectLimit` and `inmailLimit` (1–1,000, node hint "recommended 50–200") + **warm-up ramp** (start values rise by 100 every 4 / 2 / 1 weeks at LOW / MEDIUM / HIGH; re-sending warm-up settings restarts the ramp) | none |
| Inbox | `POST /inbox/GetConversations`, `GET /inbox/GetChatroom/{account}/{conversation}`, `POST /inbox/SendMessage` | list / get / mark read / reply / start conversation per account | none |
| Events | webhooks CRUD (`/webhooks/CreateWebhook`, name 3–25 chars, `campaignIds` empty = **all campaigns**): `CONNECTION_REQUEST_SENT`, `CONNECTION_REQUEST_ACCEPTED`, `MESSAGE_SENT`, `MESSAGE_REPLY_RECEIVED`, `EVERY_MESSAGE_REPLY_RECEIVED`, `INMAIL_SENT`, `INMAIL_REPLY_RECEIVED`, `FOLLOW_SENT`, `LIKED_POST`, `VIEWED_PROFILE`, `CAMPAIGN_COMPLETED`, `LEAD_TAG_UPDATED`; **no trigger node** — use n8n's Webhook node | trigger node registers `POST /webhooks` and deletes it on deactivate; events incl. `connect`, `accepted`, `reply`, `new_reply`, `campaign_reply`, `inmail_reply`, `view`, **`account_logged_out`**, `account_logged_in`, `campaign_ended`; no signature check in the node | none |
| Other | campaign statuses `DRAFT / IN_PROGRESS / PAUSED / FINISHED / CANCELED / FAILED / STARTING`; overall stats by sender + campaign + date range; "My Network" per sender (page size ≤ 100); list limits ≤ 100 or ≤ 1,000 per page | campaign create / pause / resume, labels, notes | error codes: `M000401-001` plan does not allow the API, `R000401-003` invalid LinkedIn URL, `R000401-004` prospect limit of the list / CRM reached |

**Withdrawing pending invites:** none of the three packages has a withdraw operation.
Aimfox's limits/warm-up and its vendor slide claim of "auto-withdraw" (STACK seed row) point to
an in-app setting, not an API call; nothing confirmed this run.

**Reply events and double counting:** HeyReach has two reply events — `MESSAGE_REPLY_RECEIVED`
and `EVERY_MESSAGE_REPLY_RECEIVED` ("Every Message/InMail Reply Received"). The label implies
the first fires once per lead and the second on every reply (not confirmed in docs). Subscribing
both to one n8n flow will process the first reply twice. Aimfox similarly has `reply`,
`new_reply` and `campaign_reply`.

**LinkedIn limits (search summary, not opened; LinkedIn publishes no number):** vendor blogs
put the invitation cap around 100 per rolling 7 days for normal accounts (150–200 claimed for
trusted Premium / Sales Navigator), recommend 15–25 per day on a warmed account and ~5 per day
on a new one, and disagree on pending invites (one claims a 1,500 pending ceiling and that
withdrawing does not restore quota; others advise withdrawing stale invites).

**Pricing (search summary, not opened, 2026-10-26):** HeyReach Growth $79 per sender / month
($59 at 10+ senders, ~20 % off annually); Agency $999 / month for 25 senders (reviews say the
grid changed in June 2026; an older $999-for-50 figure still circulates); Unlimited $2,999 with
a 300-sender fair-use cap; API, webhooks and MCP said to be on paid tiers. Aimfox stays at the
slide price ($49, 2026-09). Waalaxy: the node's error `M000401-001` shows API access is
plan-gated; price not found.

## Apply
- HeyReach from n8n / Clay: assign the sender per lead yourself (`linkedInAccountId` in
  `accountLeadPairs`) — e.g. by owner / territory or round-robin with a weekly budget per
  sender — and call `GetCampaignsForLead` by profile URL before adding, because the API has no
  dedupe flag.
- Subscribe **either** `MESSAGE_REPLY_RECEIVED` **or** `EVERY_MESSAGE_REPLY_RECEIVED` per n8n
  flow, never both, and dedupe on sender + lead + message before Slack / CRM. Leave
  `campaignIds` explicit: an empty list means the hook fires for every campaign in the
  workspace.
- Aimfox: alert on `account_logged_out` (a logged-out sender silently stops the sequence) and
  set weekly limits + warm-up via API for each new sender instead of by hand; do not re-send
  warm-up settings on an existing account — that restarts the ramp.
- Waalaxy: import with `canCreateDuplicates: false` (team-wide dedupe) and decide
  `addDuplicateProspectsToCampaign` on purpose — by default an already-known prospect is
  silently left out of the campaign. Map `R000401-003` (bad LinkedIn URL) to a data-quality
  fix, not a retry.
- Keep invite volume per sender under your own cap (start ~5/day new, ≤ 20/day warmed) and
  log pending invites; withdraw stale ones in the app — no API does it.

## Lab impact
06 sequencer sync: the HeyReach adapter must take an explicit sender per lead (sender
assignment is the caller's job), do a "campaigns for lead" pre-check as its dedupe (no vendor
flag), and Waalaxy's three duplicate booleans join the `dedupe_scope` mapping. 07 reply
routing: LinkedIn reply events (HeyReach first-vs-every reply, Aimfox reply variants) go
through the same event dedupe; sender `account_logged_out` is an alert, not a reply. Root
ROADMAP 06 / 07 items edited.

## Sources
- https://registry.npmjs.org/n8n-nodes-heyreach/-/n8n-nodes-heyreach-1.0.5.tgz — opened 2026-10-26 (README, `HeyReach.node.js`, `HeyReachApi.credentials.js`, `operations/*`)
- https://registry.npmjs.org/n8n-nodes-heyreach/latest — opened 2026-10-26 (author / maintainers)
- https://registry.npmjs.org/@waapi/n8n-nodes-waalaxy/-/n8n-nodes-waalaxy-1.2.2.tgz — opened 2026-10-26 (README, `shared/transport.js`, `resources/prospect/*`)
- https://registry.npmjs.org/n8n-nodes-aimfox/-/n8n-nodes-aimfox-1.0.21.tgz — opened 2026-10-26 (third-party; README, credentials, `properties/*`, `AimfoxTrigger`)
- https://registry.npmjs.org/-/v1/search?text=expandi — opened 2026-10-26 (no Expandi package)
- https://www.heyreach.io/pricing, https://aimfox.com/pricing/, https://www.linkedin.com/help/linkedin/answer/a5426958, https://www.joinvalley.co/blog/linkedin-invitation-limit-in-2026-weekly-limits-more — tried 2026-10-26, DNS ENOTFOUND, not opened
- https://emelia.io/software/heyreach/pricing, https://www.joinvalley.co/blog/heyreach-review-2026, https://blog.gojiberry.ai/blog/heyreach-review — search summary, not opened (HeyReach pricing)
- https://www.salesforge.ai/blog/linkedin-connection-request-limit, https://phantombuster.com/blog/social-selling/linkedin-connection-request-limit/, https://firstsales.io/blog/linkedin-connection-request-limits-2026/ — search summary, not opened (LinkedIn limits)
