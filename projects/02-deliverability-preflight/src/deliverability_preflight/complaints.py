"""Spam-complaint-rate guardrail.

Mailbox providers publish a user-reported spam rate per sending domain (e.g.
Gmail Postmaster Tools). Their bulk-sender guidance, as widely reported, asks
senders to stay under 0.1% and never reach 0.3%. Both numbers are parameters
here because they come from provider policy, not from a standard, and change.

``check_complaint_rate`` turns raw counts into an explainable decision:
``ok`` -> keep sending, ``watch`` -> over target, slow down and fix targeting,
``stop`` -> at/over the hard limit, pause the campaign. Small samples are
flagged so a single complaint on 50 messages does not pause a domain, while
a sample that is already over the limit in absolute terms still stops it.
"""

from __future__ import annotations

import math
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from .findings import Finding

TARGET_RATE = 0.001
HARD_LIMIT_RATE = 0.003
MIN_SAMPLE = 1000
_REF = "mailbox-provider bulk-sender guidance (0.1% target / 0.3% limit)"

ComplaintStatus = Literal["ok", "watch", "stop"]


class ComplaintStats(BaseModel):
    """Counts for one sending domain over one window (e.g. a day, per provider)."""

    delivered: int = Field(ge=0)
    complaints: int = Field(ge=0)
    provider: str = "all"

    @model_validator(mode="after")
    def _complaints_within_delivered(self) -> ComplaintStats:
        if self.complaints > self.delivered:
            raise ValueError("complaints cannot exceed delivered")
        return self


class ComplaintReport(BaseModel):
    provider: str
    delivered: int
    complaints: int
    rate: float | None
    status: ComplaintStatus
    sufficient_sample: bool
    complaints_until_limit: int | None
    findings: list[Finding] = []


def check_complaint_rate(
    stats: ComplaintStats,
    *,
    target: float = TARGET_RATE,
    hard_limit: float = HARD_LIMIT_RATE,
    min_sample: int = MIN_SAMPLE,
) -> ComplaintReport:
    """Compare a complaint rate with the target and the hard limit."""
    if not 0 < target < hard_limit < 1:
        raise ValueError("need 0 < target < hard_limit < 1")
    delivered, complaints = stats.delivered, stats.complaints
    rate = complaints / delivered if delivered else None
    sufficient = delivered >= min_sample
    # complaints still allowed in this window before the rate reaches the hard limit
    until = math.ceil(hard_limit * delivered) - complaints - 1 if delivered else None
    if until is not None:
        until = max(until, 0)
    rep = ComplaintReport(
        provider=stats.provider,
        delivered=delivered,
        complaints=complaints,
        rate=rate,
        status="ok",
        sufficient_sample=sufficient,
        complaints_until_limit=until,
    )

    def add(code: str, severity: str, message: str) -> None:
        rep.findings.append(Finding(code=code, severity=severity, message=message, ref=_REF))

    label = f"{stats.provider}: {complaints}/{delivered}"
    if rate is None:
        add("COMPLAINT_NO_DATA", "info", f"{stats.provider}: nothing delivered in this window")
        return rep
    pct = f"{rate:.3%}"
    # the absolute check: even the whole minimum sample could not dilute these complaints
    over_regardless = complaints >= hard_limit * max(delivered, min_sample)
    if rate >= hard_limit and (sufficient or over_regardless):
        rep.status = "stop"
        add(
            "COMPLAINT_RATE_OVER_LIMIT",
            "error",
            f"{label} = {pct} >= {hard_limit:.1%} limit: pause sending from this domain",
        )
    elif rate >= target and sufficient:
        rep.status = "watch"
        add(
            "COMPLAINT_RATE_OVER_TARGET",
            "warning",
            f"{label} = {pct} >= {target:.1%} target: cut volume and tighten targeting",
        )
    elif rate >= target:
        rep.status = "watch"
        add(
            "COMPLAINT_RATE_SMALL_SAMPLE",
            "warning",
            f"{label} = {pct} on fewer than {min_sample} delivered: too few to judge, "
            "keep volume flat until the sample grows",
        )
    elif not sufficient:
        add(
            "COMPLAINT_SMALL_SAMPLE",
            "info",
            f"{label} delivered is below {min_sample}; the rate is not yet reliable",
        )
    return rep
