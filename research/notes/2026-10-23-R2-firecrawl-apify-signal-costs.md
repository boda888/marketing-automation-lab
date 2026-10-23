# R2 · What site research and signal scraping cost with Firecrawl and Apify (2026-10-23)

## Question
For signal research (careers pages, news / about pages, job boards, LinkedIn data): when to
use Firecrawl's map / scrape / crawl / monitor versus Apify Store Actors, what each costs per
company or per 1,000 results, and what output shape reaches the LLM prompt.

## Findings
Method: firecrawl.dev, apify.com and api.apify.com were blocked by this run's network policy
(proxy 403 / ENOTFOUND). Both vendors keep their documentation in public GitHub repos, so the
docs were read from fresh clones: `firecrawl/firecrawl-docs` at `685d39c` (committed
2026-10-22) and `apify/apify-docs` at `fec2234` (committed 2026-10-23). Plan **prices in USD**
and individual Actor prices were not opened and are marked as search summaries.

**Firecrawl billing (docs `billing.mdx`):**

| Call | Credits | What it gives a signal workflow |
|---|---|---|
| `map` | **1 per call**, whatever the number of URLs (a 100,000-URL map is still 1 credit) | list of a site's URLs from sitemap + search results + cached crawls; `search` param filters (e.g. `careers`) |
| `scrape` | 1 per page | markdown / HTML / links of one URL |
| `crawl` | 1 per page; **default `limit` is 10,000 pages** | whole site by link traversal; `includePaths` / `excludePaths` are RE2 regex on the pathname |
| `search` | 2 per 10 results (+ scrape cost per result scraped) | web search, optionally scraped |
| JSON / `question` / `highlights` formats | **+4 per page** (so 5) | LLM extraction on Firecrawl's side |
| PDF | +1 per PDF page | |
| LinkedIn `/in/` or `/company/` URL | 5–40 per URL, priced by the data provider the team chose: FullEnrich 5, Data Legion base 10 / premium 40, Apollo 30; providers run in saved order, first hit ends | not a scrape: routed to enrichment providers; no record → `THIRD_PARTY_DATA_NOT_FOUND`, 0 credits |
| x.com URL | 30 (1 + 29 Grok) | Grok-generated summary, not raw posts |

- **A 403 or 404 page is charged** (1 credit) and comes back as a normal document; only
  `metadata.statusCode` tells you the target refused. No document at all → 0 credits.
- Plans by credits: Free 1,000 / month (10 req/min on scrape/map/search, 2 req/min on crawl,
  no pay-as-you-go → HTTP 402 when empty), Hobby 5,000, Standard 100,000, Growth 500,000,
  Scale 1,000,000; credits do not roll over (except annual Scale / Enterprise). Pay-as-you-go
  in $5 increments: 1,000 credits on Hobby … 5,000 on Scale, with a monthly cap.
- **Per-API-key spend limits** (credits per day / week / month) can be set in the dashboard.
- Batch-scrape and crawl credits are billed as pages finish, possibly minutes or hours after
  submission.
- Plan prices (search summary, not opened): Hobby $19, Standard $99, Growth $399, Scale $749
  per month billed monthly; the owner's 2026-09 slides said Apify $19 + Firecrawl $38.

**Change tracking and monitors (docs `features/change-tracking.mdx`, `features/monitoring.mdx`):**
add `changeTracking` (with `markdown`) to a scrape and each URL gets `changeStatus`
`new / same / changed / removed`, `previousScrapeAt`, `visibility` (`visible` or `hidden` =
URL still works but is no longer linked) and a `git-diff` or field-level `json` diff.
Snapshots do not expire and are scoped per team (optionally per tag). `/monitor` runs the
same check on a cron schedule (minimum 5 minutes) for pages, whole sites or a web-wide
search, and posts webhooks (2xx within 10 s, 3 retries at 1 / 5 / 15 min), email or Slack.
Each check pays for the underlying scrapes, +1 credit per changed page an optional "judge"
validates; `estimatedCreditsPerMonth` is returned on create. A check with no credits is
recorded as `skipped_no_credits` (not charged, not a failure, monitor keeps its schedule).

**n8n:** Firecrawl ships a verified node `@mendable/n8n-nodes-firecrawl` (Search, Scrape,
Crawl, Map, Interact, Agent) per `integrations/n8n.mdx`. Apify publishes
`@apify/n8n-nodes-apify` (npm, 0.9.0 on 2026-10-22).

