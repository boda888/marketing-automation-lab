"""FastAPI service wrapping ``preflight()``.

Run (demo, answers DNS from a JSON fixture — no live DNS adapter ships yet):

    PREFLIGHT_DNS_FIXTURE=examples/dns_fixture.json \
        uvicorn deliverability_preflight.api:app --port 8002

The fixture maps ``"<name> <TYPE>"`` to a list of record strings.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from fastapi import FastAPI
from pydantic import BaseModel, Field

from .complaints import ComplaintStats
from .dns import FakeResolver, Resolver
from .preflight import PreflightReport, preflight


class PreflightRequest(BaseModel):
    from_domain: str = Field(min_length=1)
    dkim_selectors: list[str] = []
    dkim_domain: str | None = None
    return_path_domain: str | None = None
    headers: list[tuple[str, str]] | None = None
    complaint_stats: list[ComplaintStats] = []


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


def create_app(resolver: Resolver | None = None) -> FastAPI:
    if resolver is None:
        fixture = os.environ.get("PREFLIGHT_DNS_FIXTURE")
        resolver = load_fixture_resolver(fixture) if fixture else FakeResolver({})
    res = resolver
    app = FastAPI(title="Deliverability preflight", version="0.0.3")

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
        )

    return app


app = create_app()
