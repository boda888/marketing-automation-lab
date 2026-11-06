# R5 · HeyReach public API V2 and who stops the other channel on a reply (2026-11-06)

## Question
1. What does HeyReach's public API (not the n8n-key endpoint read on 2026-10-26) do when leads
   are added — dedupe settings, what comes back, retries, rate limit?
2. When one lead is in a HeyReach LinkedIn campaign and a Smartlead / Instantly email
   sequence at the same time, does a reply on one channel stop the other, and who has to
   orchestrate that?

## Findings
Method: heyreach.io docs, the HeyReach Postman documenter, Smartlead's help center,
scalekit and agency blogs did not resolve from this run (DNS `ENOTFOUND`). Read instead, not
executed, three **third-party** npm packages: `heyreach-cli` 0.2.2 (published 2026-08-20,
author Brandon Charleson; bundled `dist/index.js`, `AGENTS.md`, `CHANGELOG.md`),
`heyreach-mcp-server` 2.0.5 (2025-07-30, same author; `API_ENDPOINT_STATUS.md` with an
endpoint probe dated January 2025) and `@mambalabsdev/mcp-sequencer-event-poller` 1.0.2
(README). None of them is HeyReach's own code, so treat the shapes as what the authors saw
in production, not as a vendor contract. Question 2 rests on search summaries only.

**Two API surfaces.** The vendor n8n node (R5 note 2026-10-26) calls
`https://api.heyreach.io/api/n8n` with `X-N8N-KEY`. The CLI and MCP server call
`https://api.heyreach.io/api/public` with `X-API-KEY` (key from Settings → Integrations →
Public API; keys never expire). The CLI states **300 requests / minute per key**, workspace and
organization keys counted separately; its changelog says the HeyReach Postman collection
"now documents 82 endpoints" (not opened).

**Adding leads, V2.** `POST /campaign/AddLeadsToCampaignV2` takes the same `campaignId` +
`accountLeadPairs[]` (`linkedInAccountId` + `lead{firstName, lastName, profileUrl,
companyName, position, emailAddress, customUserFields}`) as the n8n endpoint, plus two
booleans `resumeFinishedCampaign` and `resumePausedCampaign`. It returns **counts only**:
`{"addedLeadsCount": 1, "updatedLeadsCount": 0, "failedLeadsCount": 0}` — no per-lead
result, so a non-zero `failedLeadsCount` cannot be pinned to a lead without reading the
campaign back. `updatedLeadsCount` suggests a lead already in the campaign is updated, not
added twice (inferred from the field name, not documented in what was read). The 2025 probe
says the call only works on an `ACTIVE` / `IN_PROGRESS` campaign that already has sender
accounts assigned; on a draft it answers 400.

