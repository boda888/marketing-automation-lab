"""FastAPI service over the batch pipeline: dedupe -> waterfall -> precheck -> ladder.

The providers and the DNS resolver are wired by the caller, so the service never picks a paid
provider or a live resolver on its own:

    from enrichment.api import create_app
    app = create_app(EnrichmentPipeline(waterfall=..., resolver=...))

An n8n HTTP Request node posts candidates (plus earlier verifier checks) and routes each
person on ``action``: ``send``, ``verify``, ``review``, ``suppress``, ``no_email``.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from enrichment import __version__
from enrichment.dedupe import ContactCandidate
from enrichment.ladder import EmailCheck
from enrichment.pipeline import BatchResult, EnrichmentPipeline

MAX_BATCH = 500


class EnrichBatchRequest(BaseModel):
    candidates: list[ContactCandidate] = Field(min_length=1)
    checks: list[EmailCheck] = Field(
        default_factory=list, description="Earlier verifier checks (ZeroBounce, NeverBounce, ...)"
    )
    now: datetime | None = Field(
        default=None, description="Reference time for check freshness; default: server UTC now"
    )


def create_app(pipeline: EnrichmentPipeline) -> FastAPI:
    app = FastAPI(title="Enrichment and verification", version=__version__)

    @app.get("/healthz")
    def healthz() -> dict[str, object]:
        return {
            "status": "ok",
            "version": __version__,
            "email_providers": [p.name for p in pipeline.waterfall.providers],
        }

    @app.post("/v1/enrich/batch", response_model=BatchResult)
    def enrich_batch(req: EnrichBatchRequest) -> BatchResult:
        if len(req.candidates) > MAX_BATCH:
            raise HTTPException(413, f"batch limited to {MAX_BATCH} candidates")
        try:
            return pipeline.run(req.candidates, req.checks, now=req.now or datetime.now(UTC))
        except ValueError as exc:  # duplicate candidate ids, a check dated in the future, ...
            raise HTTPException(422, str(exc)) from exc

    return app