**Apify Store pricing (docs `actors/running/store/index.md`, `runs_and_builds.md`):** the
docs now describe two models — *pay per event* (the author defines events such as "result"
or "actor start"; most include platform usage, some charge it separately) and *pay per
usage* (compute units, storage, proxies only). A run can carry a **maximum charge**
(`maxTotalChargeUsd`); the Actor stops gracefully and events beyond it are never charged.
Without a limit, the account's remaining usage is the only cap. Reading the dataset after
the run is ordinary platform usage. Unnamed datasets are deleted after the retention period
(the ten latest runs are kept).
Actor prices (search summary, not opened, all community Actors): LinkedIn job scrapers
listed from ~$0.30 to $5 per 1,000 jobs; one pay-per-event Actor at $0.005 per run start +
$0.58 per 1,000 jobs; a user reported one run spending over $40 — the case for a max charge.
No Apify-maintained LinkedIn jobs Actor was found.

## Apply
- **Careers / news signal per company for ~2–4 Firecrawl credits:** `map` with
  `search: "careers"` (1 credit) → `scrape` the 1–3 matching URLs as markdown (1 each) →
  extract hiring facts in your own LLM step (Clay AI column / n8n). Use Firecrawl's JSON
  format only when you don't run your own LLM: it makes each page 5 credits.
- **Never call `crawl` without `limit`** (default 10,000 pages) and with `includePaths`
  for the sections you need; set a per-API-key spend limit on the key n8n uses.
- **Check `metadata.statusCode` before using a scraped page as evidence** — a blocked or
  missing careers page is still billed and still returns "content"; route ≥ 400 to "Unknown",
  not to "no hiring", and stop retrying it.
- **Weekly "what changed on their site" signal:** scrape `/careers`, `/pricing`, `/about`
  with `changeTracking` (1 credit per page per check) and only send `changed` pages to the
  LLM; `removed` / `hidden` pages (e.g. a job page unlinked) are signals too. Use `/monitor`
  with a webhook into n8n instead of an n8n Cron when you want Firecrawl to schedule it.
- **On every Apify run set a maximum cost per run** (Console option or `maxTotalChargeUsd`)
  and check whether the Actor's event price includes platform usage before comparing it to
  another Actor's per-1,000 price.

## Lab impact
- 03 signal engine: a page-change source fits the existing evidence model — a `changed`
  diff on a first-party URL with `previousScrapeAt` gives evidence URL + date interval, and a
  document with HTTP status ≥ 400 must never count as evidence (it becomes `Unknown`). Added
  to the 03 "signal sources" item in the root `ROADMAP.md`.
- 04 enrichment: Firecrawl's LinkedIn routing is itself a provider waterfall (saved order,
  first hit ends, cost = sum of steps, `metadata.provider.steps`) — the same shape as the
  lab's `Waterfall` attempt log; useful as a reference when adding adapters.

## Sources
- https://github.com/firecrawl/firecrawl-docs/blob/685d39cdb7ffc53c05f23f96267e4e7c5ee37fb9/billing.mdx — opened (git clone) 2026-10-23
- https://github.com/firecrawl/firecrawl-docs/blob/685d39cdb7ffc53c05f23f96267e4e7c5ee37fb9/features/change-tracking.mdx — opened (git clone) 2026-10-23
- https://github.com/firecrawl/firecrawl-docs/blob/685d39cdb7ffc53c05f23f96267e4e7c5ee37fb9/features/monitoring.mdx — opened (git clone) 2026-10-23
- https://github.com/firecrawl/firecrawl-docs/blob/685d39cdb7ffc53c05f23f96267e4e7c5ee37fb9/features/map.mdx — opened (git clone) 2026-10-23
- https://github.com/firecrawl/firecrawl-docs/blob/685d39cdb7ffc53c05f23f96267e4e7c5ee37fb9/features/crawl.mdx — opened (git clone) 2026-10-23
- https://github.com/firecrawl/firecrawl-docs/blob/685d39cdb7ffc53c05f23f96267e4e7c5ee37fb9/integrations/n8n.mdx — opened (git clone) 2026-10-23
- https://github.com/apify/apify-docs/blob/fec2234b07de6c74af3c3d24421b797ddff9ddd6/sources/platform/actors/running/store/index.md — opened (git clone) 2026-10-23
- https://github.com/apify/apify-docs/blob/fec2234b07de6c74af3c3d24421b797ddff9ddd6/sources/platform/actors/running/runs_and_builds.md — opened (git clone) 2026-10-23
- https://registry.npmjs.org/@apify/n8n-nodes-apify — opened 2026-10-23
- Firecrawl plan prices: search summary, not opened (tinyfish.ai, activepieces.com, eesel.ai write-ups listed by the search)
- Apify LinkedIn jobs Actor prices: search summary, not opened (apify.com/meka.im/linkedin-jobs-scraper, apify.com/prodiger/linkedin-jobs-scraper, apify.com/truefetch/linkedin-job-search)
