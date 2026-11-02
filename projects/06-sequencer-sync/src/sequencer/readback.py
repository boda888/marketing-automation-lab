"""Read a campaign's members back from the vendor, all pages, for ``plan_push`` and ``reconcile``.

An incomplete read is worse than none: ``reconcile`` releases every ``outcome_unknown``
key whose lead is absent from the read, so a missing page would let the next run push
leads that already exist. ``read_members`` therefore either returns every page or raises
``IncompleteRead``; it never hands back a partial member set.

Shapes (vendor doc hosts unreachable from the run environment, 2026-11-02):

- Smartlead ``GET /campaigns/{id}/leads?offset=&limit=`` (limit 1-100) →
  ``{total_leads, data: [{lead: {email, …}, status, …}]}`` (``@smartlead/cli`` 0.1.0
  ``CampaignLeadsSchema``). The CLI's own auto-pager expects a bare list here; both
  forms are accepted.
- Instantly ``POST /leads/list`` with ``limit`` (1-100) and ``starting_after`` →
  ``{items: [{email, campaign, …}], next_starting_after}``. The campaign filter key
  differs between vendor-side packages (Activepieces piece 0.2.2 sends ``campaign``,
  ``instantly-cli`` 0.1.22 sends ``campaign_id``); the lab sends ``campaign`` and also
  drops items whose own ``campaign`` field names another campaign, so an ignored
  filter cannot make workspace leads look like campaign members.
- HeyReach ``POST /campaign/GetLeadsFromCampaign`` with ``campaignId``, ``offset``,
  ``limit`` ≤ 100 (``n8n-nodes-heyreach`` 1.0.5): request only — the node does not show
  the response shape, so the caller extracts profile URLs itself.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import quote

from .adapters import HttpRequest, InstantlyAdapter, SmartleadAdapter
from .models import Campaign, normalise_email
from .push import HttpResponse, Transport, TransportError, retry_after_seconds

PAGE_LIMIT = 100


class IncompleteRead(Exception):
    """The member list could not be read to the end; do not reconcile against it."""


@dataclass(frozen=True)
class MemberPage:
    members: list[str]
    cursor: str | int | None  # next page's cursor / offset; None = this was the last page
    dropped: int = 0  # items that named another campaign (filter ignored by the vendor?)


class MemberReader(Protocol):
    vendor: str

    def members_request(self, campaign: Campaign, cursor: str | int | None) -> HttpRequest: ...

    def parse_members(
        self, campaign: Campaign, body: object, cursor: str | int | None
    ) -> MemberPage: ...


@dataclass
class MemberRead:
    vendor: str
    campaign_id: str
    members: set[str] = field(default_factory=set)
    pages: int = 0
    requests: int = 0
    dropped: int = 0


def _emails(values: list[object]) -> list[str]:
    return [e for e in (normalise_email(v) for v in values) if e]


@dataclass(frozen=True)
class SmartleadMembers:
    adapter: SmartleadAdapter
    vendor: str = "smartlead"

    def members_request(self, campaign: Campaign, cursor: str | int | None) -> HttpRequest:
        offset = int(cursor or 0)
        url = f"{self.adapter.base_url}/campaigns/{quote(campaign.campaign_id, safe='')}/leads"
        return HttpRequest(
            "GET",
            url,
            params={"api_key": self.adapter.api_key, "offset": str(offset), "limit": "100"},
            secret_params=frozenset({"api_key"}),
        )

    def parse_members(
        self, campaign: Campaign, body: object, cursor: str | int | None
    ) -> MemberPage:
        total = None
        if isinstance(body, dict):
            rows, total = body.get("data") or [], body.get("total_leads")
        elif isinstance(body, list):
            rows = body
        else:
            raise IncompleteRead(f"smartlead: unexpected page body {type(body).__name__}")
        values: list[object] = []
        for row in rows:
            lead = row.get("lead") if isinstance(row, dict) else None
            values.append(lead.get("email") if isinstance(lead, dict) else None)
        offset = int(cursor or 0) + len(rows)
        more = len(rows) >= PAGE_LIMIT and (total is None or offset < int(total))
        return MemberPage(_emails(values), offset if more else None)


@dataclass(frozen=True)
class InstantlyMembers:
    adapter: InstantlyAdapter
    vendor: str = "instantly"

    def members_request(self, campaign: Campaign, cursor: str | int | None) -> HttpRequest:
        body: dict[str, object] = {"campaign": campaign.campaign_id, "limit": PAGE_LIMIT}
        if cursor is not None:
            body["starting_after"] = str(cursor)
        return HttpRequest(
            "POST",
            f"{self.adapter.base_url}/leads/list",
            headers={"Authorization": f"Bearer {self.adapter.api_key}"},
            json=body,
        )

    def parse_members(
        self, campaign: Campaign, body: object, cursor: str | int | None
    ) -> MemberPage:
        if not isinstance(body, dict):
            raise IncompleteRead(f"instantly: unexpected page body {type(body).__name__}")
        items = [i for i in body.get("items") or [] if isinstance(i, dict)]
        mine = [i for i in items if i.get("campaign") in (None, campaign.campaign_id)]
        nxt = body.get("next_starting_after")
        return MemberPage(
            _emails([i.get("email") for i in mine]),
            nxt if nxt and items else None,
            dropped=len(items) - len(mine),
        )


def _fetch(
    request: HttpRequest,
    transport: Transport,
    retries: int,
    max_wait: float,
    sleep: Callable[[float], None],
) -> tuple[HttpResponse, int]:
    """A read is idempotent, so 429, 5xx and timeouts are retried (unlike a push)."""
    attempt = 0
    while True:
        attempt += 1
        try:
            response = transport.send(request)
        except (TransportError, TimeoutError) as exc:
            if attempt > retries:
                raise IncompleteRead(f"{request.redacted_url()}: {exc}") from exc
            sleep(min(max_wait, 2.0 ** (attempt - 1)))
            continue
        if 200 <= response.status < 300:
            return response, attempt
        if response.status == 429 or response.status >= 500:
            if attempt > retries:
                raise IncompleteRead(f"{request.redacted_url()}: HTTP {response.status}")
            wait = retry_after_seconds(response.headers)
            sleep(min(max_wait, wait if wait is not None else 2.0 ** (attempt - 1)))
            continue
        raise IncompleteRead(f"{request.redacted_url()}: HTTP {response.status}")


def read_members(
    reader: MemberReader,
    campaign: Campaign,
    transport: Transport,
    *,
    max_pages: int = 1000,
    retries: int = 2,
    max_wait_seconds: float = 60.0,
    sleep: Callable[[float], None] = time.sleep,
) -> MemberRead:
    """Every member of ``campaign`` (normalised emails), or ``IncompleteRead``.

    Stops on the vendor's last page; a repeated cursor or more than ``max_pages`` pages
    raises instead of looping or returning a truncated set.
    """
    out = MemberRead(reader.vendor, campaign.campaign_id)
    cursor: str | int | None = None
    seen: set[str | int] = set()
    while True:
        if out.pages >= max_pages:
            raise IncompleteRead(f"{reader.vendor}: more than {max_pages} pages")
        request = reader.members_request(campaign, cursor)
        response, attempts = _fetch(request, transport, retries, max_wait_seconds, sleep)
        out.requests += attempts
        page = reader.parse_members(campaign, response.body, cursor)
        out.pages += 1
        out.members.update(page.members)
        out.dropped += page.dropped
        if page.cursor is None:
            return out
        if page.cursor in seen:
            raise IncompleteRead(f"{reader.vendor}: cursor {page.cursor!r} repeated")
        seen.add(page.cursor)
        cursor = page.cursor
