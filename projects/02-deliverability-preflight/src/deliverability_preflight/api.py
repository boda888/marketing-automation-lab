"""FastAPI service wrapping ``preflight()``.

Run (demo, answers DNS from a JSON fixture):

    PREFLIGHT_DNS_FIXTURE=examples/dns_fixture.json \
        uvicorn deliverability_preflight.api:app --port 8002

The fixture maps ``"<name> <TYPE>"`` to a list of record strings.
``PREFLIGHT_DNS=live`` queries real DNS instead (needs the ``[live]`` extra);
``PREFLIGHT_DNS_LIFETIME`` caps each query in seconds (default 5).
"""

from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .capacity import CapacityPlan, CapacityPolicy, Mailbox, plan_capacity
from .complaints import ComplaintStats
from .content import MessageContent
from .dns import FakeResolver, Resolver
from .preflight import PreflightReport, preflight


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


def create_app(resolver: Resolver | None = None) -> FastAPI:
    if resolver is None:
        resolver = resolver_from_env()
    res = resolver
    app = FastAPI(title="Deliverability preflight", version="0.0.9")

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/preflight", response_model=PreflightReport)
    def run_preflight(req: PreflightRequest) -> PreflightReport:
        # DNS temp failures surface as findings (e.g. DMARC_DNS_TEMPERROR), never a silent pass
        return preflight(
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
        )

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

    return app


app = create_app()
