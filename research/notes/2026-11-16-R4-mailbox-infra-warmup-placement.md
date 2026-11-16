# R4 · Mailbox infrastructure, warm-up and inbox placement over API (2026-11-16)

## Question
1. What do mailbox-infrastructure vendors (Zapmail, Mailforge / Infraforge / Primeforge) cost
   per inbox, and what can be automated over their APIs (domains, DNS, mailboxes, export to
   the sequencer)?
2. Warm-up and inbox placement: what can a pipeline actually read back as a number, and what
   is vendor theatre?

Vendor sites (zapmail.ai, mailforge.ai, instantly.ai, support.google.com) did not resolve from
this session (DNS-blocked), so API facts come from vendor or third-party npm packages that
were downloaded and read; prices and warm-up timelines are **search summaries only**.

## Findings

**Prices (search summaries, not opened — mostly competitors' comparison pages, they disagree):**
- Zapmail: $39 / month for 10 mailboxes, then ~$3.25–3.50 per extra mailbox; a $299 tier with
  100 mailboxes is also quoted. Pre-warmed mailboxes are claimed by some reviews and denied by
  a 2026 community review — treat "pre-warmed" as unconfirmed.
- Mailforge (shared IPs): ~$2–3 per mailbox / month, 10-mailbox minimum, domains extra
  (~$14 / year .com quoted). Infraforge (dedicated IPs): ~$2.50–4 per mailbox, one source adds
  a $99 / month dedicated-IP fee (others don't). Example quoted: 100 mailboxes on 20 domains
  ≈ $489 / month on Infraforge — i.e. ~5 mailboxes per domain in that vendor example.
- The course-slide row in STACK.md ($39 for 10, then $3.50) matches the search summaries.

**Zapmail API** (`zapmail-mcp` 0.0.4, npm; author field "Zapmail Team", repo under a personal
GitHub account — treat as unofficial): base `https://api.zapmail.ai/api`, `/v2/...` routes;
workspace chosen per request with an `x-workspace-key` header and the mailbox platform with
`x-service-provider: GOOGLE | MICROSOFT`. Covered: domain availability / buy, nameservers +
verify, DNS records CRUD + `dns/check`, DMARC, forwarding, catch-all enable / disable,
`POST /v2/mailboxes` (create on a domain), `remove-on-renewal`, wallet balance / auto-recharge,
subscriptions. Export to sequencers is `POST /v2/exports/accounts/third-party` with the
**sequencer account's email + password** and `app` (REACHINBOX, INSTANTLY, …), then
`POST /v2/exports/mailboxes` with `apps` and an id / status filter. No warm-up or reputation
endpoint in the package.

**Salesforge "forge" family** (`@salesforge/forge-mcp` 1.0.0, npm, repo under the SalesforgeAI
org; hosted at `https://mcp.salesforge.ai/mcp`, one header per product key, e.g.
`X-Mailforge-Key`): separate bases `api.mailforge.ai/public`, `api.infraforge.ai/public`,
`api.primeforge.ai/public` (Google / Microsoft mailboxes), Warmforge for warm-up.
- Mailforge / Infraforge: bulk domain availability (max 100), purchase, DNS get / put /
  bulk-dns, auto-renew per domain and in bulk, domain masking, mailbox purchase / patch /
  delete, bulk forwarding, credits balance (Infraforge), export to a third party or Salesforge.
- Primeforge: `GET /mailboxes/pre-warmed` and `POST /mailboxes/pre-warmed` — pre-warmed
  inventory is a purchasable SKU over API.
- Warmforge: mailboxes listed by status `warm | pending | disconnected | suspended`,
  `GET /mailboxes/{address}/warmup/stats`, and placement tests
  (`POST/GET/DELETE /placement-tests`, filter by `external_reference`). Request bodies are
  untyped in the MCP (`z.record`), so field names are still unknown.

**Instantly API v2** (vendor CLI `@instantlyai/cli` 0.2.7, npm, author Instantly.ai):
- account create takes `daily_limit`, `warmup` JSON (e.g. `{"limit":30,"increment":"disabled"}`),
  `enable_slow_ramp`, `sending_gap` (minutes), `inbox_placement_test_limit`, `provider_code`
  (1 = Google, 2 = Microsoft, 3 = Zoho);
- `POST /accounts/warmup/enable|disable` (account ids), `POST /accounts/warmup-analytics`
  (emails), `POST /accounts/test/vitals` (DNS / SMTP / IMAP checks), `GET /accounts/ctd/status`,
  `/accounts/{email}/pause|resume|mark-fixed`, `/accounts/analytics/daily`;
- placement tests: `POST /inbox-placement-tests` with `type` 0 = one-time / 1 = automated,
  `sending_method` 0 = from Instantly / 1 = external, subject, body, seed `emails`; results
  via `/inbox-placement-analytics` (by test id, by date, `deliverability-insights`) and
  `/inbox-placement-reports`.
- Warm-up length (search summaries of Instantly's blog, not opened): campaign-eligible after
  ≥ 2 weeks of warm-up **and** a health score above 90 %; 21–28 days "safer"; a 2026 benchmark
  post says 4–6 weeks before full volume; ≥ 2 inboxes per domain; keep warm-up running while
  production sends ramp. Reply.io's default ramp quoted as 10 / day, +3 daily, cap 100.

**EmailGuard** (`emailguard-cli` 0.1.9, npm, third-party author): `POST /api/v1/inbox-placement-tests`
returns a unique phrase plus seed addresses; you send the real email to the seeds and read
placement per provider later. Also SPF / DKIM / DMARC lookups, blacklist and SURBL checks,
Spamhaus domain / nameserver reputation, content spam check.

**What is measurable vs theatre (synthesis, not a vendor claim):**
- Measurable on your own data: bounces and complaints per mailbox / domain (the lab's 02
  complaint guardrail), replies, DNS state, blacklist / Spamhaus listings, seed placement
  for one specific message at one moment.
- Vendor-defined and not comparable across vendors: "warm-up health score", "warm" status,
  "pre-warmed". Warm-up pools send and rescue mail between their own users; that proves the
  mailbox works, not that cold prospects' filters will inbox it. Seed tests are a sample of a
  few seed inboxes, not your prospects' tenants (Microsoft 365 / Proofpoint rules differ).

## Apply
- Gate "mailbox goes live" in n8n on three readable facts, not on a calendar: warm-up days
  ≥ 14 (21+ for brand-new domains), Instantly `warmup-analytics` / `test/vitals` clean, and
  one seed placement test of the *actual* first-step copy (Instantly `type: 0`, or EmailGuard
  seeds for Smartlead / lemlist accounts). Log all three per mailbox with the date.
- Do not give Zapmail your sequencer password for the export if you can avoid it: the API
  export takes the sequencer login (email + password); prefer CSV export / OAuth connect in
  the sequencer, or a dedicated sequencer user you can revoke.
- When pricing infra for a client, compare per-mailbox cost **plus** domains (~$14 / year each)
  and any dedicated-IP fee; 100 mailboxes on 20 domains is the shape vendors quote — a burnt
  domain then takes 5 senders down at once, so keep the 2–3 mailboxes per domain rule.
- Re-run a seed placement test after copy changes, not just once at setup; keep the test id
  next to the campaign step so a drop can be traced to a copy version.

## Lab impact
- 02 capacity planner (`CapacityPolicy.warmup_days` = 14, date-only): add a readiness gate so
  a mailbox leaves `warmup` only with evidence — days elapsed **and** an optional vendor
  warm-up reading (kept as provenance with vendor + date, never compared across vendors) and
  an optional placement result for the current copy; missing evidence → `held`, not `ramp`.
  Pre-warmed mailboxes still start the ramp at `ramp_start`. Added to ROADMAP 02.
- 13 (MCP): a Zapmail-style export that needs a third-party password is a write the GTM MCP
  should never expose.

## Sources
- https://registry.npmjs.org/zapmail-mcp/-/zapmail-mcp-0.0.4.tgz — opened 2026-11-16 (unofficial)
- https://registry.npmjs.org/@salesforge/forge-mcp/-/forge-mcp-1.0.0.tgz — opened 2026-11-16
- https://registry.npmjs.org/@instantlyai/cli/-/cli-0.2.7.tgz — opened 2026-11-16
- https://registry.npmjs.org/emailguard-cli/-/emailguard-cli-0.1.9.tgz — opened 2026-11-16 (third-party)
- web search "Zapmail pricing per inbox Google Workspace mailboxes per domain warmup 2026" — search summary, not opened (sendkit.ai, woodpecker.co, prospeo.io, puzzleinbox.com results)
- web search "Mailforge Infraforge pricing per mailbox cold email infrastructure" — search summary, not opened (sendkit.ai, salesforge.ai directory, moderninbound.com, prospeo.io results)
- web search "Instantly warmup how long before sending new domain mailboxes per domain recommendation" — search summary, not opened (instantly.ai blog, warmy.io, how.reply.io results)
- https://zapmail.ai/pricing, https://www.mailforge.ai/pricing, https://instantly.ai/blog/slow-ramp-plan-for-new-cold-email-domains-deliverability/, https://support.google.com/a/answer/81126 — tried 2026-11-16, ENOTFOUND, not opened
