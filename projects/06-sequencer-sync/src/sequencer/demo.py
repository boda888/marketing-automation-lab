"""Offline demo: the API wired to an in-memory fake Smartlead (no network, no account).

    uvicorn sequencer.demo:app --port 8006

``FakeSmartlead`` answers the same requests the real adapters build (``POST
/campaigns/{id}/leads`` adds the ``lead_list`` emails, ``GET /campaigns/{id}/leads`` pages
the members), so the whole push → read-back → reconcile loop runs. Two scripted faults show
the unknown-outcome path: an email whose local part starts with ``lost+`` is stored but the
response is "lost" (``TransportError(reached_vendor=True)``), one starting with ``down+``
gets a 503 and is not stored. Campaign names: ``Q4 Logistics DE`` (id 101) and two
campaigns both named ``Q1 Retail`` (a 409 on purpose).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlparse

from .adapters import HttpRequest, SmartleadAdapter
from .api import PushService, VendorWiring, create_app
from .models import Campaign
from .plan import InMemoryPushLog
from .push import HttpResponse, TransportError
from .readback import SmartleadMembers
from .suppression import SuppressionList, SuppressionReason

BASE = "https://smartlead.fake/api/v1"
CAMPAIGNS = [
    Campaign(campaign_id="101", name="Q4 Logistics DE", status="ACTIVE"),
    Campaign(campaign_id="201", name="Q1 Retail", status="DRAFTED"),
    Campaign(campaign_id="202", name="Q1 Retail", status="ACTIVE"),
]


@dataclass
class FakeSmartlead:
    members: dict[str, list[str]] = field(default_factory=dict)
    requests: list[HttpRequest] = field(default_factory=list)

    def send(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        parts = urlparse(request.url).path.split("/")  # .../campaigns/{id}/leads
        cid = parts[-2]
        stored = self.members.setdefault(cid, [])
        if request.method == "GET":
            offset = int(request.params.get("offset", 0))
            limit = int(request.params.get("limit", 100))
            page = stored[offset : offset + limit]
            return HttpResponse(
                200, {"total_leads": len(stored), "data": [{"lead": {"email": e}} for e in page]}
            )
        emails = [lead["email"] for lead in request.json["lead_list"]]
        if any(e.startswith("down+") for e in emails):
            return HttpResponse(503, {"message": "service unavailable"})
        stored.extend(e for e in emails if e not in stored)
        if any(e.startswith("lost+") for e in emails):
            raise TransportError("connection reset after send", reached_vendor=True)
        return HttpResponse(200, {"ok": True, "upload_count": len(emails)})


def demo_service() -> PushService:
    adapter = SmartleadAdapter("demo-key", base_url=BASE)
    suppression = SuppressionList()
    suppression.add_email("former.customer@fabrikam.example", SuppressionReason.UNSUBSCRIBED)
    suppression.add_domain("competitor.example", SuppressionReason.DO_NOT_CONTACT)
    return PushService(
        vendors={
            "smartlead": VendorWiring(
                adapter=adapter,
                list_campaigns=lambda: list(CAMPAIGNS),
                members=SmartleadMembers(adapter),
            )
        },
        transport=FakeSmartlead(),
        push_log=InMemoryPushLog(),
        suppression=suppression,
        allow_fixtures=True,  # fake transport only: .example leads never reach a vendor
        sleep=lambda _s: None,
    )


app = create_app(demo_service())