**Dedupe lives on the campaign, not the import.** `POST /campaign/Create` (404 in the January
2025 probe, present in the August 2026 CLI) and the campaign settings update carry
`excludeContactedFromOtherCampaigns` ("skip leads already in other campaigns"),
`excludeHasOtherAccConversations` ("skip leads with existing conversations from other
accounts"), `excludeContactedFromSenderInOtherCampaign` and `excludeListId` ("lead list to
always exclude") — a suppression list by id. So the 2026-10-26 conclusion "HeyReach has no
dedupe flag" holds for the add-leads call, but a campaign can be created with the exclusions
on; settings can only be changed while the campaign is `DRAFT` / `SCHEDULED` / `PAUSED`.

**Retries that can repeat a write.** The CLI's client retries **every** request, POST
included, up to 3 times on 429, any 5xx and its own 30 s timeout, with 1 / 2 / 4 s backoff
(`Retry-After` honoured on 429). A timed-out `AddLeadsToCampaignV2` is therefore sent again;
whether the second call counts as "updated" or adds anything is not documented.

**Webhooks (unchanged from 2026-10-26).** Same 12 event types in the CLI as in the vendor node,
one event type per webhook (search summary of a connector guide), payload fields and signing
still not seen in any package.

**Cross-channel stop (search summary, not opened).** Agency guides on the native HeyReach ↔
Smartlead / Instantly integrations (devcommx.com, moderninbound.com, salesforge.ai) say the
native sync moves **lead data, not replies**: a LinkedIn reply does not pause the email
sequence and an email reply does not stop the HeyReach campaign; inboxes stay separate. The
usual fix is a webhook or Zap per direction: Smartlead reply → HeyReach stop-lead, HeyReach
reply → Instantly / Smartlead pause. HeyReach's marketing page and Smartlead's help center
claim replies can pause the other channel — vendor claims, not confirmed.

**Polling instead of webhooks.** The Apify actor behind `mcp-sequencer-event-poller` reads
replies, bounces, unsubscribes, meetings and LinkedIn replies from Instantly, Smartlead,
HeyReach and lemlist into one flat row per event, with an `only_new` memory; billed per
event ($0.008 free plan → $0.0068 gold, README 1.0.2). It needs your four vendor keys, which
are sent in the run input to a third party's actor.

## Apply
- In HeyReach, create outbound campaigns with `excludeContactedFromOtherCampaigns` and
  `excludeHasOtherAccConversations` on and an `excludeListId` pointing at a "do not contact"
  list (customers, opted-out, email-unsubscribed people); it is the only dedupe HeyReach does
  for you, and it can't be switched on once the campaign runs.
- After every `AddLeadsToCampaignV2` call, compare `added + updated + failed` with the number
  of leads sent; if `failedLeadsCount > 0` or the sum is short, read the campaign's leads back
  before retrying — the response does not say which lead failed.
- Don't rely on the native HeyReach ↔ Smartlead / Instantly sync to stop a sequence on a
  reply. Build both directions in n8n: Smartlead / Instantly reply webhook → HeyReach
  `StopLeadInCampaign` by profile URL; HeyReach `MESSAGE_REPLY_RECEIVED` → pause or stop the
  lead in the email tool by email. Match leads on email + profile URL kept together in your
  table, and test with one real reply per direction.
- If you script HeyReach with `heyreach-cli` (or any client that retries POSTs), don't run
  add-leads unattended after a timeout without a read-back: the client re-sends it up to 3
  times.

## Lab impact
06 sequencer sync: the HeyReach adapter gains the public V2 add-leads call (`api/public`,
`X-API-KEY`, resume flags explicit `false`) and a counts parser that reconciles
`added + updated + failed` against the leads sent — failed or missing counts make the chunk
`outcome_unknown` until a read-back, and "updated" leads are reported as already in the
campaign. 07 reply routing: a reply on either channel emits a stop action for the other
channel's campaign (cross-channel suppression is the lab's job, not the vendor sync's).
Root ROADMAP 06 and 07 items edited.

## Sources
- https://registry.npmjs.org/heyreach-cli/-/heyreach-cli-0.2.2.tgz — opened 2026-11-06 (third-party; `dist/index.js` client, add-leads V2, campaign create schema; `AGENTS.md`; `CHANGELOG.md`)
- https://registry.npmjs.org/heyreach-cli — opened 2026-11-06 (publish date, author)
- https://registry.npmjs.org/heyreach-mcp-server/-/heyreach-mcp-server-2.0.5.tgz — opened 2026-11-06 (third-party; `API_ENDPOINT_STATUS.md`, `dist/heyreach-client.js` base URL)
- https://registry.npmjs.org/@mambalabsdev/mcp-sequencer-event-poller/-/mcp-sequencer-event-poller-1.0.2.tgz — opened 2026-11-06 (README: inputs, pricing, keys)
- https://registry.npmjs.org/-/v1/search?text=heyreach&size=20 — opened 2026-11-06 (package list)
- https://documenter.getpostman.com/view/23808049/2sA2xb5F75, https://helpcenter.smartlead.ai/en/articles/432-smartlead-x-heyreach-integration-guide.md, https://www.devcommx.com/blogs/heyreach-smartlead-native-integration-setup, https://docs.scalekit.com/agentkit/connectors/heyreach/ — tried 2026-11-06, DNS ENOTFOUND, not opened
- https://www.devcommx.com/blogs/heyreach-smartlead-native-integration-setup, https://moderninbound.com/blog/heyreach-smartlead-integration-guide, https://www.salesforge.ai/jp/blog/heyreach-with-smartlead, https://www.heyreach.io/multichannel-outreach, https://zapier.com/apps/heyreach/integrations/instantly — search summary, not opened (native sync vs reply stop)
- https://playbooks.com/skills/abdullahbeam/nexus-design-abdullah/heyreach, https://anythingmcp.com/ja/guides/connect-heyreach-to-claude — search summary, not opened (300 req/min, one event type per webhook)
