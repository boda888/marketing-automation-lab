# R1 · Which sourcing tools an agent can drive over MCP (2026-10-22)

## Question
Which account / contact sourcing tools (and the tools right next to them: scraping, the ops
database, LinkedIn) ship an MCP server or API that Claude can drive directly, and what that
changes in the workflow — what an agent can read, what spends credits, what starts outbound.

## Findings
Method: vendor websites (apollo.io, docs.apollo.io, clay.com, university.clay.com) were
blocked by this run's network policy (proxy 403), so the answer comes from the **official MCP
Registry** API (`registry.modelcontextprotocol.io/v0/servers?search=…`, queried 2026-10-22),
the vendors' own GitHub repos and the npm registry. The registry search matches server
*names*; a vendor can publish under any name or not publish at all, so "not found" below
means "not in the registry under an obvious name", not "has no MCP".

**First-party servers found in the registry (latest versions):**

| Vendor | Registry name | Transport / endpoint | Auth | Published |
|---|---|---|---|---|
| Apollo | `io.github.apolloio/apollo-mcp` 0.1.1 (+ `io.github.apolloio/mcp`, npm `@apolloio/mcp`) | remote, streamable HTTP, `https://mcp.apollo.io/mcp` | Apollo OAuth, acts with the user's permissions | 2026-07-20 |
| Apify | (repo `apify/apify-mcp-server`, npm `@apify/actors-mcp-server` 0.19.3, 2026-10-21) | remote `https://mcp.apify.com` (SSE endpoint removed) or stdio via npx | OAuth or `Authorization: Bearer <APIFY_TOKEN>` | — |
| Firecrawl | (repo `firecrawl/firecrawl-mcp-server`, npm `firecrawl-mcp` 3.33.4, 2026-10-20) | remote `https://mcp.firecrawl.dev/v2/mcp`, OAuth variant `/v2/mcp-oauth`, search-only `/v2/mcp-search`; stdio / Docker | API key or OAuth; keyless tier for scrape / search / parse only, rate-limited | — |
| Airtable | `com.airtable/mcp` 0.1.0 | remote, `https://mcp.airtable.com/mcp` | not stated in the entry | 2026-05-08 |
| HeyReach | `io.heyreach/mcp` 1.0.1 — campaigns, leads, unibox, webhooks, analytics | remote, `https://mcp.heyreach.io/mcp` | workspace-scoped `X-MCP-KEY` header, generated in the app | 2026-09-24 |

