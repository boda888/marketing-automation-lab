# R2 · Hiring and funding signals you can act on within a week (2026-11-14)

## Question
Which hiring and funding sources are fresh enough to act on within a week, and what date
can a signal honestly carry? Covered here: public ATS job boards (Greenhouse, Lever, Ashby)
for hiring, and SEC Form D filings for US funding. Tech-change and "new leader" signals are
still open (see research ROADMAP).

The live endpoints (`boards-api.greenhouse.io`, `api.lever.co`, `api.ashbyhq.com`,
`efts.sec.gov`) and the docs hosts (`developers.greenhouse.io`, `developers.ashbyhq.com`,
`sec.gov`, `ecfr.gov`, `law.cornell.edu`, `theirstack.com`, `data.crunchbase.com`) were
blocked from this session (proxy 403 / DNS). The findings below come from the Greenhouse
docs source on GitHub, Lever's own README on GitHub, and two open-source packages
read from npm and PyPI. Nothing was run against a live board or EDGAR.

## Findings

### Hiring: public ATS boards are free and fresh, but their dates mean different things
All three expose unauthenticated JSON for one employer's board. You must already know the
board token (from the careers URL): none of them lets you list every employer.

| Board | Endpoint | Date on a listing | What the date means |
|---|---|---|---|
| Greenhouse | `GET boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true` | list: `updated_at`; single job `GET …/jobs/{id}`: `first_published` + `updated_at` | `updated_at` is the last edit, **not** when the job was posted; `first_published` appears only in the single-job response, so getting it costs one extra call per job. Prospect posts have `internal_job_id: null` |
| Lever | `GET api.lever.co/v0/postings/{site}?mode=json&skip=&limit=` (EU: `api.eu.lever.co`) | none documented | the README's posting fields include no created / updated date; it documents only a 429 limit for application POSTs (2 / s), with no limit stated for GETs |
| Ashby | `GET api.ashbyhq.com/posting-api/job-board/{name}?includeCompensation=true` | `publishedAt` | last publication, per the package below (a re-published old role looks new); unlisted entries have `isListed: false`; the docs don't guarantee a stable id, so the package keys jobs by `jobUrl` |

`@absolutejs/job-sources` 0.2.0 (npm, README says "verified November 5, 2026") normalises
the three into one shape. Its code sets `publishedAt` only for Ashby and `updatedAt` only for
Greenhouse. It treats an empty board and an inaccessible board as different results
(`complete / partial / failed`, HTTP status and `retry-after` kept), and it warns that a
job missing from the feed "is an observation, not proof that an employer closed a role".

**Consequence:** the reliable hiring date is *your own first sighting*. Pull each board on a
schedule, store the set of job keys, and a new key is a hiring event dated in the interval
(previous successful pull, this pull]. With a weekly pull that is at most 7 days old, which
is fresher than most job-aggregator data. A failed or partial pull must not turn every job
into "new" next week, and must not mark jobs as "closed".

### Funding: Form D is the only free, dated, near-real-time US source
- Deadline: Form D is due **no later than 15 calendar days after the first sale**; if that
  day is not a business day, it moves to the next business day. An issuer may also file before
  the first sale (search summary of 17 CFR 230.503, not opened: the eCFR / LII / SEC hosts
  were blocked). So a new filing is about 0–3 weeks behind the money being committed.
- Fields a parser gets (edgartools 6.1.0 `edgar/offerings/exempt/formd.py`):
  `typeOfFiling.newOrAmendment.isAmendment`, `typeOfFiling.dateOfFirstSale`,
  `offeringSalesAmounts.totalOfferingAmount` / `totalAmountSold`, `industryGroup.industryGroupType`,
  `typesOfSecuritiesOffered.isEquityType` / `isPooledInvestmentFundType`,
  `durationOfOffering.moreThanOneYear`, and the `signatureDate` for each signature.
  The parser has a comment that `isAmendment` was once read the wrong way round (gh #1192):
  a Form D/A amends an earlier filing and is not a new round.
- Limits: US issuers only, and only those relying on Regulation D. Many filings are
  pooled investment funds, not companies. Amounts can say "Indefinite". The source gives no
  investor names, which people often expect from Crunchbase-style data.

## Apply
- **Hiring signal in n8n:** for each account keep its careers board (`provider`, `token`) in the
  ops table, set from the careers URL (`jobs.lever.co/<site>`, `jobs.ashbyhq.com/<name>`,
  `boards.greenhouse.io/<token>` or `job-boards.greenhouse.io/<token>`). Run a weekly Cron →
  HTTP Request (Batching on, one board per item) → compare job keys to last week's in
  Supabase / Airtable → a new key is the signal, dated "between last pull and today". Write the
  key set only after a pull that completed without errors.
- **Copy rule:** never write "you just posted X" from Greenhouse `updated_at` or Ashby
  `publishedAt`. An edited or re-published year-old role passes both. Use first-seen, or
  Greenhouse `first_published` from the single-job call.
- **Funding:** for "raised recently" plays in the US, filter Form D on `isAmendment = false`,
  `isPooledInvestmentFundType = false` and `dateOfFirstSale` in the last 30 days. Use the
  filing as evidence (its EDGAR URL), not a news article that repeats it.
- Track the share of pulls that are `failed` / `partial` per week. A rising share means token
  drift (the company moved ATS), and those accounts silently stop producing signals.

## Lab impact
Edits the existing 03 `signal sources` ROADMAP item and the 02/03 `next_steps` entry: an ATS
board adapter that produces hiring events from a first-seen diff, dated over the interval
(previous complete pull, this pull]. Greenhouse `updated_at` and Ashby `publishedAt` stay
provenance and are never the event date. A partial or failed pull produces `Unknown` and
does not count as absence. A Form D adapter maps `isAmendment` filings onto the original
round (dedupe), drops pooled funds, and dates the event by `dateOfFirstSale` (day precision).
Both fit 03's existing `EventDate` interval model and `dedupe()`.

## Sources
- https://github.com/grnhse/greenhouse-api-docs/blob/master/source/includes/job-board/_jobs.md — opened 2026-11-14
- https://github.com/lever/postings-api (and raw README.md) — opened 2026-11-14
- https://registry.npmjs.org/@absolutejs/job-sources/-/job-sources-0.2.0.tgz — opened 2026-11-14 (README + dist/index.js)
- https://files.pythonhosted.org/packages/47/6f/202e16ed6c801354ab707ebc8a535ba7e233a5a7ad1a3e123017806c1cd9/edgartools-6.1.0-py3-none-any.whl — opened 2026-11-14 (`edgar/offerings/exempt/formd.py`)
- https://registry.npmjs.org/-/v1/search?text=ashby+job+board+api (and `lever+postings+api`, `greenhouse+job+board`) — opened 2026-11-14, package discovery only
- https://developers.ashbyhq.com/docs/public-job-posting-api — search summary, not opened (host blocked); `includeCompensation` param
- https://www.law.cornell.edu/cfr/text/17/230.503, https://www.govinfo.gov/content/pkg/CFR-2023-title17-vol3/pdf/CFR-2023-title17-vol3-sec230-503.pdf — search summary, not opened (15 calendar days after first sale, next business day)
