# R7 · Funnel reporting from raw events: what sequencer analytics actually count (2026-11-19)

## Question
Project 10 ("reporting that reconciles") needs a funnel sent → replied → meeting → deal by
segment / signal. Can it be built from the sequencers' analytics endpoints, or must it be
built from raw per-lead events? Concretely: how do Smartlead and Instantly date and count
their aggregates, where does "meeting booked" come from, and what does the CRM side
(HubSpot meetings) add? Answers the R7 open question "Funnel reporting from raw events
(sent → replied → meeting → deal) by segment / signal" for the email half, and opens the
cost-per-meeting question with a formula only.

## Findings
Method: vendor docs hosts (`api.smartlead.ai`, `developer.instantly.ai`,
`developers.hubspot.com`) and the practitioner blogs found by search did not resolve from this
run (`ENOTFOUND` / proxy 403). I read, without running them, the vendor-published packages on
registry.npmjs.org: `@smartlead/cli` 0.1.0 (repo `Smartlead-Public/smartlead-cli`),
`@instantlyai/cli` 0.2.7 (maintainer `instantly.ai`) and `n8n-nodes-instantly` 0.1.7547
(repo `Instantly-ai/n8n-nodes-instantly`). Everything else is labelled search summary.

**Smartlead — two date attributions for the same metric.**
- `commands/analytics.js` exposes pairs of routes: `/analytics/day-wise-overall-stats` vs
  `/analytics/day-wise-overall-stats-by-sent-time`, and
  `/analytics/day-wise-positive-reply-stats` vs `…-positive-reply-stats-by-sent-time`.
  The by-sent-time routes make `--timezone` a *required* option; the event-date ones leave it
  optional. Search summary of the API reference: the event-date variant puts each metric on
  the day it happened, the sent-time variant buckets every metric on the day the email went out.
- That is the whole reporting problem in one API: an event-dated "replies on Tuesday"
  mixes replies to mail sent weeks apart, so `replies / sent` for a day or a week is not a
  rate of anything. A cohort rate needs sent-time attribution (or per-lead events re-joined
  to their send).
- Campaign stats (`schemas/stats.schema.js`) carry both `sent_count` and `unique_sent_count`,
  `open_count` / `unique_open_count`, `click_count` / `unique_click_count`, but only one
  `reply_count` and no delivered count — so the vendor gives no ready "unique replies /
  delivered" (delivered has to be derived as sent − `bounce_count`, and whether `reply_count`
  is per lead or per message is not stated in the schema).
- Per-lead pull exists: `/campaigns/{id}/leads-statistics` (limit 1–100, offset,
  `event_time_gt`), returning per-lead sent / open / click / reply counts — the incremental
  source for a raw-event table. `/analytics/lead/category-wise-response` groups replies by lead
  category (the positive / meeting labels the team sets).
- The CLI's own client retries 429 only when asked (`retry` off by default, 3 retries,
  `Retry-After` × 2^(attempt−1)), so a reporting job that pages leads-statistics must budget
  its own rate.

**Instantly — event-dated only; "meeting booked" is a lead label.**
- `@instantlyai/cli` maps `/campaigns/analytics`, `/campaigns/analytics/overview`,
  `/campaigns/analytics/daily`, `/campaigns/analytics/steps` — all filtered by
  `start_date` / `end_date` (YYYY-MM-DD) only; there is no sent-time variant in the CLI or in
  the vendor n8n node (`AnalyticsOperations.getCampaignAnalytics` sends `start_date`,
  `end_date`, optional `id`). Neither exposes `expand_crm_events`, which a search summary of the
  API Explorer says changes how status-update events are counted in the overview.
- The same CLI's lead commands define `interest_status` as numbers: `interested 1`,
  `meeting_booked 2`, `meeting_completed 3`, `won 4`, `ooo 0`, `not_interested -1`,
  `wrong_person -2`, `lost -3`, `no_show -4`, set by `PATCH /leads/{id}`. So Instantly's
  "meetings booked" (search summary: overview fields `total_opportunities`,
  `total_meeting_booked`, `total_meeting_completed`; `reply_count_unique` excludes automatic
  replies) counts **leads someone or some automation labelled 2**, not calendar events. A
  meeting booked from a LinkedIn reply or a call never becomes a 2 unless something writes it.
- Field names disagree between sources (`unique_replies` vs `reply_count_unique` — search
  summary), so a parser must not assume one spelling.

