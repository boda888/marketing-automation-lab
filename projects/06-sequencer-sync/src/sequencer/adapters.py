"""Vendor request builders: one ``DedupeScope`` mapped onto each sequencer's own flags.

Request shapes come from the vendors' first-party packages and n8n's built-in nodes
(vendor doc hosts were unreachable from the run environment; see README limits):

- Smartlead ``POST /api/v1/campaigns/{id}/leads`` with ``lead_list`` + ``settings``,
  API key as the ``api_key`` query parameter (``@smartlead/cli`` 0.1.0).
- Instantly ``POST /api/v2/leads/add`` with ``campaign_id``, ``leads``,
  ``skip_if_in_campaign`` / ``skip_if_in_workspace``, Bearer key (``instantly-cli`` 0.1.22).
- lemlist ``POST /api/campaigns/{id}/leads/{email}?deduplicate=``, Basic auth with an
  empty user and the key as password, one lead per call (n8n ``Lemlist`` v2 node).
"""

from __future__ import annotations

import base64
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import quote

from .models import Campaign, DedupeScope, SequencerLead
from .plan import PlannedLead


@dataclass(frozen=True)
class HttpRequest:
    method: str
    url: str
    params: dict[str, str] = field(default_factory=dict, repr=False)  # may hold a key
    headers: dict[str, str] = field(default_factory=dict, repr=False)
    json: object = None
    secret_params: frozenset[str] = frozenset()
    leads: tuple[PlannedLead, ...] = ()

    def redacted_url(self) -> str:
        """URL with query parameters, secrets masked: safe for logs and reports."""
        if not self.params:
            return self.url
        pairs = [
            f"{k}={'***' if k in self.secret_params else quote(str(v), safe='')}"
            for k, v in self.params.items()
        ]
        return f"{self.url}?{'&'.join(pairs)}"


class ScopeMapping(Protocol):
    vendor: str
    max_batch: int

    def effective_scope(self, scope: DedupeScope) -> DedupeScope: ...

    def build(
        self, campaign: Campaign, leads: Sequence[PlannedLead], scope: DedupeScope
    ) -> list[HttpRequest]: ...


def _chunks(items: Sequence[PlannedLead], size: int) -> list[tuple[PlannedLead, ...]]:
    return [tuple(items[i : i + size]) for i in range(0, len(items), size)]


def _drop_none(data: dict[str, object]) -> dict[str, object]:
    return {k: v for k, v in data.items() if v is not None}


def _require_email(lead: SequencerLead) -> str:
    if lead.email is None:  # plan_push never lets this through
        raise ValueError(f"lead {lead.lead_id} has no email")
    return lead.email


@dataclass(frozen=True)
class SmartleadAdapter:
    api_key: str = field(repr=False)
    base_url: str = "https://server.smartlead.ai/api/v1"
    max_batch: int = 400
    vendor: str = "smartlead"

    def effective_scope(self, scope: DedupeScope) -> DedupeScope:
        # One cross-campaign flag; Smartlead has no lead-list scope beyond campaigns.
        return DedupeScope.CAMPAIGN if scope is DedupeScope.CAMPAIGN else DedupeScope.ALL_CAMPAIGNS

    def settings(self, scope: DedupeScope) -> dict[str, bool]:
        """Every flag explicit. ``False`` means "the check runs"; block lists always run."""
        return {
            "ignore_global_block_list": False,
            "ignore_unsubscribe_list": False,
            "ignore_community_bounce_list": False,
            "ignore_duplicate_leads_in_other_campaign": scope is DedupeScope.CAMPAIGN,
        }

    def lead_payload(self, lead: SequencerLead) -> dict[str, object]:
        return _drop_none(
            {
                "email": _require_email(lead),
                "first_name": lead.first_name,
                "last_name": lead.last_name,
                "company_name": lead.company_name,
                "website": lead.website,
                "linkedin_profile": lead.linkedin_url,
                "custom_fields": dict(lead.custom_variables) or None,
            }
        )

    def build(
        self, campaign: Campaign, leads: Sequence[PlannedLead], scope: DedupeScope
    ) -> list[HttpRequest]:
        url = f"{self.base_url}/campaigns/{quote(campaign.campaign_id, safe='')}/leads"
        return [
            HttpRequest(
                "POST",
                url,
                params={"api_key": self.api_key},
                secret_params=frozenset({"api_key"}),
                json={
                    "lead_list": [self.lead_payload(p.lead) for p in chunk],
                    "settings": self.settings(scope),
                },
                leads=chunk,
            )
            for chunk in _chunks(leads, self.max_batch)
        ]


