"""Decide, before any HTTP call, which leads go to a campaign and why the others do not."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from .models import Campaign, DedupeScope, SequencerLead, idempotency_key, stricter_or_equal
from .suppression import SuppressionList


class SkipReason(StrEnum):
    MISSING_EMAIL = "missing_email"
    FIXTURE = "fixture"  # reserved test domain, real account
    SUPPRESSED = "suppressed"  # global list: detail carries the reason
    DUPLICATE_IN_BATCH = "duplicate_in_batch"
    ALREADY_PUSHED = "already_pushed"  # idempotency log: committed earlier
    PUSH_PENDING = "push_pending"  # idempotency log: earlier outcome unknown, reconcile first
    ALREADY_IN_CAMPAIGN = "already_in_campaign"
    IN_OTHER_CAMPAIGN = "in_other_campaign"
    IN_WORKSPACE = "in_workspace"
    MISSING_VARIABLES = "missing_variables"
    # LinkedIn pushes (person key = profile URL, one sender per lead)
    NO_PROFILE_URL = "no_profile_url"
    MEMBERSHIP_UNCHECKED = "membership_unchecked"  # no GetCampaignsForLead read for this person
    UNKNOWN_OWNER = "unknown_owner"
    OWNER_OVER_BUDGET = "owner_over_budget"
    NO_SENDER_BUDGET = "no_sender_budget"


class PushState(StrEnum):
    RESERVED = "reserved"
    COMMITTED = "committed"


class PushLog(Protocol):
    """Idempotency store: a key is reserved before the call, committed once it is confirmed."""

    def state(self, key: str) -> PushState | None: ...

    def reserve(self, key: str) -> bool: ...

    def commit(self, key: str) -> None: ...

    def release(self, key: str) -> None: ...


@dataclass
class InMemoryPushLog:
    states: dict[str, PushState] = field(default_factory=dict)

    def state(self, key: str) -> PushState | None:
        return self.states.get(key)

    def reserve(self, key: str) -> bool:
        """Reserve ``key``; ``False`` if it is already reserved or committed."""
        if key in self.states:
            return False
        self.states[key] = PushState.RESERVED
        return True

    def commit(self, key: str) -> None:
        self.states[key] = PushState.COMMITTED

    def release(self, key: str) -> None:
        """Drop a reservation after a call that certainly did not reach the vendor."""
        if self.states.get(key) is PushState.RESERVED:
            del self.states[key]


@dataclass(frozen=True)
class Skipped:
    lead_id: str
    email: str | None
    reason: SkipReason
    detail: str = ""


@dataclass(frozen=True)
class PlannedLead:
    lead: SequencerLead
    key: str  # idempotency key for (vendor, campaign, person key)
    person_key: str | None = None  # canonical profile URL for LinkedIn; None = the email
    sender_id: int | None = None  # LinkedIn sending account chosen for this lead

    @property
    def member(self) -> str:
        """The value the vendor's member list is matched on during reconcile."""
        return self.person_key or self.lead.email or ""


@dataclass
class PushPlan:
    vendor: str
    campaign: Campaign
    dedupe_scope: DedupeScope
    to_send: list[PlannedLead] = field(default_factory=list)
    skipped: list[Skipped] = field(default_factory=list)
    person_key: str = "email"  # "email" or "linkedin_url": what reconcile matches on

    @property
    def skipped_by_reason(self) -> dict[str, int]:
        return dict(sorted(Counter(s.reason.value for s in self.skipped).items()))

    def summary(self) -> dict[str, object]:
        return {
            "vendor": self.vendor,
            "campaign_id": self.campaign.campaign_id,
            "campaign_name": self.campaign.name,
            "dedupe_scope": self.dedupe_scope.value,
            "person_key": self.person_key,
            "input": len(self.to_send) + len(self.skipped),
            "to_send": len(self.to_send),
            "skipped": self.skipped_by_reason,
        }


def _emails(values: Iterable[str] | None) -> frozenset[str] | None:
    return None if values is None else frozenset(v.strip().lower() for v in values)


def plan_push(
    vendor: str,
    campaign: Campaign,
    leads: Iterable[SequencerLead],
    *,
    dedupe_scope: DedupeScope,
    suppression: SuppressionList | None = None,
    push_log: PushLog | None = None,
    required_variables: Iterable[str] = (),
    in_campaign: Iterable[str] | None = None,
    in_other_campaigns: Iterable[str] | None = None,
    in_workspace: Iterable[str] | None = None,
    allow_fixtures: bool = False,
) -> PushPlan:
    """Split ``leads`` into ``to_send`` and ``skipped`` (one reason each, first match wins).

    Checks, in order: no usable email, fixture address (unless ``allow_fixtures``, meant
    for fake transports only), global suppression, same email earlier in this batch,
    idempotency log (committed → already pushed, reserved → outcome unknown), membership
    sets the caller fetched from the vendor (``None`` = not fetched; the vendor flag still
    applies), and required merge tags that are blank — a template with an empty variable
    is never sent. Membership outside the campaign only counts when ``dedupe_scope``
    covers it; ``dedupe_scope`` has no default on purpose.
    """
    required = frozenset(required_variables)
    here, other, anywhere = _emails(in_campaign), _emails(in_other_campaigns), _emails(in_workspace)
    plan = PushPlan(vendor=vendor, campaign=campaign, dedupe_scope=dedupe_scope)
    seen: dict[str, str] = {}

    for lead in leads:
        email = lead.email

        def skip(reason: SkipReason, detail: str = "", _lead: SequencerLead = lead) -> None:
            plan.skipped.append(Skipped(_lead.lead_id, _lead.email, reason, detail))

        if email is None:
            skip(SkipReason.MISSING_EMAIL)
            continue
        if lead.is_fixture and not allow_fixtures:
            skip(SkipReason.FIXTURE, "reserved test domain")
            continue
        hit = suppression.check(email) if suppression else None
        if hit:
            skip(SkipReason.SUPPRESSED, f"{hit.reason.value}: {hit.matched}")
            continue
        if email in seen:
            skip(SkipReason.DUPLICATE_IN_BATCH, f"same email as {seen[email]}")
            continue
        seen[email] = lead.lead_id
        key = idempotency_key(vendor, campaign.campaign_id, email)
        state = push_log.state(key) if push_log else None
        if state is PushState.COMMITTED:
            skip(SkipReason.ALREADY_PUSHED)
            continue
        if state is PushState.RESERVED:
            skip(SkipReason.PUSH_PENDING, "earlier push outcome unknown; reconcile first")
            continue
        if here is not None and email in here:
            skip(SkipReason.ALREADY_IN_CAMPAIGN)
            continue
        if (
            stricter_or_equal(dedupe_scope, DedupeScope.ALL_CAMPAIGNS)
            and other is not None
            and email in other
        ):
            skip(SkipReason.IN_OTHER_CAMPAIGN)
            continue
        if dedupe_scope is DedupeScope.WORKSPACE and anywhere is not None and email in anywhere:
            skip(SkipReason.IN_WORKSPACE)
            continue
        missing = lead.missing_variables(required)
        if missing:
            skip(SkipReason.MISSING_VARIABLES, ", ".join(missing))
            continue
        plan.to_send.append(PlannedLead(lead, key))
    return plan
