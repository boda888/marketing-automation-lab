"""Deduplicated signals -> engagement events in the shape 01-icp-scoring reads.

01 scores timing from time-decayed ``EngagementEvent(type, occurred_at, contact_id)``
rows. Fed raw signal events, it would count one funding round seen by a press release
and a funding database twice. Here the input is ``dedupe()``'s output, so:

- one engagement event per ``MergedSignal``, never one per sighting;
- ``not_found_in_sample`` / ``unknown`` checks never become events (no signal is not an
  engagement, and an unknown is not a zero);
- ``occurred_at`` is the **oldest** day the signal can have happened (start of a month
  or year; with a date conflict, the oldest sighting), so 01's decay never makes a
  ``2026-03`` round look fresher than it can be;
- undated signals are skipped with a reason instead of being dated "now";
- ``contact_id`` stays ``None``: signals are account-level, so they must not inflate
  01's multi-stakeholder bonus.

The 01 event type for each signal type comes from ``DEFAULT_EVENT_TYPES`` (overridable);
a signal type mapped to ``None`` is skipped. The weights live in 01's config.
"""

from __future__ import annotations

from datetime import UTC, datetime, time

from pydantic import BaseModel, Field

from signal_engine.dedupe import DedupeResult, MergedSignal
from signal_engine.models import SignalType

# site_visit is already a behavioural event in 01; the rest get their own type names so
# 01 can weight them separately (default_icp.json: "signal_<type>").
DEFAULT_EVENT_TYPES: dict[SignalType, str | None] = {
    t: ("site_visit" if t is SignalType.SITE_VISIT else f"signal_{t.value}") for t in SignalType
}


class EngagementEvent(BaseModel):
    """Same fields as 01's ``EngagementEvent``, plus provenance 01 ignores."""

    type: str
    occurred_at: datetime
    contact_id: str | None = None
    company_domain: str
    signal_type: SignalType
    subject: str | None = None
    source_url: str = Field(description="Evidence URL of the primary sighting")
    sightings: int = Field(ge=1, description="How many sightings were merged into this event")


class SkippedSignal(BaseModel):
    company_domain: str
    signal_type: SignalType
    subject: str | None = None
    reason: str


class EngagementMapping(BaseModel):
    events: list[EngagementEvent]
    skipped: list[SkippedSignal]


def _oldest_day(signal: MergedSignal) -> datetime | None:
    if signal.date_conflict:
        dates = [p.occurred_on.earliest for p in signal.sightings if p.occurred_on]
        day = min(dates) if dates else None
    else:
        day = signal.occurred_on.earliest if signal.occurred_on else None
    return datetime.combine(day, time.min, tzinfo=UTC) if day else None


def to_engagement_events(
    signals: DedupeResult | list[MergedSignal],
    event_types: dict[SignalType, str | None] | None = None,
) -> EngagementMapping:
    """Map deduplicated signals to 01 engagement events, one per merged signal."""
    merged = signals.signals if isinstance(signals, DedupeResult) else signals
    types = DEFAULT_EVENT_TYPES if event_types is None else event_types
    events: list[EngagementEvent] = []
    skipped: list[SkippedSignal] = []
    for s in merged:
        name = types.get(s.type)
        when = _oldest_day(s)
        why = None
        if name is None:
            why = "signal type not mapped to an engagement event"
        elif when is None:
            why = "undated: no occurred_on to decay from"
        if why:
            skipped.append(
                SkippedSignal(
                    company_domain=s.company_domain,
                    signal_type=s.type,
                    subject=s.subject,
                    reason=why,
                )
            )
            continue
        events.append(
            EngagementEvent(
                type=name,
                occurred_at=when,
                company_domain=s.company_domain,
                signal_type=s.type,
                subject=s.subject,
                source_url=s.primary.source_url,
                sightings=len(s.sightings),
            )
        )
    return EngagementMapping(events=events, skipped=skipped)
