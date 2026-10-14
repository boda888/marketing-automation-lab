"""One call that runs every preflight check and returns a send decision.

``preflight`` checks the domains a campaign will use — SPF on the Return-Path
domain (SPF authenticates MAIL FROM, not From), DMARC on the From domain, the
DKIM selectors the ESP signs with, and DMARC alignment of those identifiers —
plus, when given, a draft's headers (RFC 8058), its rendered content (links,
merge tags, formatting) and recent complaint stats.

The verdict is ``block`` if any finding is an error, ``review`` if any is a
warning, ``pass`` otherwise. Every finding keeps its stable ``code`` so an n8n
IF/Switch node can route on it.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Literal

from pydantic import BaseModel

from .complaints import ComplaintReport, ComplaintStats, check_complaint_rate
from .content import ContentReport, MessageContent, lint_content
from .dkim import DkimReport, check_dkim
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
    alignment: AlignmentResult
    unsubscribe: UnsubscribeReport | None = None
    complaints: list[ComplaintReport] = []
    content: ContentReport | None = None
    findings: list[Finding] = []


def _verdict(findings: list[Finding]) -> Verdict:
    level = worst(findings)
    return "block" if level == "error" else "review" if level == "warning" else "pass"


def preflight(
    from_domain: str,
    resolver: Resolver,
    *,
    dkim_selectors: Iterable[str],
    dkim_domain: str | None = None,
    return_path_domain: str | None = None,
    headers: Iterable[tuple[str, str]] | None = None,
    complaint_stats: Iterable[ComplaintStats] = (),
    content: MessageContent | None = None,
    tracking_domain: str | None = None,
    org_domain: OrgDomainFn = _default_org_domain,
) -> PreflightReport:
    """Run SPF, DMARC, DKIM, alignment, one-click, content and complaint checks.

    ``dkim_domain`` (the signature's ``d=``) and ``return_path_domain``
    default to ``from_domain``. Only selectors whose key checks out count as
    a usable DKIM identifier for alignment.
    """
    from_domain = normalize_name(from_domain)
    rp = normalize_name(return_path_domain or from_domain)
    d = normalize_name(dkim_domain or from_domain)
    selectors = list(dkim_selectors)

    spf = check_spf(rp, resolver)
    dmarc = check_dmarc(from_domain, resolver, org_domain=org_domain)
    dkim = [check_dkim(d, s, resolver) for s in selectors]
    findings: list[Finding] = [*spf.findings, *dmarc.findings]
    for rep in dkim:
        findings.extend(rep.findings)
    if not selectors:
        findings.append(
            Finding(
                code="DKIM_NO_SELECTORS",
                severity="error",
                message="no DKIM selectors given; DMARC then rests on SPF alone",
                ref="RFC 7489 §3.1",
            )
        )

    rec = dmarc.record
    alignment = check_alignment(
        from_domain,
        spf_domain=rp if spf.status == "valid" else None,
        dkim_domains=[d] if any(r.status == "valid" for r in dkim) else [],
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

    return PreflightReport(
        from_domain=from_domain,
        return_path_domain=rp,
        dkim_domain=d,
        verdict=_verdict(findings),
        spf=spf,
        dmarc=dmarc,
        dkim=dkim,
        alignment=alignment,
        unsubscribe=unsubscribe,
        complaints=complaints,
        content=content_report,
        findings=findings,
    )
