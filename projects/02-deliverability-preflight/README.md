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
- RFC 8058 one-click unsubscribe, content lint, complaint-rate
  guardrail, FastAPI and n8n node are on the [roadmap](../../ROADMAP.md).

## Run

```bash
pip install -e ".[dev]" && pytest -q
```
