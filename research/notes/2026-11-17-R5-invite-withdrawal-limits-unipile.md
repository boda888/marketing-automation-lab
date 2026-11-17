# R5 · Pending-invite withdrawal, invite limits and what an API exposes about them (2026-11-17)

## Question
1. Which tools can withdraw pending LinkedIn invitations over an API, and does withdrawing
   free invite quota?
2. What "safe volume" numbers exist for invitations, and which of them are anything more than
   a vendor's rule of thumb? What does an API tell you when you hit the limit?

## Findings
Method: linkedin.com help pages, developer.unipile.com and www.unipile.com did not resolve from
this run (DNS `ENOTFOUND`). Read instead, not executed: Unipile's **own** packages on npm —
`@unipile/sdk` 2.66.0 (published 2026-11-09 by Unipile staff, generated from their v2
OpenAPI: `sdk.gen.d.ts`, `types.gen.d.ts`), `@unipile/n8n-nodes-unipile` 0.2.1 (Unipile SAS,
official community node) and the older v1 client `unipile-node-sdk` 1.9.3. Unipile is the
LinkedIn API layer several outreach tools are built on; it adds no LinkedIn limits of its own
(search summary). Limits and the error code below are search summaries only.

**Withdrawal is an API call — on Unipile, not on HeyReach.** The v2 API has the full
invitation lifecycle under `/v2/{account_id}/users/me/relation-requests`:
- `GET ?type=sent|received` (cursor or offset paging; `limit` "is a ceiling, not a
  guarantee") lists pending invitations with `id`, `created_at` (ISO 8601, *optional*),
  `message` and the target `user` (`id`, `public_identifier`, `profile_url`);
- `POST` with `{user_id, message?}` sends one (201 returns the same object); "trying to send a
  relation request to an existing relation will fail";
- `POST /{request_id}/cancel` withdraws a sent one (or refuses a received one) and answers only
  `{"object": "RelationRequestCanceled"}`.
A profile read carries `relation_request_status: PENDING | IGNORED | WITHDRAWN` (v1:
`invitation.status` with `type SENT | RECEIVED`), so "already invited / withdrawn" can be
checked before a send. The official n8n node has Send / List / Accept invitation but **no
cancel operation**: in n8n a withdrawal goes through the node's generic "any endpoint" call or
an HTTP Request. The HeyReach packages read on 2026-10-26 / 2026-11-06 and the third-party
Aimfox node have no withdraw call at all — withdrawal there is a UI setting.

**No event tells you an invite was withdrawn or ignored.** Unipile's webhook event list has
`relation.new` (a new connection = an accepted invite), `follower.new`, `blocked.new` and
account events (`account.locked` / `account.unlocked`, `account.status.disconnected |
errored | degraded | partial | running`), but nothing for withdrawn, ignored or expired
invitations. Pending age has to come from your own send log or a periodic `type=sent` list
(and `created_at` may be absent). Webhook endpoints get a `secret` on create, and
`GET /v2/webhooks/conversations/` returns the delivery log per attempt (`first_attempt` false
= retry, `http_status`, `response_time_ms`, `event_id`) — the same "check the log, replay
the gap" pattern as Instantly's `webhook-events` (R4 2026-11-05).

**Quota and the limit signal (search summaries, not opened).** LinkedIn publishes no fixed
weekly number. Vendor blogs (Expandi, Snov.io, Salesforge, Hublead and others) give
~100–200 invitations a week depending on account history; Unipile's guide recommends
~80–100 a day and ~200 a week for a paid, active account and says this is not strictly
enforced, with free accounts capped much lower for notes (figures disagree between Unipile's
own pages: 5 a month vs ~15 a week with a note). Several sources agree that **withdrawing
does not restore the weekly quota** (the limit counts sends, not pending), and LinkedIn Help,
as quoted by one vendor, blocks re-inviting the same person for up to **three weeks** after a
withdrawal. Over the limit, Unipile's docs reportedly return **HTTP 422 `cannot_resend_yet`**
for invitations and 429 or 500 for other actions; that error type does not appear in either
SDK's types (errors are untyped there), so the exact body is unconfirmed. Pending-backlog
advice ranges from "under 500" to a "ceiling around 700–1,500" — folklore, not policy.

## Apply
- Withdraw old pending invites for **hygiene and acceptance-rate**, not to get quota back:
  a weekly n8n job that lists `type=sent`, keeps those older than your cut-off (e.g. 21 days,
  taking the date from your own send log when `created_at` is missing) and cancels them. Write
  each withdrawal to the lead row with the date, and block re-invites to that profile for at
  least 3 weeks after it.
- Treat a `422 cannot_resend_yet` (or any invite-limit error) as **"sender full until next
  week"**, not as a lead failure: stop that sender's invite step for the rest of the window,
  keep the lead queued for the same sender, and never let n8n's Retry On Fail re-send it.
- Count capacity in your own table per sender per rolling 7 days (sent, not pending), start a
  new or rented profile well below the vendor numbers and raise it only after a clean week —
  the 100–200 / week range is vendor folklore, the only real signal is your own limit errors.
- Subscribe to `relation.new` for "accepted → start the message step" and to
  `account.locked` / `account.status.*` for "pause this sender + alert"; check
  `/v2/webhooks/conversations/` daily for non-2xx deliveries.

## Lab impact
06 sequencer sync: the LinkedIn sender budget counts **sent** invites per rolling 7 days
(withdrawals never refund it); a pending-invite sweep plans `cancel` for invites older than a
cut-off and records a re-invite block (≥ 21 days) per profile; an invite-limit error (`422
cannot_resend_yet`) marks the *sender* full until its window rolls, not the lead failed.
07 reply routing: a sender lock / disconnect event (Unipile `account.locked`,
`account.status.disconnected`, like Aimfox `account_logged_out`) routes as an alert that
pauses that sender. Root ROADMAP 06 item edited.

## Sources
- https://registry.npmjs.org/@unipile/sdk/-/sdk-2.66.0.tgz — opened 2026-11-17 (vendor; `dist/sdk.gen.d.ts` relation-request methods, `dist/types.gen.d.ts` request / response types, webhook event list, webhook conversations, account `status` / `is_locked`)
- https://registry.npmjs.org/@unipile/sdk — opened 2026-11-17 (publish date 2026-11-09, maintainers @unipile.com)
- https://registry.npmjs.org/@unipile/n8n-nodes-unipile/-/n8n-nodes-unipile-0.2.1.tgz — opened 2026-11-17 (vendor n8n node: invitation operations, no cancel; base URL `api.unipile.com/v2`)
- https://registry.npmjs.org/unipile-node-sdk/-/unipile-node-sdk-1.9.3.tgz — opened 2026-11-17 (v1 types: `InvitationSent`, `invitation.status` PENDING / IGNORED / WITHDRAWN)
- https://registry.npmjs.org/-/v1/search?text=unipile&size=10 — opened 2026-11-17 (package list)
- https://www.linkedin.com/help/linkedin/answer/a5394, https://www.unipile.com/linkedin-api-rate-limits/, https://developer.unipile.com/v2.0/docs/linkedin-manage-invitations — tried 2026-11-17, DNS ENOTFOUND, not opened
- https://www.unipile.com/linkedin-api-rate-limits/, https://developer.unipile.com/docs/provider-limits-and-restrictions, https://developer.unipile.com/v2.0/docs/provider-limits-and-restrictions — search summary, not opened (80–100 / day, ~200 / week, 422 `cannot_resend_yet`, free-account figures)
- https://snov.io/blog/linkedin-invitation-limit/, https://expandi.io/blog/linkedin-connections-limit/, https://www.salesforge.ai/blog/linkedin-connection-request-limit, https://www.hublead.io/linkedin-invitation-limit — search summary, not opened (weekly range, withdrawal does not refund quota, 3-week re-invite block, pending backlog figures)