**CRM side — HubSpot meetings (search summary, docs not opened).** Meetings are activity
objects with `hs_meeting_outcome` (`SCHEDULED`, `COMPLETED`, `RESCHEDULED`, `CANCELED`, a
no-show value whose exact spelling was not confirmed); portals can add custom outcomes, and a
community thread reports custom values read back as `null` from the standard endpoint — read
the property's enum options first. The SDK (`@hubspot/api-client`) is generic and lists no
property names. This is the system of record for "meeting happened"; the sequencer label is a
claim.

**Practitioner consensus (search summaries only).** Divide replies by delivered (sent − hard
bounces), count unique human replies (auto-replies out), track positive replies and meetings
per delivered as separate conversions, and state numerator / denominator / period with every
rate — published benchmarks disagree mainly because of denominators. Cost per meeting =
(retainer + data / tool spend + labour) ÷ meetings booked; no source gave a reconciled sheet.

## Apply
- In Smartlead reporting (n8n or a sheet), pull the **by-sent-time** routes for rates by
  week / campaign, and the event-date routes only for workload ("replies to handle today").
  Pass the same IANA timezone on both, or week boundaries shift.
- Never report "meetings" from Instantly's overview alone: it is the count of leads tagged
  `meeting_booked` (2). Either write status 2 from the calendar / CRM automation when a meeting
  is actually created (and 3 / `no_show -4` after it), or count meetings from CRM meeting
  objects and join to the lead by email.
- Build the funnel from one per-lead table (lead key, campaign, segment / signal, first send
  date, first human reply date, positive flag, meeting created / held, deal) fed from webhooks
  and the per-lead stats pull; compute every rate on the **send cohort**, with delivered as the
  reply denominator. Vendor dashboards are then a reconciliation check, not the report.
- For HubSpot meetings, read the `hs_meeting_outcome` options once per portal and map custom
  outcomes explicitly; a `null` outcome is "unknown", not "scheduled".

## Lab impact
Project 10 gets concrete rules: (1) funnel computed per send cohort from per-lead events, never
by dividing event-dated aggregates; (2) reply denominator = delivered (sent − bounces), unique
human replies, auto-replies excluded; (3) a meeting counts only from a CRM meeting object (or a
sequencer label with provenance `label`, reported separately); (4) reconciliation check against
vendor aggregates that names which attribution the vendor used; (5) cost per meeting only when
every spend line carries a period and a source. Added to ROADMAP 10.

## Sources
- https://registry.npmjs.org/@smartlead/cli (0.1.0 tarball: `dist/commands/analytics.js`, `dist/commands/stats.js`, `dist/schemas/stats.schema.js`, `dist/schemas/analytics.schema.js`, `dist/client.js`) — opened 2026-11-19
- https://registry.npmjs.org/@instantlyai/cli (0.2.7 tarball: `dist/index.js` analytics + `leads_update_interest_status`, `INSTANTLY.md`) — opened 2026-11-19
- https://registry.npmjs.org/n8n-nodes-instantly (0.1.7547 tarball: `operations/AnalyticsOperations.js`, `descriptions/AnalyticsDescription.js`) — opened 2026-11-19
- https://registry.npmjs.org/@hubspot/api-client (latest tarball: generic meetings API, no property list) — opened 2026-11-19
- https://raw.githubusercontent.com/n8n-io/n8n/master/packages/nodes-base/nodes/Hubspot/V2/EngagementDescription.ts — opened 2026-11-19
- https://api.smartlead.ai/api-reference/analytics/day-wise-stats — search summary, not opened (ENOTFOUND)
- https://developer.instantly.ai/api-reference/campaign/get-campaigns-analytics-overview — search summary, not opened (ENOTFOUND)
- https://www.withone.ai/knowledge/instantly-ai/conn_mod_def%3A%3AGJ4omy8qclA%3A%3A0ctvJ2F2TMWQyT9XkC_U4Q — search summary, not opened (third party)
- https://developers.hubspot.com/docs/api-reference/latest/crm/activities/meetings/guide.md — search summary, not opened
- https://community.hubspot.com/t/custom-meeting-outcomes/72836 — search summary, not opened
- https://www.civic.com/md/field-notes/the-reply-rate-isnt-the-metric.md — search summary, not opened (ENOTFOUND)
- https://tomba.io/blog/sales-hub-cold-email-metrics-benchmarks — search summary, not opened
- https://instantly.ai/blog/cold-outbound-cost-per-meeting-calculator/ — search summary, not opened
