# Tool catalogue

One row per tool. `?` = not researched yet. Prices are monthly, in USD, with where and when
they were seen; they are **not verified** until a run opens the vendor's pricing page and
updates the row (then `Source` becomes the URL + date).

Seed rows come from two inputs the owner supplied on 2026-10-21: course slides listing a
starter outbound stack with vendor prices "from vendor sites, September 2026, monthly"
(source `slides-2026-09`), and the tools used in the owner's own client work (`practice`).

| Tool | Layer | Price seen | Source | API / webhooks | MCP | n8n node | Notes |
|---|---|---|---|---|---|---|---|
| Apollo | sourcing, email | $59 | slides-2026-09 | yes (people/company search, match, sequences) | official remote `mcp.apollo.io/mcp`, OAuth | ? | filters + email in one place; enrich + sequence-enrol tools spend credits / may start outbound (R1 note 2026-10-22) |
| AI Ark | sourcing, lookalikes | $146 together with MoltSets; lookalikes from $49 | slides-2026-09 | ? | yes (slide claim); not found in MCP registry 2026-10-22 | ? | agent can search companies/people directly |
| MoltSets | sourcing, email | see AI Ark | slides-2026-09 | ? | yes (slide claim); not found in MCP registry 2026-10-22 | ? |  |
| Clay | enrichment, research, orchestration | $167 | slides-2026-09 | ? | no first-party entry in MCP registry (2026-10-22) | ? | flexible, credit-based; owner's main tool |
| Freckle | enrichment | $99 | slides-2026-09 | ? | ? | ? | pays per found contact (slide claim) |
| Apify | signals (scrapers) | $19 (+Firecrawl $38) | slides-2026-09; Actor pricing models from apify-docs 2026-10-23 | yes | official `mcp.apify.com` (OAuth / token) + npx | `@apify/n8n-nodes-apify` 0.9.0 (npm, 2026-10-22) | job boards, LinkedIn data via community Actors: pay per event or pay per usage; set max cost per run (`maxTotalChargeUsd`); LinkedIn jobs Actors ~$0.30–5 / 1,000 (search summary) (R1 2026-10-22, R2 2026-10-23) |
| Firecrawl | site crawling for research | see Apify; plans by credits: free 1k, Hobby 5k, Standard 100k / month (USD prices not opened) | slides-2026-09; credits from firecrawl-docs `billing.mdx` 2026-10-23 | yes (+ webhooks for crawl / monitor) | official `mcp.firecrawl.dev/v2/mcp` (key / OAuth; keyless scrape/search/parse) | verified `@mendable/n8n-nodes-firecrawl` | map 1 credit per call, scrape / crawl 1 per page, JSON +4, 403/404 pages billed; changeTracking + `/monitor`; LinkedIn URLs routed to providers at 5–40 credits (R1 2026-10-22, R2 2026-10-23) |
| HarvestAPI | LinkedIn signals | pay per request | slides-2026-09 | ? | ? | ? | posts, reactions, jobs |
| Bright Data | LinkedIn / web data | pay per request | slides-2026-09 | ? | ? | ? | |
| Icypeas | email finding | $19 (+MillionVerifier $38) | slides-2026-09 | ? | ? | ? | |
| MillionVerifier | email verification | see Icypeas | slides-2026-09 | ? | ? | ? | verify as a separate step; bounce > 3% = stop the domain (slide rule) |
| Zapmail | mailbox infrastructure | $39 for 10 inboxes, then $3.50 each | slides-2026-09 | ? | ? | ? | buys domains, sets SPF/DKIM/DMARC + redirect |
| Instantly (Hypergrowth) | sequencer, warm-up | $97 up to 25,000 contacts ($47 plan: 1,000 contacts) | slides-2026-09 | ? | community only in registry (2026-10-22) | ? | unlimited inboxes on plan |
| Smartlead | sequencer | ? | practice | ? | community only in registry (2026-10-22) | ? | campaign API used from n8n in client work |
| Lemlist | sequencer, multichannel | ? | practice | ? | not found (registry query timed out, 2026-10-22) | ? |  |
| HeyReach | LinkedIn automation | $79 per sender | slides-2026-09 | ? | official `mcp.heyreach.io/mcp`, workspace `X-MCP-KEY` | ? | multi-account; campaigns, leads, unibox, webhooks, analytics (R1 note 2026-10-22) |
| Aimfox | LinkedIn automation | $49 | slides-2026-09 | ? | ? | ? | limits + auto-withdraw invites |
| Flygen | rented LinkedIn sender profiles | from $40 | slides-2026-09 | ? | ? | ? | warmed 3+ months (slide claim); account-risk topic for R5 |
| RB2B | website visitor ID | $0 → $79 | slides-2026-09 | ? | ? | ? | |
| JustCall | dialer | from $78 (2 seats) | slides-2026-09 | ? | ? | ? | |
| Twenty | CRM (open source) | $12 per seat | slides-2026-09 | ? | ? | ? | API + custom fields; stop-list and dedupe live here |
| HubSpot | CRM | ? | practice | ? | community only in registry (2026-10-22) | ? |  |
| Attio | CRM | ? | practice | ? | community only (`kesslerio/attio-mcp-server`) | ? |  |
| Airtable | ops database | ? | practice | ? | official remote `mcp.airtable.com/mcp` | ? |  |
| n8n | orchestration | ? | practice | n/a | ? | n/a | |
| Trestle | phone validation | ? | practice | ? | ? | ? | line type, carrier, activity score |
| Claude (Pro / Max) | research, copy, replies | $20 / $100 | slides-2026-09 | yes | client | ? | Max when the agent works inside data / CRM via connectors (slide claim) |
| Google Sheets / Notion | dashboard | $0 | slides-2026-09 | yes | ? | yes | weekly funnel: plan vs fact |
