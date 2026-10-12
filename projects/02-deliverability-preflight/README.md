# 02 · Deliverability preflight — work in progress (v0.0.x)

Check a sending setup — and later each message — *before* it goes out, with
explainable findings instead of a pass/fail light. Every check takes an
injectable DNS resolver, so the whole suite runs offline.

## What exists now

| Check | Module | What it reports |
|---|---|---|
| SPF | [`spf.py`](src/deliverability_preflight/spf.py) | parsed record, DNS-lookup count (limit 10), void-lookup count (limit 2), include/redirect tree, `+all` / `?all` / no `all`, `ptr`, terms after `all`, missing / duplicate records, include loops, `mx` with > 10 records |
| DMARC | [`dmarc.py`](src/deliverability_preflight/dmarc.py) | record parsing, discovery (own domain, then parents), effective policy (`sp` for subdomains), `p=none` / partial `pct` / no `rua`, non-`mailto` report URIs, external report destinations without the `_report._dmarc` authorisation record |
| Alignment | `dmarc.check_alignment` | whether Return-Path and DKIM `d=` align with From under relaxed / strict `aspf` / `adkim`; warns when only SPF aligns |
| DKIM key | [`dkim.py`](src/deliverability_preflight/dkim.py) | the key record at `<selector>._domainkey.<domain>`: missing / duplicate / malformed records, revoked keys (empty `p=`), RSA key length (< 1024 error, < 2048 warning), `h=` without `sha256`, `s=` excluding email, `t=y` testing mode, Ed25519 key length |
| One-click unsubscribe | [`unsubscribe.py`](src/deliverability_preflight/unsubscribe.py) | RFC 8058: exactly one HTTPS URI in `List-Unsubscribe` (angle brackets, `mailto:` allowed alongside), `List-Unsubscribe-Post: List-Unsubscribe=One-Click`, and a DKIM-Signature whose `h=` covers both headers (warning if the draft is not signed yet) |

| Complaint rate | [`complaints.py`](src/deliverability_preflight/complaints.py) | spam-complaint rate per provider vs. a 0.1% target and 0.3% hard limit → `ok` / `watch` / `stop`, complaints left before the limit, small-sample handling (1 complaint on 50 sends does not pause a domain; 5 on 200 does) |
| All of the above | [`preflight.py`](src/deliverability_preflight/preflight.py) | `preflight()` runs SPF on the Return-Path domain, DMARC on From, every DKIM selector, alignment (using the record's `adkim`/`aspf`, and only DKIM keys that check out), optional draft headers and complaint stats → one report with verdict `pass` / `review` / `block` |

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

### DMARC rules applied (RFC 7489)

- The record must start with `v=DMARC1`; `p` must be `none`, `quarantine` or
  `reject`; duplicate tags, bad `adkim`/`aspf`/`pct` values make it unusable.
- More than one DMARC record at the same name: receivers ignore them all (§6.6.3).
- A domain without its own record uses its parent's, with `sp` (if set) as
  the effective policy.
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

## Not yet / not verified

- No live DNS adapter ships yet; the `Resolver` protocol is one method
  (`lookup(name, rtype) -> list[str]`). Nothing here has been run against
  real DNS.
- Organizational domains use a naive "last two labels" rule; there is no
  Public Suffix List bundled. Pass `org_domain=` with a PSL-aware function for
  suffixes such as `co.uk`. Discovery walks every parent instead of using the PSL.
- DMARCbis (reported published in 2026 as the RFC 7489 successor) has not been
  checked against its text yet; the parser keeps unknown tags (e.g. `np`, `psd`,
  `t`) and parses `np` when present, but no DMARCbis-specific rule is applied.
- The DKIM check reads the published key only: it does not verify a
  signature or check that the private key matches.
- The unsubscribe check reads headers only: it does not verify the DKIM
  signature or call the unsubscribe URL.
- The 0.1% / 0.3% complaint thresholds follow Gmail's and Yahoo's bulk-sender
  guidance as widely reported; the provider pages could not be opened from
  the run environment to re-verify them, so both are parameters.
- Content lint, FastAPI and the n8n node are on the [roadmap](../../ROADMAP.md).

## Run

```bash
pip install -e ".[dev]" && pytest -q
```
