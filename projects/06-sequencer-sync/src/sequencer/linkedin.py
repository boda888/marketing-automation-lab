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

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from urllib.parse import unquote, urlsplit

from .adapters import HttpRequest
from .models import Campaign, SequencerLead

_SLUG = re.compile(r"^[a-z0-9\-_%.]{3,100}$")


def normalise_linkedin_url(value: object) -> str | None:
    """Canonical member profile URL, or ``None`` for blanks, company pages and non-profiles.

    Accepts ``http(s)``, missing scheme, country / mobile subdomains (``de.``, ``m.``),
    query strings, fragments, trailing slashes and extra path parts after the slug
    (``/in/ana-lopez/details/``); the slug is lower-cased and percent-decoded.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if "://" not in text:
        text = "https://" + text
    try:
        parts = urlsplit(text)
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    if host != "linkedin.com" and not host.endswith(".linkedin.com"):
        return None
    segments = [s for s in parts.path.split("/") if s]
    if len(segments) < 2 or segments[0].lower() != "in":
        return None  # /company/…, /sales/…, /pub/… and bare hosts are not a member key
    slug = unquote(segments[1]).strip().lower()
    if not _SLUG.match(slug):
        return None
    return f"https://www.linkedin.com/in/{slug}"


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

    def lead_payload(self, pair: SenderPair) -> dict[str, object]:
        lead = pair.lead
        body: dict[str, object] = {
            k: v
            for k, v in {
                "profileUrl": pair.profile_url,
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
        if not campaign.campaign_id.isdigit():
            raise ValueError(f"HeyReach campaign ids are numeric, got {campaign.campaign_id!r}")
        return [
            HttpRequest(
                "POST",
                f"{self.base_url}/campaign/AddLeadsToCampaign",
                headers=self._headers(),
                json={
                    "campaignId": int(campaign.campaign_id),
                    "accountLeadPairs": [
                        {"linkedInAccountId": p.sender.account_id, "lead": self.lead_payload(p)}
                        for p in pairs[i : i + self.max_batch]
                    ],
                },
            )
            for i in range(0, len(pairs), self.max_batch)
        ]

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
