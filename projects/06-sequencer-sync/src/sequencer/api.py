"""FastAPI service: one call pushes a batch safely, one call settles unknown outcomes.

The caller wires vendors, transport, push log and suppression, so the service never picks
an account or a live transport on its own:

    from sequencer.api import PushService, VendorWiring, create_app
    app = create_app(PushService(vendors={...}, transport=UrllibTransport(),
                                 push_log=SqlitePushLog("push-log.sqlite")), api_token="...")

``POST /v1/push``      resolve the campaign by exact name (missing → 404, duplicate → 409),
                       read its members first when a reader is wired (a partial read → 502,
                       nothing pushed), plan, execute; one outcome per lead for n8n routing.
``POST /v1/reconcile`` re-read the members and settle the ``outcome_unknown`` leads sent
                       back by the caller: found → committed, absent → released. Keys are
                       recomputed from (vendor, campaign id, email), so no server-side
                       report has to survive between the two calls.
"""

from __future__ import annotations

import hmac
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from .adapters import ScopeMapping
from .campaigns import AmbiguousCampaign, CampaignNotFound, resolve_campaign
from .models import Campaign, DedupeScope, SequencerLead, idempotency_key
from .plan import PlannedLead, PushLog, PushPlan, PushState, plan_push
from .push import BatchResult, BatchStatus, PushReport, Transport, execute_plan, reconcile
from .readback import IncompleteRead, MemberReader, read_members
from .suppression import SuppressionList

__version__ = "0.1.0"
MAX_LEADS = 1000
Vendor = Literal["smartlead", "instantly", "lemlist"]
Outcome = Literal["sent", "outcome_unknown", "failed", "skipped"]


@dataclass
class VendorWiring:
    adapter: ScopeMapping
    list_campaigns: Callable[[], list[Campaign]]  # the vendor's campaign list, read fresh
    members: MemberReader | None = None  # None: membership not read (lemlist for now)


@dataclass
class PushService:
    vendors: dict[str, VendorWiring]
    transport: Transport
    push_log: PushLog
    suppression: SuppressionList = field(default_factory=SuppressionList)
    allow_fixtures: bool = False  # only with a fake transport (the demo)
    sleep: Callable[[float], None] = time.sleep


class PushRequest(BaseModel):
    vendor: Vendor
    campaign_name: str = Field(min_length=1, description="Exact campaign name, never an id")
    dedupe_scope: DedupeScope = Field(description="Required: campaign / all_campaigns / workspace")
    leads: list[SequencerLead] = Field(min_length=1)
    required_variables: list[str] = Field(default_factory=list)
    in_other_campaigns: list[str] | None = None
    read_members_first: bool = True


class LeadOutcome(BaseModel):
    lead_id: str
    email: str | None
    outcome: Outcome
    reason: str | None = None  # skip reason or batch status
    detail: str = ""


class PushResponse(BaseModel):
    campaign: Campaign
    membership_checked: bool
    summary: dict[str, object]
    leads: list[LeadOutcome]


class ReconcileRequest(BaseModel):
    vendor: Vendor
    campaign_name: str = Field(min_length=1)
    leads: list[SequencerLead] = Field(min_length=1, description="The outcome_unknown leads")


class ReconcileResponse(BaseModel):
    campaign: Campaign
    members_read: int
    confirmed: list[str] = Field(description="lead_ids found in the campaign: committed")
    cleared: list[str] = Field(description="lead_ids absent: released, safe to push again")
    not_pending: list[str] = Field(description="lead_ids with no reserved key: unchanged")


def _outcomes(plan: PushPlan, report: PushReport) -> list[LeadOutcome]:
    out = [
        LeadOutcome(
            lead_id=s.lead_id,
            email=s.email,
            outcome="skipped",
            reason=s.reason.value,
            detail=s.detail,
        )
        for s in plan.skipped
    ]
    kind: dict[BatchStatus, Outcome] = {
        BatchStatus.ACCEPTED: "sent",
        BatchStatus.OUTCOME_UNKNOWN: "outcome_unknown",
    }
    in_batch: set[str] = set()
    for batch in report.batches:
        for p in batch.leads:
            in_batch.add(p.key)
            out.append(
                LeadOutcome(
                    lead_id=p.lead.lead_id,
                    email=p.lead.email,
                    outcome=kind.get(batch.status, "failed"),
                    reason=batch.status.value,
                    detail=batch.error,
                )
            )
    for p in plan.to_send:  # planned but reserved by a concurrent run before execute
        if p.key not in in_batch:
            out.append(
                LeadOutcome(
                    lead_id=p.lead.lead_id,
                    email=p.lead.email,
                    outcome="skipped",
                    reason="push_pending",
                    detail="reserved by a concurrent run",
                )
            )
    return out


