# R3 · MillionVerifier API shape and catch-all scoring (ZeroBounce AI scoring, "catch-all rescue" verifiers) (2026-11-15)

## Question
Two open R3 questions: (1) what MillionVerifier's API actually returns (result codes, the
error path, bulk flow, what is charged), and (2) what catch-all scoring offers in practice —
ZeroBounce AI scoring and activity data, and the newer "catch-all rescue" verifiers — what
the output looks like and what it costs per address.

## Findings
Method: every vendor docs host tried was DNS-blocked in this run (developer.millionverifier.com,
help.millionverifier.com, www.zerobounce.net, jentic.com; a live probe of
`api.millionverifier.com` got no connection). Packages were downloaded from
registry.npmjs.org and read, not executed: ZeroBounce's **own** SDK and MCP server, the
**Activepieces** MillionVerifier piece (maintained integration, not the vendor), a third-party
MillionVerifier MCP wrapper, and Giggal.ai's **own** n8n node. Nothing was run against a live
API; prices and score meanings beyond the packages are search summaries.

**MillionVerifier (from `@activepieces/piece-millionverifier` 0.2.0, 2026-11-05):**
- Single verify: `GET https://api.millionverifier.com/api/v3/?api=<key>&email=…&timeout=…`
  (key in the **query string**; timeout 2–60 s, default 20 per the newer action — the older
  action in the same piece defaults to 10). Response fields: `email`, `result`, `subresult`,
  `quality`, `resultcode`, `free`, `role`, `didyoumean`, `credits` (balance left),
  `executiontime`, `livemode`, `error`.
- `result` values used by the piece: `ok`, `catch_all`, `unknown`, `disposable`, `invalid`.
  A test key `API_KEY_FOR_TEST` "will return random results" — useful to wire a flow, useless
  to judge quality.
- **The error path is a 200 with `result: "error"`.** The piece throws only when the body has a
  non-empty `error` string; its older "Verify Email" action skips that check and hands back the
  body as a result. A third-party MCP wrapper (`@pipeworx/mcp-millionverifier` 0.2.1) reports
  an edge probe: invalid key → HTTP 200,
  `{"result":"error","resultcode":4,"error":"Apikey not found","credits":0,…}`
  (third-party claim; not reproduced here — the host was unreachable).
- Bulk: separate host `https://bulkapi.millionverifier.com/bulkapi` with the key as `key=`
  (not `api=`). `POST /v2/upload` (multipart `file_contents`) → `file_id`; poll
  `GET /v2/fileinfo` (status `in_queue_to_start` / `in_progress` / `paused` / `finished` /
  `canceled` / `error`; counts `ok`, `catch_all`, `disposable`, `invalid`, `unknown`,
  `reverify`, `credit` used, `estimated_time_sec`); `GET /v2/download` with filter `ok`,
  `ok_and_catch_all`, `unknown`, `invalid`, `all` or `custom` (+ `statuses`, `free`, `role`);
  `/stop` cancels for good; `/v2/delete` is permanent. **No webhook / callback** — completion
  is found by polling. `/credits` returns `credits`, `bulk_credits`, `renewing_credits`.
- Billing: the piece says upload "spends one credit per unique email"; the single call "spends
  one credit per call". MillionVerifier's help center reportedly says the API does not deduct
  credits for catch-all / unknown and bulk refunds them after the run (search summary, not
  opened — unchanged from the 2026-10-24 note). Reconciling the two is cheap: the response
  carries `credits`, so log it before/after a catch-all row.

**ZeroBounce (from `@zerobounce/zero-bounce-sdk` 4.0.0, 2026-11-11, and `@zerobounce/mcp`
0.3.1, 2026-11-11 — both vendor):**
- **Real-time AI scoring exists, not only bulk:** `GET|POST /v2/scoring` → `{email, score}` or
  `{error}`; MCP tool `score_email`. Bulk scoring: `/scoring/sendfile` → `filestatus` →
  `getfile` (CSV text in Node; JSON instead of a file — even on HTTP 200 — is raised as an
  error by the SDK).
- `validate` options: `activity_data` adds `active_in_days` (bucket such as `30` or `365+`)
  and `active_first_seen` (date); `catchall_domain` true/false/null; `verify_plus`;
  `domainInfo` adds `domain_website_exists` and WHOIS `domain_registrant_company_name`;
  `greylistProcessing` (with optional `callbackUrl`) returns a `message` instead of a
  result while ZeroBounce re-tries a greylisted server — call again in ~30 min.
