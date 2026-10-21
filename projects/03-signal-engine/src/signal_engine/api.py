"""FastAPI service: raw signal events (+ headcount series) in -> dedupe + tiers out.

Run:  uvicorn signal_engine.api:app --port 8003

One call does the whole deterministic part of the engine, so an n8n workflow never has
to do dates, windows or dedupe in expressions:

1. every ``HeadcountSeries`` is assessed and becomes a ``headcount_growth`` check;
2. all events are deduplicated once (``dedupe``);
3. every company is tiered from its merged signals and checks (``prioritise``);
4. merged signals are mapped to 01 engagement events (``to_engagement_events``).
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from signal_engine import __version__
from signal_engine.dedupe import dedupe
from signal_engine.engagement import EngagementEvent, SkippedSignal, to_engagement_events
from signal_engine.headcount import HeadcountSeries, HeadcountTrend, assess_headcount
from signal_engine.models import SignalEvent
from signal_engine.priority import AccountPriority, Tier, prioritise

MAX_EVENTS = 2000
MAX_SERIES = 500


class PrioritiseRequest(BaseModel):
    events: list[SignalEvent] = Field(default_factory=list)
    headcount: list[HeadcountSeries] = Field(
        default_factory=list, description="Raw monthly counts, one source per series"
    )
    as_of: date | None = Field(
        default=None, description="Reference day for signal ages; defaults to today (UTC)"
    )


class DedupeSummary(BaseModel):
    input_found: int = Field(description="found events in, headcount events included")
    duplicates_removed: int
    merged_signals: int


class PrioritiseResponse(BaseModel):
    as_of: date
    accounts: list[AccountPriority] = Field(description="One per company, first-seen order")
    tier_counts: dict[str, int]
    dedupe: DedupeSummary
    headcount: list[HeadcountTrend]
    engagement_events: list[EngagementEvent] = Field(
        description="One per merged signal, ready for 01's ScoreRequest.events"
    )
    skipped_for_engagement: list[SkippedSignal]


def run(req: PrioritiseRequest) -> PrioritiseResponse:
    as_of = req.as_of or datetime.now(UTC).date()
    trends = [assess_headcount(s) for s in req.headcount]
    events = [*req.events, *(t.to_event() for t in trends)]
    result = dedupe(events)
    domains = list(dict.fromkeys(e.company_domain for e in events))
    accounts = [prioritise(d, result.signals, as_of, result.checks) for d in domains]
    counts = {t.value: 0 for t in Tier}
    for a in accounts:
        counts[a.tier.value] += 1
    mapping = to_engagement_events(result)
    return PrioritiseResponse(
        as_of=as_of,
        accounts=accounts,
        tier_counts=counts,
        dedupe=DedupeSummary(
            input_found=result.input_found,
            duplicates_removed=result.duplicates_removed,
            merged_signals=len(result.signals),
        ),
        headcount=trends,
        engagement_events=mapping.events,
        skipped_for_engagement=mapping.skipped,
    )


def create_app() -> FastAPI:
    app = FastAPI(title="Signal engine", version=__version__)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.post("/v1/signals/prioritise", response_model=PrioritiseResponse)
    def prioritise_signals(req: PrioritiseRequest) -> PrioritiseResponse:
        if len(req.events) > MAX_EVENTS:
            raise HTTPException(413, f"limited to {MAX_EVENTS} events per request")
        if len(req.headcount) > MAX_SERIES:
            raise HTTPException(413, f"limited to {MAX_SERIES} headcount series per request")
        if not req.events and not req.headcount:
            raise HTTPException(422, "send at least one event or headcount series")
        return run(req)

    return app


app = create_app()
