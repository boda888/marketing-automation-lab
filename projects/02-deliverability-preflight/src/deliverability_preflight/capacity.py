"""Sending-capacity and warmup planner.

Cold-outreach volume is limited far below what a mailbox provider would accept:
the binding limit is the per-mailbox campaign cap a sequencer is set to, the
warmup period before a new mailbox sends campaigns, and the ramp from a few
campaign emails a day up to that cap. This module turns a list of mailboxes
plus those policy numbers into today's safe volume, a day-by-day ramp, the
date a target volume is reachable, and how many more mailboxes / domains the
target needs.

Every number is a parameter in ``CapacityPolicy``. Defaults follow common
sequencer guidance as reported in vendor help pages (about 30 campaign emails
per mailbox per day once warmed, ~14 days of warmup, a slow ramp adding 2 per
day) — none of it is a standard, and provider pages could not be opened from
the run environment to re-verify it. ``provider_daily_limits`` are the
providers' own per-user ceilings (Google Workspace 2,000 messages/day,
Exchange Online 10,000 recipients/day, as reported in search results): a
campaign cap at or above them is a configuration error, not a plan.

Complaint status from ``check_complaint_rate`` feeds in per mailbox: ``watch``
holds the mailbox at the ramp start volume, ``stop`` takes it to zero.
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import date, timedelta
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from .complaints import ComplaintStatus
from .findings import Finding

Provider = Literal["google", "microsoft", "smtp"]
MailboxPhase = Literal["not_started", "warmup", "ramp", "steady", "held", "paused"]

_REF = "sequencer warmup/ramp guidance + provider per-user sending limits (parameters)"


class CapacityPolicy(BaseModel):
    warmup_days: int = Field(default=14, ge=0)
    ramp_start: int = Field(default=5, ge=1)
    ramp_step: int = Field(default=2, ge=1)
    campaign_cap: int = Field(default=30, ge=1)
    max_mailboxes_per_domain: int = Field(default=3, ge=1)
    provider_daily_limits: dict[str, int] = {"google": 2000, "microsoft": 10000}

    @model_validator(mode="after")
    def _start_within_cap(self) -> CapacityPolicy:
        if self.ramp_start > self.campaign_cap:
            raise ValueError("ramp_start cannot exceed campaign_cap")
        return self


class Mailbox(BaseModel):
    address: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    provider: Provider = "google"
    warmup_started: date
    complaint_status: ComplaintStatus = "ok"
    daily_cap: int | None = Field(default=None, ge=0)

    @property
    def domain(self) -> str:
        return self.address.rsplit("@", 1)[1].lower()


class MailboxCapacity(BaseModel):
    address: str
    domain: str
    phase: MailboxPhase
    capacity: int
    cap: int
    steady_from: date


class DayCapacity(BaseModel):
    day: date
    capacity: int


class CapacityPlan(BaseModel):
    on: date
    target_daily: int
    capacity_today: int
    steady_state_capacity: int
    target_reached_on: date | None
    mailboxes: list[MailboxCapacity]
    by_domain: dict[str, int]
    schedule: list[DayCapacity]
    mailboxes_needed: int
    additional_mailboxes: int
    additional_domains: int
    findings: list[Finding] = []


def _cap(mb: Mailbox, policy: CapacityPolicy) -> int:
    return policy.campaign_cap if mb.daily_cap is None else mb.daily_cap


def mailbox_capacity(
    mb: Mailbox, on: date, policy: CapacityPolicy | None = None
) -> MailboxCapacity:
    """Campaign emails this mailbox may send on ``on`` (warmup mail not counted)."""
    policy = policy or CapacityPolicy()
    cap = _cap(mb, policy)
    campaign_day = (on - mb.warmup_started).days - policy.warmup_days  # 0 = first campaign day
    ramp_days = max(math.ceil((cap - policy.ramp_start) / policy.ramp_step), 0)
    steady_from = mb.warmup_started + timedelta(days=policy.warmup_days + ramp_days)
    if on < mb.warmup_started:
        phase, capacity = "not_started", 0
    elif campaign_day < 0:
        phase, capacity = "warmup", 0
    else:
        capacity = min(policy.ramp_start + policy.ramp_step * campaign_day, cap)
        phase = "steady" if capacity >= cap else "ramp"
    if capacity and mb.complaint_status == "watch":
        phase, capacity = "held", min(capacity, policy.ramp_start)
    if mb.complaint_status == "stop":
        phase, capacity = "paused", 0
    return MailboxCapacity(
        address=mb.address,
        domain=mb.domain,
        phase=phase,
        capacity=capacity,
        cap=cap,
        steady_from=steady_from,
    )


def plan_capacity(
    mailboxes: list[Mailbox],
    on: date,
    target_daily: int,
    *,
    days: int = 30,
    policy: CapacityPolicy | None = None,
) -> CapacityPlan:
    """Today's safe volume, a ``days``-long ramp and what the target still needs."""
    if target_daily < 0 or days < 1:
        raise ValueError("need target_daily >= 0 and days >= 1")
    policy = policy or CapacityPolicy()
    findings: list[Finding] = []

    def add(code: str, severity: str, message: str) -> None:
        findings.append(Finding(code=code, severity=severity, message=message, ref=_REF))

    seen: set[str] = set()
    unique: list[Mailbox] = []
    for mb in mailboxes:
        key = mb.address.lower()
        if key in seen:
            add("CAPACITY_DUPLICATE_MAILBOX", "warning", f"{mb.address} listed twice; counted once")
            continue
        seen.add(key)
        unique.append(mb)

    today = [mailbox_capacity(mb, on, policy) for mb in unique]
    by_domain: dict[str, int] = defaultdict(int)
    per_domain_count: dict[str, int] = defaultdict(int)
    for mb, mc in zip(unique, today, strict=True):
        by_domain[mc.domain] += mc.capacity
        per_domain_count[mc.domain] += 1
        limit = policy.provider_daily_limits.get(mb.provider)
        if limit is not None and mc.cap >= limit:
            add(
                "CAPACITY_CAP_AT_PROVIDER_LIMIT",
                "error",
                f"{mb.address}: daily cap {mc.cap} reaches the {mb.provider} per-user limit "
                f"({limit}/day); hitting it blocks sending for up to a day",
            )
        if mc.phase == "paused":
            add(
                "CAPACITY_MAILBOX_PAUSED",
                "error",
                f"{mb.address}: complaint rate over the hard limit, no campaign volume",
            )
        elif mc.phase == "held":
            add(
                "CAPACITY_MAILBOX_HELD",
                "warning",
                f"{mb.address}: complaint rate over target, held at {policy.ramp_start}/day",
            )
        elif mc.phase in ("warmup", "not_started"):
            add(
                "CAPACITY_MAILBOX_WARMING",
                "info",
                f"{mb.address}: warmup only until "
                f"{mb.warmup_started + timedelta(days=policy.warmup_days)}",
            )
    for domain, count in sorted(per_domain_count.items()):
        if count > policy.max_mailboxes_per_domain:
            add(
                "CAPACITY_DOMAIN_CROWDED",
                "warning",
                f"{domain}: {count} mailboxes > {policy.max_mailboxes_per_domain} per domain; "
                "one domain's reputation carries all of them",
            )

    schedule = [
        DayCapacity(
            day=on + timedelta(days=i),
            capacity=sum(
                mailbox_capacity(mb, on + timedelta(days=i), policy).capacity for mb in unique
            ),
        )
        for i in range(days)
    ]
    reached = next((d.day for d in schedule if d.capacity >= target_daily), None)
    # steady state: every mailbox at its cap, except paused (0) and held (ramp start)
    steady = 0
    for mb in unique:
        if mb.complaint_status == "stop":
            continue
        cap = _cap(mb, policy)
        steady += min(cap, policy.ramp_start) if mb.complaint_status == "watch" else cap
    needed = math.ceil(target_daily / policy.campaign_cap)
    shortfall = max(target_daily - steady, 0)
    extra_mailboxes = math.ceil(shortfall / policy.campaign_cap)
    free_slots = sum(max(policy.max_mailboxes_per_domain - n, 0) for n in per_domain_count.values())
    on_new_domains = max(extra_mailboxes - free_slots, 0)
    extra_domains = math.ceil(on_new_domains / policy.max_mailboxes_per_domain)

    if shortfall:
        add(
            "CAPACITY_SHORTFALL",
            "warning",
            f"target {target_daily}/day exceeds steady-state capacity {steady}/day: add "
            f"{extra_mailboxes} mailbox(es) ({extra_domains} new domain(s)) and allow "
            f"{policy.warmup_days} days of warmup plus the ramp",
        )
    elif reached is None:
        add(
            "CAPACITY_TARGET_AFTER_HORIZON",
            "info",
            f"target {target_daily}/day is reachable but not within {days} days",
        )
    elif reached > on:
        add(
            "CAPACITY_RAMPING",
            "info",
            f"capacity today {schedule[0].capacity}/day; target {target_daily}/day from {reached}",
        )

    return CapacityPlan(
        on=on,
        target_daily=target_daily,
        capacity_today=schedule[0].capacity,
        steady_state_capacity=steady,
        target_reached_on=reached,
        mailboxes=today,
        by_domain=dict(sorted(by_domain.items())),
        schedule=schedule,
        mailboxes_needed=needed,
        additional_mailboxes=extra_mailboxes,
        additional_domains=extra_domains,
        findings=findings,
    )
