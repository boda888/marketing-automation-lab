"""One call that runs every preflight check and returns a send decision.

``preflight`` checks the domains a campaign will use — SPF on the Return-Path
domain (SPF authenticates MAIL FROM, not From), DMARC on the From domain, the
DKIM selectors the ESP signs with, and DMARC alignment of those identifiers —
plus, when given, a draft's headers (RFC 8058 and every DKIM-Signature, whose
``d=`` / ``s=`` key is then checked too), its rendered content (links, merge
tags, formatting) and recent complaint stats.

The verdict is ``block`` if any finding is an error, ``review`` if any is a
warning, ``pass`` otherwise. Every finding keeps its stable ``code`` so an n8n
IF/Switch node can route on it.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from .complaints import ComplaintReport, ComplaintStats, check_complaint_rate
from .content import ContentReport, MessageContent, lint_content
from .dkim import DkimReport, check_dkim
from .dkim_signature import DkimSignatureReport, check_dkim_signature, dkim_signatures
from .dmarc import AlignmentResult, DmarcReport, OrgDomainFn, check_alignment, check_dmarc
from .dmarc import default_org_domain as _default_org_domain
from .dns import Resolver, normalize_name
from .findings import Finding, worst
from .spf import SpfReport, check_spf
from .unsubscribe import UnsubscribeReport, check_one_click

Verdict = Literal["pass", "review", "block"]


class PreflightReport(BaseModel):
    from_domain: str
    return_path_domain: str
    dkim_domain: str
    verdict: Verdict
    spf: SpfReport
    dmarc: DmarcReport
    dkim: list[DkimReport]
    dkim_signatures: list[DkimSignatureReport] = []
    alignment: AlignmentResult
    unsubscribe: UnsubscribeReport | None = None
    complaints: list[ComplaintReport] = []
    content: ContentReport | None = None
    findings: list[Finding] = []
    # set by the API: which org-domain rule alignment and content lint used
    org_domain_strategy: str | None = None


def _verdict(findings: list[Finding]) -> Verdict:
    level = worst(findings)
    return "block" if level == "error" else "review" if level == "warning" else "pass"


def preflight(
    from_domain: str,
    resolver: Resolver,
    *,
    dkim_selectors: Iterable[str] = (),
    dkim_domain: str | None = None,
    return_path_domain: str | None = None,
    headers: Iterable[tuple[str, str]] | None = None,
    complaint_stats: Iterable[ComplaintStats] = (),
    content: MessageContent | None = None,
    tracking_domain: str | None = None,
    org_domain: OrgDomainFn = _default_org_domain,
    author_domain_exists: bool | None = None,
    now: datetime | None = None,
) -> PreflightReport:
    """Run SPF, DMARC, DKIM, alignment, one-click, content and complaint checks.

    ``dkim_selectors`` are checked on ``dkim_domain``. When ``headers`` carry
    DKIM-Signature headers, each one is checked and the key at its own
    ``d=`` / ``s=`` is checked as well, so a test message's headers are enough.
    ``dkim_domain`` defaults to the first signature's ``d=``, else
    ``from_domain``; ``return_path_domain`` defaults to ``from_domain``.
    For alignment, a DKIM domain counts only when its key checks out and, for
    a domain taken from a signature, that signature has no errors.
    ``author_domain_exists`` is passed to ``check_dmarc`` (``np`` vs ``sp``).
    An ``org_domain`` with an ``errors`` list (``TreeWalkOrgDomain``) adds an
    ``ORG_DOMAIN_TEMPERROR`` warning when any of its lookups failed.
    """
    from_domain = normalize_name(from_domain)
    rp = normalize_name(return_path_domain or from_domain)
    headers = list(headers) if headers is not None else None
    selectors = [normalize_name(s) for s in dkim_selectors]

    signatures = [check_dkim_signature(v, now=now) for v in dkim_signatures(headers or [])]
    signed_refs = [s.key_ref for s in signatures if s.key_ref]
    d = normalize_name(dkim_domain or (signed_refs[0][0] if signed_refs else from_domain))

    refs: list[tuple[str, str]] = []
    for ref in [(d, s) for s in selectors] + signed_refs:
        if ref not in refs:
            refs.append(ref)

    spf = check_spf(rp, resolver)
    dmarc = check_dmarc(
        from_domain, resolver, org_domain=org_domain, author_domain_exists=author_domain_exists
    )
    dkim = [check_dkim(dom, sel, resolver) for dom, sel in refs]
    findings: list[Finding] = [*spf.findings, *dmarc.findings]
    for sig in signatures:
        findings.extend(sig.findings)
    for rep in dkim:
        findings.extend(rep.findings)
    if not refs:
        findings.append(
            Finding(
                code="DKIM_NO_SELECTORS",
                severity="error",
                message="no DKIM selectors given and no DKIM-Signature in the headers; "
                "DMARC then rests on SPF alone",
                ref="RFC 7489 §3.1",
            )
        )

    good_keys = {(r.domain, r.selector) for r in dkim if r.status == "valid"}
    explicit = {(d, s) for s in selectors}
    usable = {ref for ref in explicit if ref in good_keys}
    usable |= {s.key_ref for s in signatures if s.status == "ok" and s.key_ref in good_keys}
    rec = dmarc.record
    alignment = check_alignment(
        from_domain,
        spf_domain=rp if spf.status == "valid" else None,
        dkim_domains=sorted({dom for dom, _ in usable}),
        adkim=rec.adkim if rec else "r",
        aspf=rec.aspf if rec else "r",
        org_domain=org_domain,
    )
    findings.extend(alignment.findings)

    unsubscribe = None
    if headers is not None:
        unsubscribe = check_one_click(headers)
        findings.extend(unsubscribe.findings)

    content_report = None
    if content is not None:
        content_report = lint_content(
            content,
            from_domain=from_domain,
            tracking_domain=tracking_domain,
            org_domain=org_domain,
        )
        findings.extend(content_report.findings)

    complaints = [check_complaint_rate(s) for s in complaint_stats]
    for rep in complaints:
        findings.extend(rep.findings)

    # TreeWalkOrgDomain lists names whose walk hit a DNS failure; they were
    # treated as their own org domain, so relaxed alignment ran as strict
    walk_errors = sorted(set(getattr(org_domain, "errors", None) or []))
    if walk_errors:
        findings.append(
            Finding(
                code="ORG_DOMAIN_TEMPERROR",
                severity="warning",
                message="DNS failure while finding the organizational domain of "
                f"{', '.join(walk_errors)}; relaxed alignment was checked as strict",
                ref="DMARCbis §4.10.2",
            )
        )

    return PreflightReport(
        from_domain=from_domain,
        return_path_domain=rp,
        dkim_domain=d,
        verdict=_verdict(findings),
        spf=spf,
        dmarc=dmarc,
        dkim=dkim,
        dkim_signatures=signatures,
        alignment=alignment,
        unsubscribe=unsubscribe,
        complaints=complaints,
        content=content_report,
        findings=findings,
    )
