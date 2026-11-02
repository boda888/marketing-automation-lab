"""LinkedIn pushes: profile URL as the person key, an explicit sender per lead, HeyReach shapes.

LinkedIn tools key a lead by profile URL, not by email, and HeyReach makes the caller pick
the sending account for every lead (``accountLeadPairs[].linkedInAccountId``) and has no
dedupe flag on import (research R5, 2026-10-26). So the lab decides three things itself:

1. **Person key** — a canonical ``https://www.linkedin.com/in/<slug>`` URL; company pages,
   Sales Navigator links and anything else are not a person and are refused.
2. **Sender** — the lead's owner when one is pinned (a conversation never moves to another
   sender behind the owner's back), otherwise the sender with the most weekly invite budget
   left. No budget left = the lead waits, it is never pushed over the cap.
3. **Membership pre-check** — ``GetCampaignsForLead`` by profile URL before adding.

Request shapes come from HeyReach's own n8n package ``n8n-nodes-heyreach`` 1.0.5:
``POST {base}/campaign/AddLeadsToCampaign`` with ``campaignId`` and
``accountLeadPairs: [{linkedInAccountId: int, lead: {profileUrl, firstName, lastName,
companyName, position, emailAddress, customUserFields: [{name, value}]}}]``, header
``X-N8N-KEY``. Not tested against the live API.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from .adapters import HttpRequest
from .models import (
    Campaign,
    DedupeScope,
    SequencerLead,
    idempotency_key,
    normalise_linkedin_url,
    stricter_or_equal,
)
from .plan import PlannedLead, PushLog, PushPlan, PushState, Skipped, SkipReason
from .suppression import SuppressionList


@dataclass(frozen=True)
class SenderAccount:
    """A LinkedIn sending account and its own weekly invite cap (set by you, not LinkedIn)."""

    account_id: int
    name: str
    weekly_invite_budget: int
    used_this_week: int = 0
    active: bool = True

    def __post_init__(self) -> None:
        if self.weekly_invite_budget < 0 or self.used_this_week < 0:
            raise ValueError("invite budget and usage must be >= 0")

    @property
    def remaining(self) -> int:
        return max(0, self.weekly_invite_budget - self.used_this_week) if self.active else 0


class Unassigned(StrEnum):
    NO_PROFILE_URL = "no_profile_url"  # no usable /in/ profile URL
    DUPLICATE_PROFILE = "duplicate_profile"  # same profile earlier in this batch
    ALREADY_IN_CAMPAIGN = "already_in_campaign"  # GetCampaignsForLead said so
    UNKNOWN_OWNER = "unknown_owner"  # pinned owner is not one of the senders
    OWNER_OVER_BUDGET = "owner_over_budget"  # pinned owner inactive or out of budget
    NO_SENDER_BUDGET = "no_sender_budget"  # every active sender is at its weekly cap


@dataclass(frozen=True)
class SenderPair:
    lead: SequencerLead
    profile_url: str
    sender: SenderAccount


@dataclass(frozen=True)
class Held:
    lead: SequencerLead
    reason: Unassigned
    detail: str = ""


@dataclass
class SenderAssignment:
    pairs: list[SenderPair] = field(default_factory=list)
    held: list[Held] = field(default_factory=list)
    used: dict[int, int] = field(default_factory=dict)  # invites planned per sender this run

    def summary(self) -> dict[str, object]:
        reasons: dict[str, int] = {}
        for h in self.held:
            reasons[h.reason.value] = reasons.get(h.reason.value, 0) + 1
        return {
            "assigned": len(self.pairs),
            "held": dict(sorted(reasons.items())),
            "per_sender": dict(sorted(self.used.items())),
        }


def assign_senders(
    leads: Iterable[SequencerLead],
    senders: Sequence[SenderAccount],
    *,
    owners: Mapping[str, int] | None = None,
    in_campaign: Iterable[str] | None = None,
) -> SenderAssignment:
    """Pair each lead with one sender without exceeding any sender's weekly budget.

    ``owners`` maps ``lead_id`` → ``account_id`` for leads that already talk to a sender
    (CRM owner, earlier LinkedIn thread); such a lead only ever goes to that sender.
    Everyone else goes to the active sender with the most budget left (ties: lowest
    ``account_id``), so load spreads evenly and the result is deterministic.
    ``in_campaign`` holds profile URLs (any form) the caller read with
    ``GetCampaignsForLead``; HeyReach has no flag to skip them itself.
    """
    ids = [s.account_id for s in senders]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate sender account_id")
    by_id = {s.account_id: s for s in senders}
    owners = owners or {}
    members = {u for u in (normalise_linkedin_url(v) for v in in_campaign or ()) if u}
    left = {s.account_id: s.remaining for s in senders}
    out = SenderAssignment(used={s.account_id: 0 for s in senders})
    seen: dict[str, str] = {}

    def take(lead: SequencerLead, url: str, account_id: int) -> None:
        left[account_id] -= 1
        out.used[account_id] += 1
        out.pairs.append(SenderPair(lead, url, by_id[account_id]))

    for lead in leads:
        url = normalise_linkedin_url(lead.linkedin_url)
        if url is None:
            out.held.append(Held(lead, Unassigned.NO_PROFILE_URL))
            continue
        if url in seen:
            out.held.append(Held(lead, Unassigned.DUPLICATE_PROFILE, f"same as {seen[url]}"))
            continue
        seen[url] = lead.lead_id
        if url in members:
            out.held.append(Held(lead, Unassigned.ALREADY_IN_CAMPAIGN))
            continue
        owner = owners.get(lead.lead_id)
        if owner is not None:
            if owner not in by_id:
                out.held.append(Held(lead, Unassigned.UNKNOWN_OWNER, str(owner)))
            elif left[owner] <= 0:
                out.held.append(Held(lead, Unassigned.OWNER_OVER_BUDGET, by_id[owner].name))
            else:
                take(lead, url, owner)
            continue
        best = max(ids, key=lambda a: (left[a], -a), default=None)
        if best is None or left[best] <= 0:
            out.held.append(Held(lead, Unassigned.NO_SENDER_BUDGET))
            continue
        take(lead, url, best)
    return out


_HELD_TO_SKIP = {
    Unassigned.UNKNOWN_OWNER: SkipReason.UNKNOWN_OWNER,
    Unassigned.OWNER_OVER_BUDGET: SkipReason.OWNER_OVER_BUDGET,
    Unassigned.NO_SENDER_BUDGET: SkipReason.NO_SENDER_BUDGET,
}


def plan_linkedin_push(
    campaign: Campaign,
    leads: Iterable[SequencerLead],
    senders: Sequence[SenderAccount],
    *,
    dedupe_scope: DedupeScope,
    memberships: Mapping[str, Iterable[str]],
    suppression: SuppressionList | None = None,
    push_log: PushLog | None = None,
    owners: Mapping[str, int] | None = None,
    required_variables: Iterable[str] = (),
    allow_fixtures: bool = False,
    vendor: str = "heyreach",
) -> PushPlan:
    """``plan_push`` for LinkedIn: profile URL as the person key, then one sender per lead.

    ``memberships`` maps profile URL (any form) → campaign ids the person is already in,
    read with ``GetCampaignsForLead`` (every page). HeyReach has no dedupe flag on import,
    so this read *is* the dedupe: a person with no entry is held as
    ``membership_unchecked`` instead of being pushed blind. Checks, first match wins: no
    profile URL, fixture email, suppression (email, domain or profile), same profile
    earlier in the batch, push log, already in this campaign, in another campaign (scope ≥
    ``all_campaigns``), blank required merge tags; survivors then get a sender from
    ``assign_senders``, so held leads never use up a sender's weekly budget.
    """
    required = frozenset(required_variables)
    read: dict[str, set[str]] = {}
    for raw, campaign_ids in memberships.items():
        url = normalise_linkedin_url(raw)
        if url is not None:
            read.setdefault(url, set()).update(str(c) for c in campaign_ids)
    plan = PushPlan(
        vendor=vendor, campaign=campaign, dedupe_scope=dedupe_scope, person_key="linkedin_url"
    )
    seen: dict[str, str] = {}
    keys: dict[str, tuple[str, str]] = {}  # lead_id -> (profile url, idempotency key)
    survivors: list[SequencerLead] = []

    for lead in leads:

        def skip(reason: SkipReason, detail: str = "", _lead: SequencerLead = lead) -> None:
            plan.skipped.append(Skipped(_lead.lead_id, _lead.email, reason, detail))

        url = normalise_linkedin_url(lead.linkedin_url)
        if url is None:
            skip(SkipReason.NO_PROFILE_URL)
            continue
        if lead.is_fixture and not allow_fixtures:
            skip(SkipReason.FIXTURE, "reserved test domain")
            continue
        hit = (
            (suppression.check_profile(url) or suppression.check(lead.email))
            if suppression
            else None
        )
        if hit:
            skip(SkipReason.SUPPRESSED, f"{hit.reason.value}: {hit.matched}")
            continue
        if url in seen:
            skip(SkipReason.DUPLICATE_IN_BATCH, f"same profile as {seen[url]}")
            continue
        seen[url] = lead.lead_id
        key = idempotency_key(vendor, campaign.campaign_id, url)
        state = push_log.state(key) if push_log else None
        if state is PushState.COMMITTED:
            skip(SkipReason.ALREADY_PUSHED)
            continue
        if state is PushState.RESERVED:
            skip(SkipReason.PUSH_PENDING, "earlier push outcome unknown; reconcile first")
            continue
        if url not in read:
            skip(SkipReason.MEMBERSHIP_UNCHECKED, "read GetCampaignsForLead first")
            continue
        if campaign.campaign_id in read[url]:
            skip(SkipReason.ALREADY_IN_CAMPAIGN)
            continue
        if stricter_or_equal(dedupe_scope, DedupeScope.ALL_CAMPAIGNS) and read[url]:
            skip(SkipReason.IN_OTHER_CAMPAIGN, ", ".join(sorted(read[url])))
            continue
        missing = lead.missing_variables(required)
        if missing:
            skip(SkipReason.MISSING_VARIABLES, ", ".join(missing))
            continue
        keys[lead.lead_id] = (url, key)
        survivors.append(lead)

    assignment = assign_senders(survivors, senders, owners=owners)
    for held in assignment.held:
        plan.skipped.append(
            Skipped(held.lead.lead_id, held.lead.email, _HELD_TO_SKIP[held.reason], held.detail)
        )
    for pair in assignment.pairs:
        url, key = keys[pair.lead.lead_id]
        plan.to_send.append(
            PlannedLead(pair.lead, key, person_key=url, sender_id=pair.sender.account_id)
        )
    return plan


@dataclass(frozen=True)
class HeyReachAdapter:
    api_key: str = field(repr=False)
    base_url: str = "https://api.heyreach.io/api/n8n"
    # The vendor node sends whatever it is given; no documented cap was found, so batches
    # stay small enough that one failed call holds back few leads.
    max_batch: int = 100
    vendor: str = "heyreach"

    def _headers(self) -> dict[str, str]:
        return {"X-N8N-KEY": self.api_key}

    @staticmethod
    def _campaign_id(campaign: Campaign) -> int:
        if not campaign.campaign_id.isdigit():
            raise ValueError(f"HeyReach campaign ids are numeric, got {campaign.campaign_id!r}")
        return int(campaign.campaign_id)

    def effective_scope(self, scope: DedupeScope) -> DedupeScope:
        # HeyReach applies no dedupe on import; the scope is enforced by
        # plan_linkedin_push from GetCampaignsForLead, which sees campaigns, not lists.
        return DedupeScope.CAMPAIGN if scope is DedupeScope.CAMPAIGN else DedupeScope.ALL_CAMPAIGNS

    def lead_payload(self, pair: SenderPair) -> dict[str, object]:
        return self._payload(pair.lead, pair.profile_url)

    def _payload(self, lead: SequencerLead, profile_url: str) -> dict[str, object]:
        body: dict[str, object] = {
            k: v
            for k, v in {
                "profileUrl": profile_url,
                "firstName": lead.first_name,
                "lastName": lead.last_name,
                "companyName": lead.company_name,
                "emailAddress": lead.email,
            }.items()
            if v is not None
        }
        body["customUserFields"] = [
            {"name": k, "value": "" if v is None else str(v)}
            for k, v in lead.custom_variables.items()
        ]
        return body

    def add_leads(self, campaign: Campaign, pairs: Sequence[SenderPair]) -> list[HttpRequest]:
        """``AddLeadsToCampaign`` requests; every lead carries its own sender id."""
        campaign_id = self._campaign_id(campaign)
        return [
            HttpRequest(
                "POST",
                f"{self.base_url}/campaign/AddLeadsToCampaign",
                headers=self._headers(),
                json={
                    "campaignId": campaign_id,
                    "accountLeadPairs": [
                        {"linkedInAccountId": p.sender.account_id, "lead": self.lead_payload(p)}
                        for p in pairs[i : i + self.max_batch]
                    ],
                },
            )
            for i in range(0, len(pairs), self.max_batch)
        ]

    def build(
        self, campaign: Campaign, leads: Sequence[PlannedLead], scope: DedupeScope
    ) -> list[HttpRequest]:
        """Requests for ``execute_plan``: planned leads from ``plan_linkedin_push`` only."""
        campaign_id = self._campaign_id(campaign)
        for p in leads:
            if p.person_key is None or p.sender_id is None:
                raise ValueError(
                    f"lead {p.lead.lead_id} has no profile URL or sender; use plan_linkedin_push"
                )
        chunks = [
            tuple(leads[i : i + self.max_batch]) for i in range(0, len(leads), self.max_batch)
        ]
        return [
            HttpRequest(
                "POST",
                f"{self.base_url}/campaign/AddLeadsToCampaign",
                headers=self._headers(),
                json={
                    "campaignId": campaign_id,
                    "accountLeadPairs": [
                        {
                            "linkedInAccountId": p.sender_id,
                            "lead": self._payload(p.lead, p.person_key or ""),
                        }
                        for p in chunk
                    ],
                },
                leads=chunk,
            )
            for chunk in chunks
        ]

    def campaign_leads(self, campaign: Campaign, *, offset: int = 0) -> HttpRequest:
        """``GetLeadsFromCampaign`` page (limit 100) for the reconcile read-back.

        The vendor node does not show the response shape, so the caller extracts the
        profile URLs; ``reconcile`` accepts them in any LinkedIn form.
        """
        if offset < 0:
            raise ValueError("offset must be >= 0")
        return HttpRequest(
            "POST",
            f"{self.base_url}/campaign/GetLeadsFromCampaign",
            headers=self._headers(),
            json={"campaignId": self._campaign_id(campaign), "offset": offset, "limit": 100},
        )

    def campaigns_for_lead(self, profile_url: str, *, offset: int = 0) -> HttpRequest:
        """``GetCampaignsForLead`` by profile URL: the pre-check HeyReach does not do itself.

        Paged (``offset`` / ``limit``, the node allows up to 100 per page); the caller pages
        until a short page before treating a lead as "in no campaign".
        """
        url = normalise_linkedin_url(profile_url)
        if url is None:
            raise ValueError(f"not a LinkedIn member profile URL: {profile_url!r}")
        if offset < 0:
            raise ValueError("offset must be >= 0")
        return HttpRequest(
            "POST",
            f"{self.base_url}/campaign/GetCampaignsForLead",
            headers=self._headers(),
            json={"profileUrl": url, "offset": offset, "limit": 100},
        )
