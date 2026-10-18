# 02 · Deliverability preflight — work in progress (v0.0.x)

Check a sending setup — and later each message — *before* it goes out, with
explainable findings instead of a pass/fail light. Every check takes an
injectable DNS resolver, so the whole suite runs offline.

## What exists now

| Check | Module | What it reports |
|---|---|---|
| SPF | [`spf.py`](src/deliverability_preflight/spf.py) | parsed record, DNS-lookup count (limit 10), void-lookup count (limit 2), include/redirect tree, `+all` / `?all` / no `all`, `ptr`, terms after `all`, missing / duplicate records, include loops, `mx` with > 10 records |
| DMARC | [`dmarc.py`](src/deliverability_preflight/dmarc.py) | record parsing, DMARCbis DNS Tree Walk discovery (own domain, then parents up to the TLD, ≤ 8 queries, `psd=y`/`psd=n`), Organizational Domain by tree walk (`TreeWalkOrgDomain`), public-suffix-only policy, effective policy (`sp` for subdomains), `p=none` / partial `pct` / no `rua`, non-`mailto` report URIs, external report destinations without the `_report._dmarc` authorisation record |
| Alignment | `dmarc.check_alignment` | whether Return-Path and DKIM `d=` align with From under relaxed / strict `aspf` / `adkim`; warns when only SPF aligns |
| Organizational domain | [`psl.py`](src/deliverability_preflight/psl.py) | `PublicSuffixList.from_file(path)` reads the official `public_suffix_list.dat` (wildcards, `!` exceptions, implicit `*`, IDN rules matched as A-labels, optional ICANN-only); `.org_domain` plugs into every `org_domain=` so `a.co.uk` and `b.co.uk` are different organizations for alignment and content lint |
| DKIM key | [`dkim.py`](src/deliverability_preflight/dkim.py) | the key record at `<selector>._domainkey.<domain>`: missing / duplicate / malformed records, revoked keys (empty `p=`), RSA key length (< 1024 error, < 2048 warning), `h=` without `sha256`, `s=` excluding email, `t=y` testing mode, Ed25519 key length |
| DKIM-Signature | [`dkim_signature.py`](src/deliverability_preflight/dkim_signature.py) | each `DKIM-Signature` header of a test message: missing required tags (`v a b bh d h s`), `v` ≠ 1, `rsa-sha1` or an unknown `a=`, From not in `h=`, `i=` outside `d=`, bad `c=`, `x=` not after `t=` or already past → error; `l=` body-length limit → warning. Its `d=` / `s=` tell `preflight()` which key to check |
| One-click unsubscribe | [`unsubscribe.py`](src/deliverability_preflight/unsubscribe.py) | RFC 8058: exactly one HTTPS URI in `List-Unsubscribe` (angle brackets, `mailto:` allowed alongside), `List-Unsubscribe-Post: List-Unsubscribe=One-Click`, and a DKIM-Signature whose `h=` covers both headers (warning if the draft is not signed yet) |

