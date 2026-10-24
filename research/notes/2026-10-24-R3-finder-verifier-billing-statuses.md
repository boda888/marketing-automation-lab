# R3 · Email finders and verifiers: what is billed, what "verified" means, catch-all policy (2026-10-24)

## Question
For an email waterfall (Icypeas, Prospeo, LeadMagic, Findymail) followed by a separate
verifier (NeverBounce, ZeroBounce, MillionVerifier): which calls cost credits when nothing
is found, what status values each tool returns, how to treat catch-all / unknown results,
and what bounce rate should stop a domain.

## Findings
Method: every vendor website and docs host was blocked by this run's network policy (proxy
403 / ENOTFOUND for millionverifier.com, zerobounce.net, neverbounce.com, prospeo.io,
leadmagic.io, findymail.com, icypeas.com, smartlead.ai, support.google.com). The vendors'
**own published npm packages** (official MCP servers and n8n nodes, repository URLs under the
vendor's GitHub org) were downloaded from registry.npmjs.org and read, not executed. Hit
rates, plan prices and bounce thresholds come only from web-search summaries and are
labelled as such.

**What a finder call costs (first-party packages):**

| Tool | Package (version, date) | Billing rule stated in the package | Statuses / flags |
|---|---|---|---|
| Prospeo | `@prospeo/prospeo-mcp-server` 1.1.1 (2026-10-13); hosted `https://mcp.prospeo.io` (OAuth or `X-KEY`) | "Credits are only deducted when the requested contact data is actually returned"; 1 credit per matched email, 10 per email + mobile (email then free); no charge if no match or if `only_verified_email` is set and no verified email exists; search 1 credit per page of up to 25 that returns results; `search_suggestions` and account info free | `free_enrichment` per record says whether credits were taken; search previews carry email `status` `VERIFIED` / `UNVERIFIED` before you pay; errors `NO_MATCH`, `INVALID_DATAPOINTS`, `INSUFFICIENT_CREDITS`, `RATE_LIMITED`; bulk enrich up to 25 per call |
| Findymail | `n8n-nodes-findymail` 0.2.13 (2025-11-05), `findymail-mcp` 0.1.19 (2026-09-30) | 1 finder credit per *successful* email, 10 per found phone, 1 verifier credit per verification; `find_employees` 1 finder credit per contact (name + LinkedIn URL, no email) | README: "email addresses provided by Findymail are already verified and do not need to be verified again" (vendor claim) |
| LeadMagic | `n8n-nodes-leadmagic` 1.6.0 (2026-09-07) | costs "vary by endpoint and plan"; free `GET /v1/credits`; responses carry `credits_consumed` | README: requests time out after 30 s and are **not retried**; "a timed-out paid request may already have consumed credits; inspect its outcome before replaying it"; "Email Finder returns validated work emails; do not immediately validate them again" (vendor claim) |
| Icypeas | `n8n-nodes-icypeas` 1.0.1 (2023-09-28) | not stated in the package | node is three years old; no MCP package from Icypeas found on npm (only third-party wrappers) |

**What a verifier returns:**
- **NeverBounce** (`n8n-nodes-neverbounce-email-verification` 1.0.20, 2026-02-17, repo
  `github.com/NeverBounce/n8n-nodes-neverbounce`): `result` is one of `valid`, `invalid`,
  `disposable`, `catchall`, `unknown` (numeric codes 0–4 in that order), plus `flags` and
  `suggested_correction`; calls `GET https://api.neverbounce.com/v4/single/check`. The node
  sets `valid = (result == "valid")` but **also sets `email_verified: true` on every item it
  checked, whatever the result** — `email_verified` means "a check ran", not "deliverable".
  A syntactically invalid address throws a node error instead of returning `invalid`.
  With "include hints" it attaches content rules for the email copy (subject 30–60 chars, no
  ALL CAPS, branded HTTPS links only).
- **ZeroBounce** (`@zerobounce/mcp` 0.1.12, 2026-10-17): statuses `valid`, `invalid`,
  `catch-all`, `unknown`, `spamtrap`, `abuse`, `do_not_mail` (same set the lab already maps
  from the Python SDK); separate tools for credits, **activity data** (`/activity`: whether
  the address was active recently) and **bulk AI scoring** by CSV file — ZeroBounce's way to
  rank catch-all addresses.
- **MillionVerifier** (search summary, not opened): results `ok`, `catch_all`, `unknown`,
  `disposable`, `invalid`; its help center reportedly says the API does **not** charge for
  `catch_all` / `unknown` and bulk uploads refund them after the run; a third-party (LeadMagic)
  post says the refund can be withdrawn for accounts that verify many scraped lists.

**Waterfall order and hit rates (search summaries, not opened, mostly vendor-published):**
cheapest-first chains start with Icypeas, then Prospeo / Findymail / LeadMagic; single
providers are quoted at 40–77 % hit rate and 3–4 step waterfalls at 70–95 %. The one
head-to-head found (Anymail Finder, a competitor whose own product ranks first; 5,000 B2B
contacts) lists Findymail 70.9 % coverage / 3.3 % false positives, Icypeas 49.0 % / 0.5 %,
Prospeo 45.2 % / 3.7 %. No independent benchmark covering all four was found.

**Catch-all policy and bounce thresholds (search summaries, not opened):** vendor blogs agree
on < 2 % bounces as the target (Smartlead: rolling 14-day window, 2–4 % warning, > 4 % damage
outruns warm-up), measured **per mailbox**, not as a fleet average; catch-all cohorts sent
only when scored, tracked as their own cohort, suppressed above ~3 % bounces; blanket
suppression of catch-alls is said to drop 30–50 % of valid B2B contacts (vendor claim).
No mailbox provider publishes a bounce cutoff: Google's published bulk-sender numbers are
spam-rate (keep < 0.10 %, never ≥ 0.30 %), not bounces.

## Apply
- **Order the waterfall by "charged only on a hit", not by list price:** Prospeo (with
  `only_verified_email: true`) and Findymail bill only found emails, so a miss costs nothing;
  put any step that bills per lookup after them. In n8n, log Prospeo's `free_enrichment` /
  LeadMagic's `credits_consumed` per row to get cost per found email per provider.
- **Use Prospeo's search previews before enriching:** search results show whether a
  person's email is `VERIFIED` without paying for it (1 credit per page of 25) — filter on
  that, then bulk-enrich (25 per call) only those rows.
- **Never filter on `email_verified` from the NeverBounce n8n node** — it is true for
  invalid and catch-all addresses too. Branch on `verification_result.status` (or
  `.valid`), and turn on "continue on fail" so one malformed address does not stop the run.
- **Do not re-run a timed-out LeadMagic (or any paid) request blindly:** check credits /
  the stored result first; the node does not retry and the first call may already be billed.
- **Treat catch-all as its own cohort:** keep `valid` and `catch-all` in separate sequences,
  watch bounces per mailbox on a rolling 14-day window, and pause the catch-all cohort first
  when it passes ~3 %; "the finder says verified" (Findymail, LeadMagic) is the vendor's
  claim and the owner's practice notes still say to verify separately.

## Lab impact
- 04 ladder: NeverBounce's full result set is now known from NeverBounce's own package, so
  `EmailCheck.from_neverbounce` can map it (closing the gap left on 2026-10-22).
- 04 next steps: a per-record credit / cache step should key on "charged only on a hit" vs
  "charged per lookup" per provider and record a timed-out paid call as "outcome unknown",
  never as a free miss to retry.
- 02 deliverability: a bounce guard should be per mailbox on a rolling window, with
  catch-all sends counted as their own cohort (added to the 02 follow-ups in `next_steps`).

## Sources
- https://registry.npmjs.org/@prospeo/prospeo-mcp-server/-/prospeo-mcp-server-1.1.1.tgz — opened (README + dist) 2026-10-24
- https://registry.npmjs.org/@prospeo/n8n-nodes-prospeo/-/n8n-nodes-prospeo-1.0.0.tgz — opened 2026-10-24
- https://registry.npmjs.org/n8n-nodes-findymail/-/n8n-nodes-findymail-0.2.13.tgz — opened 2026-10-24
- https://registry.npmjs.org/findymail-mcp/-/findymail-mcp-0.1.19.tgz — opened 2026-10-24
- https://registry.npmjs.org/n8n-nodes-leadmagic/-/n8n-nodes-leadmagic-1.6.0.tgz — opened 2026-10-24
- https://registry.npmjs.org/n8n-nodes-icypeas/-/n8n-nodes-icypeas-1.0.1.tgz — opened 2026-10-24
- https://registry.npmjs.org/n8n-nodes-neverbounce-email-verification/-/n8n-nodes-neverbounce-email-verification-1.0.20.tgz — opened (README + dist) 2026-10-24
- https://registry.npmjs.org/@zerobounce/mcp/-/mcp-0.1.12.tgz — opened 2026-10-24
- MillionVerifier billing for catch-all / unknown: search summary, not opened (help.millionverifier.com/payments-credits/refund-for-risky-emails, leadmagic.io/blog/does-millionverifier-charge-for-catch-all)
- Waterfall order and hit rates: search summary, not opened (deepline.com/blog/best-email-enrichment-tools, anymailfinder.com/email-finder-benchmark, gtm-stack.deepline.com)
- Bounce thresholds and catch-all cohorts: search summary, not opened (smartlead.ai/blog/cold-email-bounce-rate, hub.causo.ai/guides/verified-vs-deliverable-email-2026, bouncezero.io/catch-all-email-verification-2026)