def create_app(service: PushService, *, api_token: str | None = None) -> FastAPI:
    """``api_token`` set → every /v1 call needs ``Authorization: Bearer <token>``."""
    app = FastAPI(title="Sequencer sync", version=__version__)

    def auth(authorization: str | None = Header(default=None)) -> None:
        if api_token is None:
            return
        given = (authorization or "").removeprefix("Bearer ").strip()
        if not hmac.compare_digest(given.encode(), api_token.encode()):
            raise HTTPException(401, "missing or wrong bearer token")

    def wiring(vendor: str) -> VendorWiring:
        if vendor not in service.vendors:
            raise HTTPException(404, f"vendor {vendor!r} is not configured")
        return service.vendors[vendor]

    def campaign(wired: VendorWiring, name: str) -> Campaign:
        try:
            return resolve_campaign(name, wired.list_campaigns())
        except CampaignNotFound as exc:
            raise HTTPException(404, str(exc)) from exc
        except AmbiguousCampaign as exc:
            raise HTTPException(409, str(exc)) from exc

    def members(wired: VendorWiring, target: Campaign) -> set[str]:
        assert wired.members is not None
        try:
            return read_members(
                wired.members, target, service.transport, sleep=service.sleep
            ).members
        except IncompleteRead as exc:
            raise HTTPException(502, f"member read incomplete, nothing changed: {exc}") from exc

    @app.get("/healthz")
    def healthz() -> dict[str, object]:
        return {"status": "ok", "version": __version__, "vendors": sorted(service.vendors)}

    @app.post("/v1/push", response_model=PushResponse, dependencies=[Depends(auth)])
    def push(req: PushRequest) -> PushResponse:
        if len(req.leads) > MAX_LEADS:
            raise HTTPException(413, f"batch limited to {MAX_LEADS} leads")
        wired = wiring(req.vendor)
        target = campaign(wired, req.campaign_name)
        checked = req.read_members_first and wired.members is not None
        before = members(wired, target) if checked else None
        plan = plan_push(
            req.vendor,
            target,
            req.leads,
            dedupe_scope=req.dedupe_scope,
            suppression=service.suppression,
            push_log=service.push_log,
            required_variables=req.required_variables,
            in_campaign=before,
            in_other_campaigns=req.in_other_campaigns,
            allow_fixtures=service.allow_fixtures,
        )
        report = execute_plan(
            plan, wired.adapter, service.transport, service.push_log, sleep=service.sleep
        )
        return PushResponse(
            campaign=target,
            membership_checked=checked,
            summary=report.summary(),
            leads=_outcomes(plan, report),
        )

    @app.post("/v1/reconcile", response_model=ReconcileResponse, dependencies=[Depends(auth)])
    def settle(req: ReconcileRequest) -> ReconcileResponse:
        wired = wiring(req.vendor)
        if wired.members is None:
            raise HTTPException(501, f"no member read-back for {req.vendor}; reconcile by hand")
        target = campaign(wired, req.campaign_name)
        pending: list[PlannedLead] = []
        not_pending: list[str] = []
        for lead in req.leads:
            key = idempotency_key(req.vendor, target.campaign_id, lead.email) if lead.email else ""
            if key and service.push_log.state(key) is PushState.RESERVED:
                pending.append(PlannedLead(lead, key))
            else:
                not_pending.append(lead.lead_id)
        found = members(wired, target) if pending else set()
        report = PushReport(
            vendor=req.vendor,
            campaign_id=target.campaign_id,
            requested_scope="",
            effective_scope="",
            skipped_before_send={},
            batches=[BatchResult(BatchStatus.OUTCOME_UNKNOWN, "", tuple(pending))],
        )
        reconcile(report, service.push_log, found)
        confirmed = [
            p.lead.lead_id for p in pending if service.push_log.state(p.key) is PushState.COMMITTED
        ]
        return ReconcileResponse(
            campaign=target,
            members_read=len(found),
            confirmed=confirmed,
            cleared=[p.lead.lead_id for p in pending if p.lead.lead_id not in set(confirmed)],
            not_pending=not_pending,
        )

    return app
