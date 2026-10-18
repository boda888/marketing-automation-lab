"""FastAPI service wrapping ``preflight()``.

Run (demo, answers DNS from a JSON fixture):

    PREFLIGHT_DNS_FIXTURE=examples/dns_fixture.json \
        uvicorn deliverability_preflight.api:app --port 8002

The fixture maps ``"<name> <TYPE>"`` to a list of record strings.
``PREFLIGHT_DNS=live`` queries real DNS instead (needs the ``[live]`` extra);
``PREFLIGHT_DNS_LIFETIME`` caps each query in seconds (default 5).

``PREFLIGHT_ORG_DOMAIN`` picks how Organizational Domains are derived for
alignment and content lint: ``last-two`` (default, naive), ``psl`` (needs
``PREFLIGHT_PSL_FILE``, a local copy of ``public_suffix_list.dat``) or
``tree-walk`` (DMARCbis DNS Tree Walk, a fresh cache per request). The name
used is echoed in every report as ``org_domain_strategy``.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from datetime import date
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .batch import BatchPlan, Lead, assign_batch, unique_leads
from .capacity import CapacityPlan, CapacityPolicy, Mailbox, plan_capacity
from .complaints import ComplaintStats
from .content import MessageContent
from .dmarc import OrgDomainFn, TreeWalkOrgDomain, default_org_domain
from .dns import FakeResolver, Resolver
from .preflight import PreflightReport, preflight
from .psl import PublicSuffixList

# builds the org_domain function for one request from the app's resolver
OrgDomainFactory = Callable[[Resolver], OrgDomainFn]
ORG_DOMAIN_STRATEGIES = ("last-two", "psl", "tree-walk")


class PreflightRequest(BaseModel):
    from_domain: str = Field(min_length=1)
    dkim_selectors: list[str] = []
    dkim_domain: str | None = None
    return_path_domain: str | None = None
    headers: list[tuple[str, str]] | None = None
    complaint_stats: list[ComplaintStats] = []
    content: MessageContent | None = None
    tracking_domain: str | None = None
    author_domain_exists: bool | None = None


class CapacityRequest(BaseModel):
    mailboxes: list[Mailbox]
    target_daily: int = Field(ge=0)
    on: date | None = None
    days: int = Field(default=30, ge=1, le=180)
    policy: CapacityPolicy = CapacityPolicy()


class ScheduleRequest(BaseModel):
    mailboxes: list[Mailbox]
    leads: list[Lead]
    on: date | None = None
    policy: CapacityPolicy = CapacityPolicy()


class ScheduleResponse(BatchPlan):
    plan: CapacityPlan


def load_fixture_resolver(path: str | Path) -> FakeResolver:
    """Build a FakeResolver from ``{"name TYPE": [records...]}`` JSON."""
    raw = json.loads(Path(path).read_text())
    records: dict[tuple[str, str], list[str]] = {}
    for key, values in raw.items():
        name, _, rtype = key.rpartition(" ")
        if not name or not rtype:
            raise ValueError(f"bad fixture key {key!r}; expected '<name> <TYPE>'")
        records[(name, rtype)] = list(values)
    return FakeResolver(records)


def resolver_from_env() -> Resolver:
    """``PREFLIGHT_DNS=live`` → real DNS; else the fixture, else an empty resolver."""
    mode = os.environ.get("PREFLIGHT_DNS", "fixture").strip().lower()
    if mode == "live":
        from .live_dns import DnsPythonResolver

        return DnsPythonResolver(lifetime=float(os.environ.get("PREFLIGHT_DNS_LIFETIME", "5")))
    if mode != "fixture":
        raise ValueError(f"PREFLIGHT_DNS={mode!r}; expected 'live' or 'fixture'")
    fixture = os.environ.get("PREFLIGHT_DNS_FIXTURE")
    return load_fixture_resolver(fixture) if fixture else FakeResolver({})


def org_domain_from_env() -> tuple[str, OrgDomainFactory]:
    """Strategy name and factory from ``PREFLIGHT_ORG_DOMAIN`` / ``PREFLIGHT_PSL_FILE``.

    A misconfiguration raises at startup instead of quietly falling back to
    ``last-two``, which would merge unrelated companies under one suffix.
    """
    name = os.environ.get("PREFLIGHT_ORG_DOMAIN", "last-two").strip().lower()
    if name == "last-two":
        return name, lambda _res: default_org_domain
    if name == "psl":
        path = os.environ.get("PREFLIGHT_PSL_FILE")
        if not path:
            raise ValueError("PREFLIGHT_ORG_DOMAIN=psl needs PREFLIGHT_PSL_FILE")
        psl = PublicSuffixList.from_file(path)
        if not psl.rules:
            raise ValueError(f"PREFLIGHT_PSL_FILE={path!r} has no rules")
        return name, lambda _res: psl.org_domain
    if name == "tree-walk":
        # new instance per request: its cache must not outlive DNS changes
        return name, TreeWalkOrgDomain
    raise ValueError(f"PREFLIGHT_ORG_DOMAIN={name!r}; expected one of {ORG_DOMAIN_STRATEGIES}")


def create_app(
    resolver: Resolver | None = None,
    org_domain: tuple[str, OrgDomainFactory] | None = None,
) -> FastAPI:
    if resolver is None:
        resolver = resolver_from_env()
    if org_domain is None:
        org_domain = org_domain_from_env()
    res = resolver
    strategy, make_org_domain = org_domain
    app = FastAPI(title="Deliverability preflight", version="0.0.9")

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok", "org_domain_strategy": strategy}

    @app.post("/v1/preflight", response_model=PreflightReport)
    def run_preflight(req: PreflightRequest) -> PreflightReport:
        # DNS temp failures surface as findings (e.g. DMARC_DNS_TEMPERROR), never a silent pass
        report = preflight(
            req.from_domain,
            res,
            dkim_selectors=req.dkim_selectors,
            dkim_domain=req.dkim_domain,
            return_path_domain=req.return_path_domain,
            headers=req.headers,
            complaint_stats=req.complaint_stats,
            content=req.content,
            tracking_domain=req.tracking_domain,
            author_domain_exists=req.author_domain_exists,
            org_domain=make_org_domain(res),
        )
        return report.model_copy(update={"org_domain_strategy": strategy})

    @app.post("/v1/capacity", response_model=CapacityPlan)
    def run_capacity(req: CapacityRequest) -> CapacityPlan:
        try:
            return plan_capacity(
                req.mailboxes,
                req.on or date.today(),
                req.target_daily,
                days=req.days,
                policy=req.policy,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/v1/schedule", response_model=ScheduleResponse)
    def run_schedule(req: ScheduleRequest) -> ScheduleResponse:
        # target = the unique leads to send, so the plan's shortfall speaks to this list
        target = len(unique_leads(req.leads)[0])
        plan = plan_capacity(req.mailboxes, req.on or date.today(), target, policy=req.policy)
        batch = assign_batch(plan, req.leads)
        return ScheduleResponse(
            **batch.model_dump(exclude={"findings"}),
            plan=plan,
            findings=[*plan.findings, *batch.findings],
        )

    return app


app = create_app()
