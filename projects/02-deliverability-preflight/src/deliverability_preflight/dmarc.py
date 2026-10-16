"""DMARC record parsing, policy discovery and identifier alignment (RFC 7489).

- ``parse_dmarc`` turns a ``v=DMARC1; p=...`` TXT string into a typed record.
- ``check_dmarc`` finds the record that governs a From domain and explains it:
  missing / duplicate / invalid records, monitoring-only policies, partial
  ``pct``, missing ``rua``, and external report destinations that have not
  authorised the policy domain (§7.1).
- ``check_alignment`` tells whether the SPF (Return-Path / MAIL FROM) and DKIM
  (``d=``) identifiers align with the From domain under ``aspf`` / ``adkim``.

Policy discovery follows the DMARCbis DNS Tree Walk
(draft-ietf-dmarc-dmarcbis-41 §4.10): the Author Domain first, then its
parents up to and including the TLD, at most eight queries, stopping at a
``psd=y`` / ``psd=n`` record. ``tree_walk_org_domain`` and ``TreeWalkOrgDomain``
apply §4.10.2 to find an Organizational Domain from DNS alone.

``default_org_domain`` (used for alignment unless another function is passed)
keeps the last two labels, which is wrong for suffixes like ``co.uk``. Pass
``TreeWalkOrgDomain(resolver)`` or a PSL-aware function where that matters.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Literal

from pydantic import BaseModel

from .dns import DNSTempError, Resolver, normalize_name
from .findings import Finding

Policy = Literal["none", "quarantine", "reject"]
Mode = Literal["r", "s"]
Psd = Literal["y", "n", "u"]
OrgDomainFn = Callable[[str], str]

_POLICIES = ("none", "quarantine", "reject")


class DmarcSyntaxError(ValueError):
    """The TXT string is not a usable DMARC record."""


class DmarcRecord(BaseModel):
    raw: str
    p: Policy | None = None
    sp: Policy | None = None
    np: Policy | None = None
    adkim: Mode = "r"
    aspf: Mode = "r"
    pct: int = 100
    psd: Psd = "u"
    rua: list[str] = []
    ruf: list[str] = []
    tags: dict[str, str] = {}


def is_dmarc_record(txt: str) -> bool:
    first = txt.split(";", 1)[0].replace(" ", "").replace("\t", "")
    return first == "v=DMARC1"


def _uris(value: str) -> list[str]:
    # each URI may carry a "!size" limit suffix (§6.4); keep only the URI
    return [u.strip().split("!", 1)[0] for u in value.split(",") if u.strip()]


def parse_dmarc(raw: str) -> DmarcRecord:
    """Parse a DMARC TXT string. Raises ``DmarcSyntaxError`` if unusable."""
    tags: dict[str, str] = {}
    order: list[str] = []
    for part in raw.split(";"):
        part = part.strip()
        if not part:
            continue
        key, sep, value = part.partition("=")
        key, value = key.strip().lower(), value.strip()
        if not sep or not key:
            raise DmarcSyntaxError(f"malformed tag {part!r}")
        if key in tags:
            raise DmarcSyntaxError(f"tag {key!r} appears more than once")
        tags[key] = value
        order.append(key)
    if not order or order[0] != "v" or tags["v"] != "DMARC1":
        raise DmarcSyntaxError("record must start with 'v=DMARC1'")

    rec = DmarcRecord(raw=raw, tags=tags)
    for key in ("p", "sp", "np"):
        if key in tags:
            if tags[key].lower() not in _POLICIES:
                raise DmarcSyntaxError(f"{key}={tags[key]!r} is not none/quarantine/reject")
            setattr(rec, key, tags[key].lower())
    for key in ("adkim", "aspf"):
        if key in tags:
            if tags[key].lower() not in ("r", "s"):
                raise DmarcSyntaxError(f"{key}={tags[key]!r} must be 'r' or 's'")
            setattr(rec, key, tags[key].lower())
    if "pct" in tags:
        if not tags["pct"].isdigit() or not 0 <= int(tags["pct"]) <= 100:
            raise DmarcSyntaxError(f"pct={tags['pct']!r} must be an integer 0-100")
        rec.pct = int(tags["pct"])
    if "psd" in tags:
        if tags["psd"].lower() not in ("y", "n", "u"):
            raise DmarcSyntaxError(f"psd={tags['psd']!r} must be 'y', 'n' or 'u'")
        rec.psd = tags["psd"].lower()
    rec.rua = _uris(tags.get("rua", ""))
    rec.ruf = _uris(tags.get("ruf", ""))
    return rec


def default_org_domain(domain: str) -> str:
    """Last two labels. Not PSL-aware — see module docstring."""
    labels = normalize_name(domain).split(".")
    return ".".join(labels[-2:])


DmarcStatus = Literal["valid", "none", "invalid", "temperror"]


class DmarcReport(BaseModel):
    domain: str
    status: DmarcStatus
    policy_domain: str | None = None
    organizational_domain: str | None = None
    record: DmarcRecord | None = None
    effective_policy: Policy | None = None
    findings: list[Finding] = []


def _f(code: str, severity: str, message: str, ref: str = "RFC 7489") -> Finding:
    return Finding(code=code, severity=severity, message=message, ref=ref)


MAX_WALK_QUERIES = 8  # DMARCbis §4.10: Author Domains of 8+ labels jump to 7


def tree_walk_targets(domain: str) -> list[str]:
    """Names a DMARCbis DNS Tree Walk queries, starting at ``domain`` itself.

    Below eight labels each step drops the left-most label; a name with eight
    or more labels jumps straight to its last seven, so a walk never takes
    more than eight queries. The TLD is the last target.
    """
    labels = normalize_name(domain).split(".")
    targets = [".".join(labels)]
    rest = labels[-7:] if len(labels) >= MAX_WALK_QUERIES else labels[1:]
    while rest:
        targets.append(".".join(rest))
        rest = rest[1:]
    return targets


class WalkHit(BaseModel):
    domain: str
    record: DmarcRecord


class _Walk(BaseModel):
    hits: list[WalkHit] = []
    multiple: list[str] = []  # targets with more than one DMARC record (discarded)
    invalid: dict[str, str] = {}  # target -> syntax error; the walk stops there


def _walk(targets: list[str], resolver: Resolver) -> _Walk:
    """Query each target; raises ``DNSTempError`` (the answer is unknown)."""
    walk = _Walk()
    for target in targets:
        records = [t for t in resolver.lookup(f"_dmarc.{target}", "TXT") if is_dmarc_record(t)]
        if len(records) > 1:
            walk.multiple.append(target)
            continue
        if not records:
            continue
        try:
            rec = parse_dmarc(records[0])
        except DmarcSyntaxError as exc:
            walk.invalid[target] = str(exc)
            break
        walk.hits.append(WalkHit(domain=target, record=rec))
        if rec.psd in ("y", "n"):
            break
    return walk


def select_org_domain(start: str, hits: list[WalkHit]) -> str:
    """Organizational Domain from a walk's hits, longest name first (§4.10.2).

    ``psd=n`` marks the Organizational Domain itself; ``psd=y`` on a record
    other than the start marks a public suffix, so the name one label below
    it is the Organizational Domain; otherwise the hit with the fewest labels
    wins. No hits: ``start`` is its own Organizational Domain.
    """
    start = normalize_name(start)
    labels = start.split(".")
    for hit in hits:
        if hit.record.psd == "n":
            return hit.domain
        if hit.record.psd == "y" and hit.domain != start:
            return ".".join(labels[-(hit.domain.count(".") + 2) :])
    return hits[-1].domain if hits else start


def tree_walk_org_domain(domain: str, resolver: Resolver) -> str:
    """Organizational Domain of ``domain`` by DNS Tree Walk.

    Raises ``DNSTempError`` when a query fails: the answer is unknown.
    """
    domain = normalize_name(domain)
    return select_org_domain(domain, _walk(tree_walk_targets(domain), resolver).hits)


class TreeWalkOrgDomain:
    """``OrgDomainFn`` backed by DNS Tree Walks, cached per domain.

    On a DNS failure the domain is treated as its own Organizational Domain
    (so relaxed alignment degrades to strict, never to a false pass) and the
    domain is listed in ``errors``.
    """

    def __init__(self, resolver: Resolver) -> None:
        self._resolver = resolver
        self._cache: dict[str, str] = {}
        self.errors: list[str] = []

    def __call__(self, domain: str) -> str:
        domain = normalize_name(domain)
        if domain not in self._cache:
            try:
                self._cache[domain] = tree_walk_org_domain(domain, self._resolver)
            except DNSTempError:
                self.errors.append(domain)
                return domain
        return self._cache[domain]


def check_dmarc(
    domain: str,
    resolver: Resolver,
    *,
    org_domain: OrgDomainFn = default_org_domain,
) -> DmarcReport:
    """Find and explain the DMARC policy that governs mail From ``domain``.

    Discovery is the DMARCbis DNS Tree Walk: ``_dmarc.<domain>`` first; when
    it has no usable record, the parents up to the TLD. The record applied is
    the Author Domain's, else the Organizational Domain's, else the public
    suffix domain's (§4.10.1). A name with several DMARC records is reported
    and skipped, as receivers discard them. ``org_domain`` is used only to
    decide whether report destinations are external.
    """
    domain = normalize_name(domain)
    report = DmarcReport(domain=domain, status="none")
    findings = report.findings

    try:
        walk = _walk(tree_walk_targets(domain)[:1], resolver)
        if not walk.hits and not walk.invalid:
            parents = _walk(tree_walk_targets(domain)[1:], resolver)
            walk.hits, walk.invalid = parents.hits, parents.invalid
            walk.multiple += parents.multiple
    except DNSTempError as exc:
        findings.append(_f("DMARC_DNS_TEMPERROR", "error", str(exc)))
        report.status = "temperror"
        return report

    for target in walk.multiple:
        findings.append(
            _f(
                "DMARC_MULTIPLE_RECORDS",
                "error",
                f"_dmarc.{target} has more than one DMARC record; receivers discard them all",
                "RFC 7489 §6.6.3; DMARCbis §4.10",
            )
        )
    if walk.invalid:
        target, error = next(iter(walk.invalid.items()))
        report.policy_domain = target
        findings.append(_f("DMARC_SYNTAX", "error", f"_dmarc.{target}: {error}", "RFC 7489 §6.3"))
        report.status = "invalid"
        return report
    if not walk.hits:
        if walk.multiple:
            report.status = "invalid"
            return report
        findings.append(
            _f(
                "DMARC_MISSING",
                "error",
                f"no DMARC record for {domain} or its parents; "
                "major mailbox providers require one from bulk senders",
                "RFC 7489 §6.6.3",
            )
        )
        return report

    org = select_org_domain(domain, walk.hits)
    by_domain = {h.domain: h for h in walk.hits}
    policy = by_domain.get(domain) or by_domain.get(org) or walk.hits[-1]
    report.organizational_domain = org
    report.policy_domain = policy.domain
    report.record = policy.record

    rec = report.record
    inherited = report.policy_domain != domain
    if rec.p is None:
        findings.append(
            _f(
                "DMARC_NO_POLICY",
                "error",
                "record has no p= tag; receivers may ignore it or treat it as p=none",
                "RFC 7489 §6.3, §6.6.3",
            )
        )
        report.status = "invalid"
        return report

    report.status = "valid"
    report.effective_policy = (rec.sp or rec.p) if inherited else rec.p
    if inherited:
        findings.append(
            _f(
                "DMARC_INHERITED",
                "info",
                f"{domain} uses the record of {report.policy_domain} "
                f"(effective policy {report.effective_policy})",
                "RFC 7489 §6.6.3; DMARCbis §4.10.1",
            )
        )
        if rec.psd == "y":
            findings.append(
                _f(
                    "DMARC_PSD_POLICY_ONLY",
                    "warning",
                    f"only the public suffix operator's record at {report.policy_domain} "
                    f"covers {domain}; publish a record at {org} to control policy and reports",
                    "DMARCbis §4.10.1",
                )
            )
    if report.effective_policy == "none":
        findings.append(
            _f(
                "DMARC_POLICY_NONE",
                "info",
                "p=none is monitoring only: spoofed mail is not quarantined or rejected",
                "RFC 7489 §6.3",
            )
        )
    elif rec.pct < 100:
        findings.append(
            _f(
                "DMARC_PCT_PARTIAL",
                "warning",
                f"pct={rec.pct}: the policy applies to only part of failing mail",
                "RFC 7489 §6.3",
            )
        )
    if not rec.rua:
        findings.append(
            _f(
                "DMARC_NO_RUA",
                "warning",
                "no rua= aggregate report address: you cannot see who sends as this domain",
                "RFC 7489 §7.2",
            )
        )
    for uri in rec.rua + rec.ruf:
        if not uri.lower().startswith("mailto:"):
            findings.append(
                _f(
                    "DMARC_REPORT_URI_NOT_MAILTO",
                    "warning",
                    f"{uri}: receivers are only required to support mailto: report URIs",
                    "RFC 7489 §6.2",
                )
            )
            continue
        _check_external_destination(report, uri, resolver, org_domain)
    return report


def _check_external_destination(
    report: DmarcReport, uri: str, resolver: Resolver, org_domain: OrgDomainFn
) -> None:
    dest = normalize_name(uri.split(":", 1)[1].rsplit("@", 1)[-1])
    policy_domain = report.policy_domain or report.domain
    if org_domain(dest) == org_domain(policy_domain):
        return
    name = f"{policy_domain}._report._dmarc.{dest}"
    try:
        txts = resolver.lookup(name, "TXT")
    except DNSTempError as exc:
        report.findings.append(_f("DMARC_DNS_TEMPERROR", "warning", str(exc)))
        return
    if not any(is_dmarc_record(t) for t in txts):
        report.findings.append(
            _f(
                "DMARC_EXTERNAL_REPORTS_UNAUTHORIZED",
                "warning",
                f"reports to {dest} need a 'v=DMARC1' TXT at {name}; receivers will skip them",
                "RFC 7489 §7.1",
            )
        )


def aligned(
    identifier: str, from_domain: str, mode: Mode, org_domain: OrgDomainFn = default_org_domain
) -> bool:
    a, b = normalize_name(identifier), normalize_name(from_domain)
    if mode == "s":
        return a == b
    return org_domain(a) == org_domain(b)


class AlignmentResult(BaseModel):
    from_domain: str
    spf_aligned: bool
    dkim_aligned: bool
    findings: list[Finding] = []

    @property
    def dmarc_pass_possible(self) -> bool:
        return self.spf_aligned or self.dkim_aligned


def check_alignment(
    from_domain: str,
    *,
    spf_domain: str | None,
    dkim_domains: list[str],
    adkim: Mode = "r",
    aspf: Mode = "r",
    org_domain: OrgDomainFn = default_org_domain,
) -> AlignmentResult:
    """Would these identifiers satisfy DMARC for ``from_domain``?

    ``spf_domain`` is the RFC5321.MailFrom (Return-Path) domain and
    ``dkim_domains`` the ``d=`` values of signatures expected to verify. This
    checks alignment only; it does not verify SPF or DKIM themselves.
    """
    spf_ok = bool(spf_domain) and aligned(spf_domain, from_domain, aspf, org_domain)
    dkim_ok = any(aligned(d, from_domain, adkim, org_domain) for d in dkim_domains)
    res = AlignmentResult(from_domain=from_domain, spf_aligned=spf_ok, dkim_aligned=dkim_ok)
    if not (spf_ok or dkim_ok):
        res.findings.append(
            _f(
                "DMARC_NOT_ALIGNED",
                "error",
                f"neither Return-Path ({spf_domain}) nor DKIM d= ({', '.join(dkim_domains) or '-'})"
                f" aligns with From {from_domain}",
                "RFC 7489 §3.1",
            )
        )
    elif not dkim_ok:
        res.findings.append(
            _f(
                "DMARC_SPF_ONLY_ALIGNMENT",
                "warning",
                "only SPF aligns; forwarding breaks SPF, so sign with an aligned DKIM d= too",
                "RFC 7489 §3.1",
            )
        )
    return res
