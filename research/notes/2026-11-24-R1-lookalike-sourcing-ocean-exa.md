# R1 · Lookalike sourcing from best customers: Ocean.io, Clay, Exa, Explorium (2026-11-24)

## Question
How do practitioners turn "our 10 best customers" into a new account list — which tools do it,
what goes in (seed format, limits), what comes out, what it costs per company, and which
quality checks stop a bad seed from burning credits on a useless list?

## Findings
All vendor documentation hosts (ocean.io, app.ocean.io, docs.exa.ai, developers.explorium.ai,
docs.clay.com, university.clay.com, community.clay.com) were DNS-blocked from this session on
2026-11-24. The API facts below come from published packages that call the APIs (read, not run);
prices and Clay behaviour are **search summaries**.

**Ocean.io — the dedicated lookalike API** (from `n8n-nodes-ocean` 1.0.8, npm 2026-07-02, and
`ocean-agent-cli` 0.1.12, npm 2026-05-31; both by the same third-party author, "aligned with
ocean-agent-cli" — not Ocean's own code):
- Input: `POST https://api.ocean.io/v3/search/companies` with `companiesFilters.lookalikeDomains`
  = list of seed root domains (the node strips `https://`, `www.`, paths, ports and rejects
  anything that fails a domain regex). Filters combine with it: `employeeCountLinkedin
  {from,to}` + `companySizes` buckets (`2-10` … `500000+`), `primaryLocations.includeCountries`
  (lowercase ISO-2), `industries {industries:[…], mode:"anyOf"}`, `includeDomains`.
  People side: `POST /v3/search/people` with `lookalikePeople`, `peoplePerCompany` and an
  optional `companiesFilters` block. Auth header `X-Api-Token`. The v2 search is deprecated.
- Paging: `size` 1–100 per call and a `searchAfter` cursor returned by the API. The node's
  "Return All" loops up to **100 pages × 100 = 10,000 companies**, and if it reaches page 100
  it **throws** — the rows fetched (and paid for) are not passed on in n8n.
- Cost as written in the node's UI strings: lookalike company search **1 credit per company
  returned**, people search 1–3 credits per person by plan, single company enrich 1, person
  enrich 3, autocomplete 0.1, `GET /v2/credits/balance` free (also returns
  `dailyLimitRateLeft` = API calls left today, per the CLI's AGENTS.md). Email / phone reveal
  use separate credit pools and are **async** (`oceanIds` + `webhookUrl`; results arrive on the
  webhook). A search summary of Ocean's own developer docs quotes different illustrative rates
  (0.2 credits per search result) — **conflict, unverified**; measure with the free balance call.
- Errors come back as `detail` strings (`Insufficient standard credits`, `… email credits`,
  `… phone credits`) or FastAPI-style validation arrays; no retry / 429 handling in the node.
- MCP: the CLI ships an MCP server (`ocean mcp`) exposing `search_companies`,
  `search_people`, `enrich_*`, `reveal_*`, `credits_balance`, `autocomplete_*`; the context
  file tells agents to run `credits_balance` before bulk search.
- No `excludeDomains` field appears anywhere in either package. Ocean's own workflow page
  (search summary) sends the seeds as both `lookalikeDomains` and `excludeDomains`, which only
  removes the seeds — existing customers and open pipeline still come back unless you filter.

**Clay** (search summaries only): Ocean.io is a Clay integration ("Find lookalike companies
with Ocean.io"), billed in Clay credits per result; up to **10 seed domains** per lookalike
query; since a March 2026 changelog Clay previews lookalike results before credits are spent.
Clay's guide suggests a closed-won trigger: CRM deal → Clay row → lookalikes. A Clay community
thread reports ~500 credits lost on one Ocean table whose seed list contained investment firms
(e.g. a broker's domain) — one off-profile seed pulls the whole neighbourhood off-ICP.

**Exa** (from `exa_py/api.py` on GitHub master, exa-py 2.29.0 on PyPI 2026-11-24):
`find_similar(url, num_results, include_domains, exclude_domains, exclude_source_domain,
category="company", …)` still exists but is marked **DEPRECATED** — "Use
`search("pages similar to " + url)` instead". It returns web pages, not firmographic rows:
neighbours by how a site reads, with no headcount / country filter, so results still need
enrichment + ICP scoring. Useful as a cheap second opinion on niche B2B where a company
database is thin.

**Explorium** (from `explorium-mcp-server` 0.2.11 wheel, PyPI): the MCP exposes
`match_businesses`, `fetch_businesses`, firmographics / technographics / events enrichments
over `https://api.explorium.ai/v1` — **no lookalike tool**. Its developer reference lists a
"lookalike companies" enrichment (search result title only, page not opened).

**Apollo / AI Ark**: Apollo has a lookalike feature in the app and practitioners describe it as
"more volume, less precise" than Ocean (search summary); no API shape found this run. AI Ark
remains a slide claim ("lookalikes from $49", STACK row) — not researched further.

**What a sound seed→list flow looks like** (combining the above):
1. Seeds = 5–10 closed-won accounts from one segment (same size band + motion); never mix
   segments in one query, never seed with holding / investment / parent domains.
2. Preview or `size` ≤ 25 first; read the first page by hand before paging further.
3. Page with `searchAfter` and a hard row cap per run, storing the cursor between runs.
4. Drop customers, open deals, suppressed and already-sourced domains **before** people
   search / reveal (the expensive step), then score the rest with your own ICP rules.
5. Track hit rate: share of returned accounts that pass ICP scoring — that is the seed list's
   quality metric, not the vendor's similarity score.

## Apply
- **n8n + Ocean node: never use "Return All".** Set Limit ≤ 100, store `searchAfter` in the
  ops table and loop yourself with a row cap per run; at page 100 the node throws after you
  have already paid up to 10,000 credits. Call `credits_balance` (free) before and after the
  first 100 rows to learn your real per-result cost — the node says 1, a docs summary says 0.2.
- **Seed hygiene column in Clay**: before the lookalike action, a formula flags seeds whose
  industry / size band differ from the segment's mode, and you run only the clean 5–10.
  Run one segment per query (e.g. "51–200 SaaS, EU"), not "all our best customers".
- **Exclude before you pay for people**: after lookalikes land, a lookup against CRM
  (customers, open deals, DNC) and your previous lookalike pulls removes known domains;
  only then run people search / email reveal. Seeds-only exclusion is not enough.
- **Measure ICP pass rate per seed set** (rows passing your score ÷ rows returned) and
  keep the seed sets with the highest rate; that number is the real "lookalike quality".
- Exa `findSimilar` code in existing n8n / Clay HTTP columns should move to `/search` with
  "pages similar to <url>" before the deprecated method is removed.

## Lab impact
04's "dedupe / match against existing CRM records" item now names the sourcing side: newly
sourced or lookalike accounts are matched against customers / open deals / suppression and
prior pulls *before* any per-person paid lookup, and 01 scoring reports an ICP pass rate per
seed set. No new project.

## Sources
- https://registry.npmjs.org/n8n-nodes-ocean/-/n8n-nodes-ocean-1.0.8.tgz — opened 2026-11-24 (`RequestBuilders.js`, `OceanPayloads.js`, `GenericFunctions.js` paging, `CompanyDescription.js`, credit strings)
- https://registry.npmjs.org/ocean-agent-cli/-/ocean-agent-cli-0.1.12.tgz — opened 2026-11-24 (`OCEAN.md`, `README.md`, `AGENTS.md`)
- https://raw.githubusercontent.com/exa-labs/exa-py/master/exa_py/api.py — opened 2026-11-24 (`find_similar` deprecation, `Category`)
- https://pypi.org/pypi/exa-py/json — opened 2026-11-24 (version 2.29.0)
- https://files.pythonhosted.org/packages/8a/ac/8785325474d0b6675a19ef64f5c59c93df1a1b4cd6c09c5ef1faac48d596/explorium_mcp_server-0.2.11-py3-none-any.whl — opened 2026-11-24 (`tools/businesses.py`)
- https://www.clay.com/changelog/ocean-io — search summary, not opened (preview before credits, March 2026)
- https://clay.com/university/lesson/company-lookalikes — search summary, not opened (up to 10 seed domains)
- https://community.clay.com/x/support/7xc4i6a9doni/issue-with-oceanio-table-result-wasting-500-credit — search summary, not opened (host blocked)
- https://app.ocean.io/docs/getting-started/workflows — search summary, not opened (seeds as `lookalikeDomains` + `excludeDomains`; 0.2 credits / result illustrative)
- https://developers.explorium.ai/reference/businesses/enrichments/lookalike-companies.md — search summary, not opened
