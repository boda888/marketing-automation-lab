"""Sending-capacity and warmup planner.

Turns "N mailboxes on M domains, I want V emails/day" into a day-by-day ramp
and an explainable verdict: how much can safely go out each day, when the
target is reachable, and how many extra mailboxes are needed if it is not.

Every limit is a parameter of :class:`CapacityConfig`. The defaults mirror the
rough consensus of vendor-affiliated guidance (about 5-10 sends/day on a new
mailbox, 25-30 on a warmed one, ramping over 2-3 weeks); that guidance is
*not* an official provider limit and differs between sources, so treat the
defaults as a conservative starting point and set your own from your
sequencer's and mailbox provider's current docs. Nothing here talks to a
sequencer; it only does the arithmetic.
"""

from __future__ import annotations

import math
from collections import defaultdict

from pydantic import BaseModel, Field, model_validator

from .findings import Finding

_REF = "vendor-guidance defaults (not an official provider limit); configure per account"


class CapacityConfig(BaseModel):
    start_per_mailbox: int = Field(default=5, ge=1)
    warmed_per_mailbox: int = Field(default=30, ge=1)
    ramp_days: int = Field(default=21, ge=1)
    max_per_domain: int = Field(default=90, ge=1)
    max_mailboxes_per_domain: int = Field(default=3, ge=1)

    @model_validator(mode="after")
    def _start_below_warmed(self) -> CapacityConfig:
        if self.start_per_mailbox > self.warmed_per_mailbox:
            raise ValueError("start_per_mailbox cannot exceed warmed_per_mailbox")
        return self


class Mailbox(BaseModel):
    address: str
    age_days: int = Field(default=0, ge=0, description="days of warmup already completed")

    @property
    def domain(self) -> str:
        return self.address.rsplit("@", 1)[-1].strip().lower().rstrip(".")


class DayPlan(BaseModel):
    day: int  # 0 = today
    capacity: int
    per_domain: dict[str, int]


class CapacityPlan(BaseModel):
    target_per_day: int
    steady_state_capacity: int
    day_target_reached: int | None
    additional_mailboxes_needed: int
    days: list[DayPlan]
    findings: list[Finding] = []


def mailbox_cap(age_days: int, cfg: CapacityConfig) -> int:
    """Linear ramp from ``start`` to ``warmed`` over ``ramp_days`` of warmup."""
    if age_days >= cfg.ramp_days:
        return cfg.warmed_per_mailbox
    span = cfg.warmed_per_mailbox - cfg.start_per_mailbox
    return cfg.start_per_mailbox + span * age_days // cfg.ramp_days


def _day_capacity(mailboxes: list[Mailbox], day: int, cfg: CapacityConfig) -> dict[str, int]:
    totals: dict[str, int] = defaultdict(int)
    for m in mailboxes:
        totals[m.domain] += mailbox_cap(m.age_days + day, cfg)
    return {d: min(v, cfg.max_per_domain) for d, v in sorted(totals.items())}


def plan_capacity(
    mailboxes: list[Mailbox],
    target_per_day: int,
    *,
    config: CapacityConfig | None = None,
    horizon_days: int = 42,
    paused: bool = False,
) -> CapacityPlan:
    """Plan safe daily volume. ``paused`` (e.g. complaint status ``stop``) zeroes it."""
    if target_per_day < 0 or horizon_days < 1:
        raise ValueError("target_per_day must be >= 0 and horizon_days >= 1")
    cfg = config or CapacityConfig()
    findings: list[Finding] = []

    def add(code: str, severity: str, message: str) -> None:
        findings.append(Finding(code=code, severity=severity, message=message, ref=_REF))

    days = []
    reached: int | None = None
    for d in range(horizon_days + 1):
        per_domain = {} if paused else _day_capacity(mailboxes, d, cfg)
        cap = sum(per_domain.values())
        days.append(DayPlan(day=d, capacity=cap, per_domain=per_domain))
        if reached is None and cap >= target_per_day and not paused:
            reached = d
    steady = 0 if paused else sum(_day_capacity(mailboxes, 10**6, cfg).values())

    by_domain: dict[str, int] = defaultdict(int)
    for m in mailboxes:
        by_domain[m.domain] += 1
    for dom, n in sorted(by_domain.items()):
        if n > cfg.max_mailboxes_per_domain:
            add(
                "CAPACITY_TOO_MANY_MAILBOXES_PER_DOMAIN",
                "warning",
                f"{dom} has {n} mailboxes (> {cfg.max_mailboxes_per_domain}): "
                "spread them over more domains to limit reputation concentration",
            )
    if paused:
        add(
            "CAPACITY_PAUSED",
            "error",
            "sending is paused (complaint guardrail): planned volume is 0",
        )
    elif not mailboxes:
        add("CAPACITY_NO_MAILBOXES", "error", "no mailboxes: nothing can be sent")
    elif reached is None and steady >= target_per_day:
        add(
            "CAPACITY_TARGET_BEYOND_HORIZON",
            "warning",
            f"{target_per_day}/day is reachable but not within {horizon_days} days of warmup",
        )
    elif steady < target_per_day:
        add(
            "CAPACITY_TARGET_EXCEEDS_CAPACITY",
            "error",
            f"target {target_per_day}/day exceeds steady-state capacity {steady}/day",
        )
    elif reached and reached > 0:
        add(
            "CAPACITY_RAMP_REQUIRED",
            "info",
            f"target {target_per_day}/day is safe from day {reached}; "
            f"send only the planned capacity until then",
        )

    needed = 0
    if not paused and steady < target_per_day:
        needed = math.ceil((target_per_day - steady) / cfg.warmed_per_mailbox)
    return CapacityPlan(
        target_per_day=target_per_day,
        steady_state_capacity=steady,
        day_target_reached=reached,
        additional_mailboxes_needed=needed,
        days=days,
        findings=findings,
    )
