# Insights the owner can apply

Newest first. Each entry: one line of what to do, why, and the source. Only add entries that
change something concrete (a Clay column, an n8n node, a sequencer or CRM setting, a buying
decision). If a later run disproves an entry, strike it through and say why — do not delete.

Format: `- YYYY-MM-DD · <channel> · <action> — <why>. Source: <URL or note>`

- 2026-10-23 · signals · Hiring / news signal for ~2–4 Firecrawl credits per company: `map` with `search: "careers"` (1 credit per call, however many URLs) → scrape only the 1–3 matching pages as markdown → extract in your own LLM step; Firecrawl's JSON format makes every page 5 credits, and `crawl` without `limit` may run to 10,000 pages. Source: research/notes/2026-10-23-R2-firecrawl-apify-signal-costs.md
- 2026-10-23 · signals · Before a scraped page counts as evidence, check `metadata.statusCode`: Firecrawl bills and returns 403 / 404 pages as normal documents, so a blocked careers page can read as "no hiring" — map ≥ 400 to Unknown and stop retrying it. Source: same note
- 2026-10-23 · signals · Put a spend cap on every scraping key/run: Firecrawl per-API-key credit limit (day / week / month) on the key n8n uses, Apify "maximum cost per run" (`maxTotalChargeUsd`) on every Actor run — events beyond the cap are never charged. Source: same note
- 2026-10-22 · sourcing · Connect Apollo to Claude only via the official server `https://mcp.apollo.io/mcp` (registry `io.github.apolloio/…`) — the registry also lists ~20 third-party "Apollo" servers and paid proxies that would hold your account. Source: research/notes/2026-10-22-R1-sourcing-mcp-servers.md
- 2026-10-22 · sourcing · In Apollo-via-agent prospecting: search first (no enrichment), enrich only the shortlist, and set `reveal_personal_emails` to false for B2B — Apollo's own `/apollo:prospect` and `sequence-load` skills enrich with it set to true. Source: same note (apolloio/apollo-mcp-plugin skills)
- 2026-10-22 · agents · Give an agent sequence-enrol / campaign tools only against a paused or manual-start campaign; Apollo's README warns enrolment "may start outbound", and HeyReach's MCP key covers the whole workspace. Source: same note
- 2026-10-21 · email · Verify emails in a separate step after the waterfall, even when the finder says "verified"; pause a sending domain when bounces pass ~3% — a provider's own check is context, not proof. Source: owner-supplied course slides (2026-09, unverified) + `.agent/PRACTICE.md`.
- 2026-10-21 · stack · Buy mailbox infrastructure first (it needs weeks of warm-up), data and LinkedIn tools only when the list is being built (~weeks 3–4), calling and visitor-ID only after first replies show the need; pay monthly at the start. Source: owner-supplied course slides (2026-09, unverified).
- 2026-10-21 · email · Plan mailboxes with spares: e.g. 10 inboxes over 4 domains when 6 are needed, so a burnt domain or a second hypothesis does not stop sending. Source: owner-supplied course slides (2026-09, unverified); check against `projects/02-deliverability-preflight` capacity planner.