| Complaint rate | [`complaints.py`](src/deliverability_preflight/complaints.py) | spam-complaint rate per provider vs. a 0.1% target and 0.3% hard limit → `ok` / `watch` / `stop`, complaints left before the limit, small-sample handling (1 complaint on 50 sends does not pause a domain; 5 on 200 does) |
| Content lint | [`content.py`](src/deliverability_preflight/content.py) | rendered draft (`subject`, `text`, `html`): unrendered merge tags (`{{first_name}}`, `%%X%%`, `*\|X\|*`, `{x}`) → error, links to bare IPs → error, link text naming a different domain than its `href` → error; public shorteners, plain `http`, more than 3 links, click-tracking domain outside the From organization, HTML without a text part, image-heavy / CSS-hidden content, empty or shouting subject, `!!!`/`$$`, spam-trigger phrases → warning; third-party link domains → info |
| Sending capacity | [`capacity.py`](src/deliverability_preflight/capacity.py) | `plan_capacity()`: mailboxes (provider, warmup start, complaint status, optional cap) → today's safe campaign volume per mailbox and per domain, a day-by-day ramp (warmup → ramp start + step/day → cap), the date a target volume is reachable, and how many more mailboxes / domains it needs; flags crowded domains, duplicates, caps at a provider's per-user limit, held (`watch`) and paused (`stop`) mailboxes |
| All of the above | [`preflight.py`](src/deliverability_preflight/preflight.py) | `preflight()` runs SPF on the Return-Path domain, DMARC on From, every DKIM selector, alignment (using the record's `adkim`/`aspf`, and only DKIM keys that check out), optional draft headers, draft content and complaint stats → one report with verdict `pass` / `review` / `block` |

Each finding has a stable `code`, a `severity` (`error` / `warning` / `info`),
a human `message` and the `ref` (RFC section) it is based on.

```python
from deliverability_preflight import FakeResolver, check_spf

dns = FakeResolver(
    {
        ("acme.example", "TXT"): ["v=spf1 include:_spf.esp.example mx -all"],
        ("_spf.esp.example", "TXT"): ["v=spf1 ip4:198.51.100.0/24 ~all"],
        ("acme.example", "MX"): ["10 mx1.acme.example"],
    }
)
report = check_spf("acme.example", dns)
report.status, report.lookups  # ('valid', 2)
```

```python
from deliverability_preflight import ComplaintStats, preflight

report = preflight(
    "acme.example",
    dns,
    dkim_selectors=["s1"],
    return_path_domain="bounce.acme.example",
    complaint_stats=[ComplaintStats(provider="gmail", delivered=5000, complaints=2)],
)
report.verdict  # 'pass' | 'review' | 'block'; report.findings lists every reason
```

### SPF rules applied (RFC 7208)

- Terms that query DNS — `include`, `a`, `mx`, `ptr`, `exists` and the
  `redirect` modifier — are counted across the whole include tree; more than
  10 is `permerror` (§4.6.4). `ip4`, `ip6`, `all` and `exp` cost nothing.
- More than 2 *void* lookups (empty answer or NXDOMAIN) is `permerror` (§4.6.4).
- An `mx` that returns more than 10 MX records is `permerror` (§4.6.4).
- Exactly one record starting with `v=spf1` may exist (§4.5); `v=spf10` is not one.
- `include` / `redirect` of a domain with no SPF record is `permerror` (§5.2, §6.1).
- `redirect=` is ignored when the record contains `all` (§6.1); terms after
  `all` are never evaluated (§5.1), so they are flagged and not counted.

Counting is the static worst case (no mechanism matches before `all`). The
checker keeps counting past 10 — up to a safety cap of 50 — so a report shows
how far over budget a policy is. Terms with macros (`%{i}`…) depend on the
connecting IP: they are counted but not resolved.

### DMARC rules applied (RFC 7489, DMARCbis draft -41 discovery)

- The record must start with `v=DMARC1`; duplicate tags and bad
  `adkim`/`aspf`/`pct`/`psd` values make it unusable.
- A missing or invalid `p`, or an invalid `sp`/`np`, does not discard the
  record (DMARCbis §4.10.1): with at least one valid `rua` URI receivers act as
  if it said `p=none` (`DMARC_POLICY_FALLBACK_NONE`, warning); without one
  they apply no DMARC at all (`DMARC_NO_POLICY` / `DMARC_INVALID_POLICY`, error).
- `t=y` (testing, §4.7) steps the applied policy down one level (`reject` →
  `quarantine`, `quarantine` → `none`); `effective_policy` is the stepped-down
  value, `requested_policy` the one in the record (`DMARC_TESTING_MODE`).
- `pct` is historic in DMARCbis (Appendix A.6): `pct<100` is still reported,
  since RFC 7489 receivers apply it while DMARCbis receivers ignore it.
- A subdomain that inherits a parent's record gets `sp` (else `p`); if it does
  not exist in DNS it gets `np` (else `sp`, else `p`). The resolver cannot tell
  NXDOMAIN from "no TXT", so pass `author_domain_exists=False` to apply `np`;
  by default the report adds `DMARC_NP_IF_NXDOMAIN` when `np` would differ.
- More than one DMARC record at the same name: receivers ignore them all (§6.6.3);
  the tree walk skips that name and goes on to the parents.
- Discovery is the DMARCbis DNS Tree Walk (draft-ietf-dmarc-dmarcbis-41 §4.10):
  `_dmarc.<author>`, then each parent up to the TLD; a name with 8+ labels jumps
  to its last 7, so at most 8 queries. A `psd=y` / `psd=n` record stops the walk.
- The record applied is the Author Domain's, else the Organizational Domain's,
  else the public suffix domain's (§4.10.1), with `sp` (if set) as the effective
  policy for subdomains. The Organizational Domain is the hit with the fewest
  labels, or the `psd=n` name, or the name one label below a `psd=y` record
  (§4.10.2). Note this is not "nearest parent": with records at
  `mail.acme.example` and `acme.example`, `news.mail.acme.example` gets
  `acme.example`'s policy. A policy that only comes from a `psd=y` record is a
  warning (`DMARC_PSD_POLICY_ONLY`).
- `TreeWalkOrgDomain(resolver)` is an `org_domain=` function for alignment and
  content checks that uses the same walk (cached per domain; on a DNS failure
  the domain counts as its own Organizational Domain, so relaxed alignment
  degrades to strict rather than to a false pass).
- Reports to a different organizational domain are only sent when that domain
  publishes `<policy-domain>._report._dmarc.<dest>` with `v=DMARC1` (§7.1).
- Relaxed alignment = same organizational domain; strict = identical domain (§3.1).

### DKIM key rules applied (RFC 6376 §3.6, RFC 8301, RFC 8463)

- The key lives at `<selector>._domainkey.<d>` (§3.6.2.1); pass the selectors
  your ESP signs with (`check_dkim_selectors`) — DNS cannot list them.
- `v=`, if present, must be the first tag and equal `DKIM1`; `p=` is
  required, and an empty `p=` means the key is revoked (§3.6.1).
- RSA keys: verifiers must not accept keys under 1024 bits, signers should use
  2048 (RFC 8301 §3.2). The length is read from the DER key
  (SubjectPublicKeyInfo or bare RSAPublicKey) without a crypto library; both
  thresholds are parameters.
- `h=` that does not list `sha256` leaves only `rsa-sha1`, which must not be
  used (RFC 8301 §3.1). `s=` must include `email` or `*`; `t=y` is flagged.
- `k=ed25519` keys must be 32 bytes (RFC 8463); an info finding reminds you to
  keep an RSA selector signing too.

### DKIM-Signature rules applied (RFC 6376 §3.5, §5.4, §6.1.1, §8.2; RFC 8301; RFC 8463)

- Required tags are `v a b bh d h s` (§6.1.1); `v=` must be `1`; tags may
  appear once (§3.2); folding whitespace inside values is dropped.
- `a=` must be `rsa-sha256` or `ed25519-sha256`; `rsa-sha1` must not be used
  (RFC 8301 §3.1).
- `h=` must include `from` (§5.4); `i=` must be in `d=` or a subdomain of it
  (§3.5); `x=` must be later than `t=` (§3.5) and, at check time, not past;
  `c=` is `simple` or `relaxed` for header and body (§3.4).
- `l=` is a warning: content appended after the signed length still verifies (§8.2).
- In `preflight(headers=...)` every signature's own `d=` / `s=` key is
  checked, so `dkim_selectors` can be left empty when headers are given;
  `dkim_domain` defaults to the first signature's `d=`. A signature with
  errors does not make its `d=` count for DMARC alignment.

## Not yet / not verified

- `DnsPythonResolver` (live DNS, `[live]` extra) is tested against a stub
  that raises dnspython's own exceptions, not against real DNS: the run
  environment's DNS returned empty answers even for well-known DMARC records,
  so no live result is claimed. Every check still runs offline by default.
- The default `org_domain` for alignment and content lint is still a naive
  "last two labels" rule: the Public Suffix List is not bundled (it changes
  often and its maintainers ask users to fetch it from
  https://publicsuffix.org/list/public_suffix_list.dat). Download it and pass
  `org_domain=PublicSuffixList.from_file(path).org_domain`, or
  `TreeWalkOrgDomain(resolver)`; real public suffixes rarely publish `psd=y`,
  so a tree walk alone can still merge two companies under one suffix. The
  FastAPI service picks one with `PREFLIGHT_ORG_DOMAIN` (see below).
- DMARCbis: discovery and Organizational Domain follow draft-ietf-dmarc-dmarcbis-41
  (April 2025, read from the IETF DMARC WG repository). Whether the published
  RFC (reported as RFC 9989) changed that text was not checked: rfc-editor.org
  and datatracker.ietf.org were unreachable. The `t`, `pct`, `np` and
  invalid-`p` rules come from the same draft (§4.7, §4.10.1, Appendix A.6).
- DKIM-Signature rules are implemented from knowledge of RFC 6376 / 8301 /
  8463; the RFC pages and the IANA DKIM registry could not be opened from the
  run environment (2026-10-15) to re-check them.
- The DKIM check reads the published key only: it does not verify a
  signature or check that the private key matches.
- The unsubscribe check reads headers only: it does not verify the DKIM
  signature or call the unsubscribe URL.
- The 0.1% / 0.3% complaint thresholds follow Gmail's and Yahoo's bulk-sender
  guidance as widely reported; the provider pages could not be opened from
  the run environment to re-verify them, so both are parameters.
- Content lint rules are heuristics, not published provider rules (providers do
  not publish their filters); the shortener list, spam-phrase list and link
  limit are parameters (`lint_content(shorteners=, spam_phrases=, max_links=)`).
  It lints the draft as given: render merge tags first, it does not follow
  redirects or check URLs against blocklists.
- Capacity-planner defaults (14 warmup days, ramp 5 + 2/day, 30 campaign emails per
  mailbox per day, 3 mailboxes per domain) are common sequencer guidance as reported
  in search summaries of vendor help pages, not a standard; provider ceilings
  (Google Workspace 2,000 messages/user/day, Exchange Online 10,000 recipients/day)
  are from search summaries of the Google and Microsoft pages, which could not be
  opened from the run environment. All are `CapacityPolicy` parameters. Warmup mail
  itself is not counted, and the plan does not read live sequencer or Postmaster data.
- The n8n workflow is structure-tested only; it has not been imported into a live n8n instance.

## Run

```bash
pip install -e ".[dev]" && python -m pytest -q
```

### Live DNS

```bash
pip install -e ".[live]"
```

```python
from deliverability_preflight import DnsPythonResolver, preflight

report = preflight("acme.example", DnsPythonResolver(lifetime=5), dkim_selectors=["s1"])
```

`DnsPythonResolver` queries names as absolute (no search list), joins a TXT
record's 255-byte strings into one (RFC 7208 §3.3, RFC 6376 §3.6.2.2), turns
NXDOMAIN / no answer into `[]` and timeouts / no working nameserver into
`DNSTempError` (a `*_DNS_TEMPERROR` finding, never a silent pass). It also
answers `domain_exists(name)`, so for a subdomain that inherits a record with
`np=` `check_dmarc` asks once whether the name exists and applies `np` or `sp`
itself instead of reporting `DMARC_NP_IF_NXDOMAIN`. `FakeResolver(...,
nxdomain=[...])` does the same in tests.

### HTTP service (demo DNS from a fixture)

```bash
PREFLIGHT_DNS_FIXTURE=examples/dns_fixture.json uvicorn deliverability_preflight.api:app --port 8002
curl -s localhost:8002/v1/preflight -H 'content-type: application/json' -d @examples/request.json
```

`POST /v1/preflight` takes `from_domain`, `dkim_selectors`, optional
`dkim_domain`, `return_path_domain`, `headers` (`[name, value]` pairs),
`content` (`subject`, `text`, `html` of the rendered draft), `tracking_domain`
`complaint_stats` and `author_domain_exists` (overrides the NXDOMAIN probe),
and returns the `PreflightReport` (`verdict` is `pass` / `review` / `block`).
`PREFLIGHT_DNS=live` (with the `[live]` extra; `PREFLIGHT_DNS_LIFETIME`
seconds per query, default 5) answers from real DNS. Otherwise, without
`PREFLIGHT_DNS_FIXTURE` the app has an empty resolver, so everything reports
missing records.

`PREFLIGHT_ORG_DOMAIN` sets how the service finds Organizational Domains for
alignment and content lint, and every report echoes it as `org_domain_strategy`
(so does `GET /healthz`):

| Value | Rule | Notes |
|---|---|---|
| `last-two` (default) | last two labels | wrong under multi-label suffixes (`a.co.uk` and `b.co.uk` align) |
| `psl` | Public Suffix List, file in `PREFLIGHT_PSL_FILE` | download `public_suffix_list.dat` from publicsuffix.org and refresh it regularly; not bundled |
| `tree-walk` | DMARCbis DNS Tree Walk | new cache per request; a DNS failure adds `ORG_DOMAIN_TEMPERROR` (relaxed alignment checked as strict) |

An unknown value, `psl` without a file, or a file with no rules stops the app
at startup instead of falling back to `last-two`.

`POST /v1/capacity` takes `mailboxes` (`address`, `provider` = `google` /
`microsoft` / `smtp`, `warmup_started`, optional `complaint_status` and `daily_cap`),
`target_daily`, optional `on` (default today), `days` (ramp horizon, default 30)
and `policy` (`CapacityPolicy` overrides), and returns the `CapacityPlan`:

```bash
curl -s localhost:8002/v1/capacity -H 'content-type: application/json' -d @examples/capacity_request.json
```

In that example five mailboxes on two domains give 75/day on 2026-10-20 and 125/day
once every mailbox has ramped (one is held at 5/day for complaints), so a 150/day
target needs one more mailbox: `CAPACITY_SHORTFALL`.

`n8n/preflight-before-send.json` posts a campaign to the service and switches on
`verdict` (send / hold for review / block).