- Sub-statuses in 4.0.0 now include `accept_all`, `role_based_accept_all`,
  `role_based_catch_all`, `gold`, `alias_address`, `mx_forward`, `alternate`, `allowed`,
  `blocked`, `toxic` and a new `ai_agent_mailbox`. Status set unchanged (`valid`, `invalid`,
  `catch-all`, `unknown`, `spamtrap`, `abuse`, `do_not_mail`).
- Fixed test addresses (`catch_all@example.com`, `greylisted@example.com`, …) return each
  status and are not charged — a free way to test every branch of an n8n flow.
- Hosted MCP (HTTP mode): each caller sends their own key (`X-ZeroBounce-Api-Key`); the
  server ignores any configured key, so a shared deployment cannot spend one account's credits.
- Score meaning (search summary, not opened): 0–10, where 0 means "could not score", not
  "worst"; one review quotes ZeroBounce data of ~40 % bounces at 0 vs ~1.3 % at 10; scoring is
  1 credit per address on top of validation (so ~2 credits per catch-all row).

**"Catch-all rescue" verifiers (Giggal.ai, `n8n-nodes-giggal` 0.1.5, 2026-08-18, vendor):**
claims a definite valid / invalid for catch-all, accept-all and Proofpoint / Mimecast-protected
addresses (vendor claim; no method described). Engineering facts from the node: 1 credit per
address, 300 requests / 15 min, batch jobs with an optional **idempotency key** (same key →
existing job), results paginated ≤ 500 and **deleted 48 h after completion (410 Gone)**.

## Apply
- **MillionVerifier in n8n / Clay HTTP: check the body, not the status code.** Route
  `result == "error"` or a non-empty `error` to a retry / alert branch — never to "unknown"
  or "invalid". Otherwise a revoked key quietly marks a whole list undeliverable.
- **Log `credits` from every MillionVerifier response** (before vs after) on the first
  50 catch-all rows to see whether catch-all / unknown are really free on your plan, instead
  of trusting the help-center line.
- **Score only the catch-all cohort, then cut by score.** Validate first (1 credit), send
  only `catch-all` rows to ZeroBounce `/v2/scoring` (+1 credit), treat score 0 as "unknown"
  not "bad", and pick the send cut-off from your own bounce rate by score band — the 40 % /
  1.3 % figures are vendor numbers quoted by a reviewer.
- **Build n8n branches against ZeroBounce's free test addresses** before spending credits;
  add a branch for `greylisted` with `greylistProcessing` (re-check after ~30 min) rather
  than dropping those rows as unknown.
- **If you try a catch-all rescue verifier, download results inside 48 h** and pass an
  idempotency key on batch submits so an n8n retry does not pay twice.

## Lab impact
- 04 ladder: add `EmailCheck.from_millionverifier` (result `ok` / `catch_all` / `unknown` /
  `disposable` / `invalid`; `result == "error"` or a non-empty `error` raises — a failed call
  is never a verdict). Added to `next_steps`.
- 04 ladder: ZeroBounce sub-status `ai_agent_mailbox` and `accept_all` / `gold` exist in SDK
  4.0.0; the status map needs no change (it keys on `status`), but the source comment cites
  `zerobouncesdk 2.2.4` and should cite the current set when that file is next touched.
- 04: a catch-all score (0–10, 0 = unscored) is a natural input for the ladder's
  catch-all cohort; kept as a known gap until a scored cut-off can be justified.

## Sources
- https://registry.npmjs.org/@activepieces/piece-millionverifier/-/piece-millionverifier-0.2.0.tgz — opened (dist) 2026-11-15
- https://registry.npmjs.org/@pipeworx/mcp-millionverifier/-/mcp-millionverifier-0.2.1.tgz — opened (README, tools.json) 2026-11-15
- https://registry.npmjs.org/@zerobounce/zero-bounce-sdk/-/zero-bounce-sdk-4.0.0.tgz — opened (README, d.ts, dist) 2026-11-15
- https://registry.npmjs.org/@zerobounce/mcp/-/mcp-0.3.1.tgz — opened (README) 2026-11-15
- https://registry.npmjs.org/n8n-nodes-giggal/-/n8n-nodes-giggal-0.1.5.tgz — opened (README) 2026-11-15
- MillionVerifier docs / help center (developer.millionverifier.com, help.millionverifier.com/payments-credits/refund-for-risky-emails): search summary, not opened (ENOTFOUND)
- ZeroBounce AI scoring range, credits and bounce-by-score (zerobounce.net/docs/ai-email-scoring, tlinky.com/zerobounce-review, prospeo.io/s/zerobounce-pricing-reviews-pros-and-cons): search summary, not opened