@dataclass(frozen=True)
class InstantlyAdapter:
    api_key: str = field(repr=False)
    base_url: str = "https://api.instantly.ai/api/v2"
    max_batch: int = 1000
    vendor: str = "instantly"

    def effective_scope(self, scope: DedupeScope) -> DedupeScope:
        # No "other campaigns only" flag: the next stricter one is the whole workspace.
        return DedupeScope.CAMPAIGN if scope is DedupeScope.CAMPAIGN else DedupeScope.WORKSPACE

    def flags(self, scope: DedupeScope) -> dict[str, bool]:
        return {
            "skip_if_in_campaign": True,
            "skip_if_in_workspace": scope is not DedupeScope.CAMPAIGN,
        }

    def lead_payload(self, lead: SequencerLead) -> dict[str, object]:
        return _drop_none(
            {
                "email": _require_email(lead),
                "first_name": lead.first_name,
                "last_name": lead.last_name,
                "company_name": lead.company_name,
                "website": lead.website,
                "custom_variables": dict(lead.custom_variables) or None,
            }
        )

    def build(
        self, campaign: Campaign, leads: Sequence[PlannedLead], scope: DedupeScope
    ) -> list[HttpRequest]:
        return [
            HttpRequest(
                "POST",
                f"{self.base_url}/leads/add",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={
                    "campaign_id": campaign.campaign_id,
                    "leads": [self.lead_payload(p.lead) for p in chunk],
                    **self.flags(scope),
                },
                leads=chunk,
            )
            for chunk in _chunks(leads, self.max_batch)
        ]


_LEMLIST_FIELDS = frozenset(
    {
        "firstName",
        "lastName",
        "companyName",
        "companyDomain",
        "linkedinUrl",
        "findEmail",
        "findPhone",
        "verifyEmail",
        "linkedinEnrichment",
        "icebreaker",
        "jobTitle",
        "phone",
        "picture",
    }
)


@dataclass(frozen=True)
class LemlistAdapter:
    api_key: str = field(repr=False)
    base_url: str = "https://api.lemlist.com/api"
    max_batch: int = 1
    vendor: str = "lemlist"

    def effective_scope(self, scope: DedupeScope) -> DedupeScope:
        return DedupeScope.CAMPAIGN if scope is DedupeScope.CAMPAIGN else DedupeScope.ALL_CAMPAIGNS

    def lead_payload(self, lead: SequencerLead) -> dict[str, object]:
        clash = sorted(set(lead.custom_variables) & _LEMLIST_FIELDS)
        if clash:
            raise ValueError(f"custom variables shadow lemlist fields: {clash}")
        body = _drop_none(
            {
                "firstName": lead.first_name,
                "lastName": lead.last_name,
                "companyName": lead.company_name,
                "linkedinUrl": lead.linkedin_url,
                **lead.custom_variables,
            }
        )
        # A push must never start paid lookups as a side effect.
        body.update(findEmail=False, findPhone=False, verifyEmail=False, linkedinEnrichment=False)
        return body

    def build(
        self, campaign: Campaign, leads: Sequence[PlannedLead], scope: DedupeScope
    ) -> list[HttpRequest]:
        token = base64.b64encode(f":{self.api_key}".encode()).decode()
        cid = quote(campaign.campaign_id, safe="")
        return [
            HttpRequest(
                "POST",
                f"{self.base_url}/campaigns/{cid}/leads/{quote(_require_email(p.lead), safe='@')}",
                params={"deduplicate": "false" if scope is DedupeScope.CAMPAIGN else "true"},
                headers={"Authorization": f"Basic {token}"},
                json=self.lead_payload(p.lead),
                leads=(p,),
            )
            for p in leads
        ]
