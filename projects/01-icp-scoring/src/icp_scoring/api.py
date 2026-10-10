"""FastAPI service exposing the scorer.

Run:  ICP_CONFIG_PATH=./my_icp.json uvicorn icp_scoring.api:app --port 8001
"""

from __future__ import annotations

import os

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .config import ICPConfig, load_config
from .models import ScoreRequest, ScoreResult
from .scoring import score

MAX_BATCH = 500


class BatchRequest(BaseModel):
    items: list[ScoreRequest] = Field(min_length=1)


class BatchResponse(BaseModel):
    results: list[ScoreResult]
    tier_counts: dict[str, int]


def create_app(config: ICPConfig | None = None) -> FastAPI:
    cfg = config or load_config(os.environ.get("ICP_CONFIG_PATH"))
    app = FastAPI(title="ICP scoring", version="0.1.0")

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok", "config_version": cfg.version}

    @app.get("/v1/config", response_model=ICPConfig)
    def get_config() -> ICPConfig:
        return cfg

    @app.post("/v1/score", response_model=ScoreResult)
    def score_one(req: ScoreRequest) -> ScoreResult:
        return score(req, cfg)

    @app.post("/v1/score/batch", response_model=BatchResponse)
    def score_batch(req: BatchRequest) -> BatchResponse:
        if len(req.items) > MAX_BATCH:
            raise HTTPException(413, f"batch limited to {MAX_BATCH} items")
        results = [score(item, cfg) for item in req.items]
        counts = {str(t): 0 for t in (1, 2, 3, 4)}
        for r in results:
            counts[str(r.tier)] += 1
        return BatchResponse(results=results, tier_counts=counts)

    return app


app = create_app()