(The Apify and Firecrawl registry queries timed out after 60 s, so their rows come from the
vendors' GitHub READMEs and npm metadata, not from the registry entry.)

**Not found as first-party in the registry:** Clay (40 "clay" matches, none from Clay),
AI Ark, MoltSets, Instantly, Smartlead, Lemlist, HubSpot, Attio. Instantly / Smartlead /
HubSpot / Attio have **community** servers only (e.g. `kesslerio/attio-mcp-server`, a
third-party "Clay → Instantly/Smartlead push" server, many HubSpot wrappers). Apollo alone
has ~20 third-party "Apollo" servers (wrappers, paid proxies) next to the official one —
anyone can register a server with a vendor's name in its description.

**What the Apollo server actually lets an agent do** (tool list from the vendor README,
confirmed through its bundled skills; the remote server may expose more):
- read: `apollo_mixed_people_api_search`, `apollo_mixed_companies_search`,
  `apollo_emailer_campaigns_search`, `apollo_email_accounts_index`, `apollo_analytics_sync_report`;
- enrich (spends credits): `apollo_people_match`, `apollo_people_bulk_match`,
  `apollo_organizations_enrich`, `apollo_organizations_bulk_enrich`;
- write: `apollo_contacts_create`;
- **outbound**: `apollo_emailer_campaigns_add_contact_ids` — "may start outbound depending on
  sequence settings" — and `…_remove_or_stop_contact_ids`.
The README classifies every tool by "data effect" and "outreach / external action" and asks
for review before credit-spending and sequence-loading actions.

**Apollo's own skills, read as a workflow** (`/apollo:prospect`, `/apollo:sequence-load`):
ICP in plain English → company search (25 per page) → bulk-enrich top 10 companies →
people search scoped to those domains → **credit warning** → `people_bulk_match` in batches
of 10 → ranked table. `sequence-load` resolves the sequence by name and shows all sequences
when there is no match or several (good), asks for the sending account when there are
several, previews candidates and waits for confirmation, then enriches, creates contacts
with `run_dedupe: true` and enrolls. Two things to notice: both skills enrich with
`reveal_personal_emails: true`, and the "ICP Fit" column is a count of 4 criteria matched
(Strong = 4, Good = 3, Partial = 2) — no evidence, no Unknown state.

**Apify** turns any Store Actor into a tool: by default `search-actors`,
`fetch-actor-details` (input schema, README, pricing), `call-actor`, run / dataset readers,
docs search and two web Actors; writing tools (schedules, tasks, builds, `delete-actor`)
are opt-in via a `tools=` list. Agents can pay without an account (AGI prepaid tokens,
x402 / Skyfire per run). **Firecrawl** exposes 27 tools on the full profile (scrape, map,
crawl with `limit` / path filters, search, interact, monitors, `firecrawl_credit_usage`);
its README notes that `firecrawl_search` with `scrapeOptions` bills every result as a scrape.

Prices: not checked this run (pricing pages blocked); STACK prices stay `slides-2026-09`.

## Apply
- Connect the **official** Apollo server (`https://mcp.apollo.io/mcp`), not one of the
  look-alikes; check the registry name (`io.github.apolloio/…`) or the vendor repo before
  adding any MCP server that will hold a data or sending account.
- In Apollo-via-Claude prospecting, run the free search steps first, then enrich only the
  shortlist, and override `reveal_personal_emails` to `false` for B2B work unless there is a
  lawful reason to collect personal addresses (the vendor skill sets it to `true`).
- Never give an agent the sequence-enrol tool on a sequence that sends automatically; point
  it at a paused / manual-start sequence and let a human start it after the preview.
- For Apify inside Claude, pass an explicit `tools=` list (search, details, call, dataset
  reads) so the agent cannot create schedules or delete Actors; for Firecrawl use `map` +
  targeted `scrape` instead of `search` + `scrapeOptions` when cost matters, and read
  `firecrawl_credit_usage` after a research batch.
- HeyReach's MCP key is workspace-scoped: one key gives the agent every sender and campaign
  in that workspace — use a separate workspace for experiments.

## Lab impact
Project 13 (GTM MCP server): every tool declares its effect class — `read`,
`spends_credits` (with an estimate before the call) or `outbound` — and the non-read classes
need an explicit confirm / dry-run flag; tool calls reuse 04's fixture gate so `.example`
records never reach paid tools. Root `ROADMAP.md` item 13 edited accordingly. Apollo
adapter in 04 can now take request field names from the vendor skill files
(`first_name`, `last_name`, `domain`, `reveal_personal_emails`, batches of 10) instead of
search summaries — still to be checked against docs.apollo.io when it is reachable.

## Sources
- https://registry.modelcontextprotocol.io/v0/servers?search=apollo — opened 2026-10-22 (also `search=` airtable, heyreach, clay, instantly, smartlead, attio, hubspot, com.hubspot, firecrawl, io.github.apify; apify / firecrawl / lemlist re-queries timed out)
- https://raw.githubusercontent.com/apolloio/apollo-mcp-plugin/main/README.md — opened 2026-10-22
- https://raw.githubusercontent.com/apolloio/apollo-mcp-plugin/main/server.json — opened 2026-10-22
- https://raw.githubusercontent.com/apolloio/apollo-mcp-plugin/main/skills/prospect/SKILL.md — opened 2026-10-22
- https://raw.githubusercontent.com/apolloio/apollo-mcp-plugin/main/skills/sequence-load/SKILL.md — opened 2026-10-22
- https://raw.githubusercontent.com/apify/apify-mcp-server/master/README.md — opened 2026-10-22
- https://raw.githubusercontent.com/firecrawl/firecrawl-mcp-server/main/README.md — opened 2026-10-22
- https://registry.npmjs.org/@apify/actors-mcp-server , https://registry.npmjs.org/firecrawl-mcp , https://registry.npmjs.org/@apolloio/mcp — opened 2026-10-22 (latest versions + dates)
- https://www.apollo.io/pricing , https://docs.apollo.io , https://www.clay.com/pricing , https://university.clay.com — blocked (proxy 403), not opened
