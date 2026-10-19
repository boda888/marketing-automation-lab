"""Collapse the same real-world event seen in several places into one signal.

PRACTICE: "same funding/job event counted twice from two sources". Two ``found`` events
are the same event when, for the same company and signal type, one of these holds:

1. ``same_source_id`` — same ``source_name`` and ``source_event_id`` (a re-scrape).
   Source ids are only unique within their source (Lever: per posting; schema.org
   ``JobPosting.identifier``: within the hiring organisation), so ids from different
   sources are never compared with each other.
2. ``same_url`` — the same evidence URL (scheme, ``www.``, trailing slash and fragment
   ignored), e.g. two research runs that read the same press release.
3. ``same_subject_and_date`` — the same normalised ``subject`` ("Series A") and
   overlapping ``occurred_on`` intervals. Both dates are required: no date, no merge.

Two events from the same source with *different* ids are never merged, even when the
subject and date agree: two openings for the same title are two openings. Only
``found`` events are deduplicated; ``not_found_in_sample`` / ``unknown`` checks pass
through untouched, so a missing check never cancels a found one.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from enum import StrEnum
from urllib.parse import urlsplit

from pydantic import BaseModel, Field

from signal_engine.models import DatePrecision, EventDate, SignalEvent, SignalType

_PRECISION_RANK = {DatePrecision.DAY: 0, DatePrecision.MONTH: 1, DatePrecision.YEAR: 2}


class MatchReason(StrEnum):
    SAME_SOURCE_ID = "same_source_id"
    SAME_URL = "same_url"
    SAME_SUBJECT_AND_DATE = "same_subject_and_date"


class Provenance(BaseModel):
    """One sighting of a merged event: where and when it was seen."""

    source_url: str
    source_name: str | None = None
    source_event_id: str | None = None
    observed_at: datetime
    occurred_on: EventDate | None = None
    evidence: str


class MergedSignal(BaseModel):
    """One real-world event, with every source that reported it."""

    company_domain: str
    type: SignalType
    subject: str | None
    occurred_on: EventDate | None = Field(
        description="Most precise date among the sightings (overlapping calendar intervals nest)"
    )
    primary: SignalEvent = Field(description="Sighting with the most precise date, then oldest")
    sightings: list[Provenance]
    match_reasons: list[MatchReason] = Field(description="Why sightings were merged (sorted)")
    date_conflict: bool = Field(
        description="Sightings merged by id or URL give dates that do not overlap: check by hand"
    )

    @property
    def source_count(self) -> int:
        return len({(p.source_name, _url_key(p.source_url)) for p in self.sightings})

    @property
    def first_observed_at(self) -> datetime:
        return min(p.observed_at for p in self.sightings)


class DedupeResult(BaseModel):
    signals: list[MergedSignal]
    checks: list[SignalEvent] = Field(description="not_found_in_sample / unknown, unchanged")
    input_found: int
    duplicates_removed: int


def normalise_subject(text: str | None) -> str | None:
    """Casefold, strip accents and punctuation, collapse spaces: "Série-A " -> "serie a"."""
    if text is None:
        return None
    t = unicodedata.normalize("NFKD", text)
    t = "".join(c for c in t if not unicodedata.combining(c)).casefold()
    t = re.sub(r"[^\w]+", " ", t).strip()
    return t or None


def _url_key(url: str) -> str:
    parts = urlsplit(url)
    host = (parts.hostname or "").removeprefix("www.")
    path = parts.path.rstrip("/")
    return f"{host}{path}?{parts.query}" if parts.query else f"{host}{path}"


def _overlap(a: EventDate, b: EventDate) -> bool:
    return a.earliest <= b.latest and b.earliest <= a.latest


def _conflicts(a: SignalEvent, b: SignalEvent) -> bool:
    """Same source, both with ids, different ids: provably two events."""
    return (
        a.source_name is not None
        and a.source_name == b.source_name
        and a.source_event_id is not None
        and b.source_event_id is not None
        and a.source_event_id != b.source_event_id
    )


def match(a: SignalEvent, b: SignalEvent) -> MatchReason | None:
    """Why ``a`` and ``b`` are the same event, or ``None`` if they are not (or may not be)."""
    if not (a.is_found and b.is_found):
        return None
    if a.company_domain != b.company_domain or a.type is not b.type or _conflicts(a, b):
        return None
    if (
        a.source_name is not None
        and a.source_name == b.source_name
        and a.source_event_id is not None
        and a.source_event_id == b.source_event_id
    ):
        return MatchReason.SAME_SOURCE_ID
    if _url_key(a.source_url) == _url_key(b.source_url):
        return MatchReason.SAME_URL
    sa, sb = normalise_subject(a.subject), normalise_subject(b.subject)
    da, db = a.occurred_on, b.occurred_on
    if sa and sa == sb and da and db and _overlap(da, db):
        return MatchReason.SAME_SUBJECT_AND_DATE
    return None


def _date_conflict(events: list[SignalEvent]) -> bool:
    dates = [e.occurred_on for e in events if e.occurred_on]
    return any(not _overlap(a, b) for i, a in enumerate(dates) for b in dates[i + 1 :])


def _primary(events: list[SignalEvent]) -> SignalEvent:
    def key(item: tuple[int, SignalEvent]) -> tuple[int, datetime, int]:
        i, e = item
        rank = _PRECISION_RANK[e.occurred_on.precision] if e.occurred_on else 3
        return rank, e.observed_at, i

    return min(enumerate(events), key=key)[1]


def dedupe(events: list[SignalEvent]) -> DedupeResult:
    """Group duplicate ``found`` events; deterministic for a given input order.

    Each event joins the first group it matches a member of and conflicts with none of;
    checking every member (not just the first) keeps a chain A~C~B from merging two
    different ids of the same source through a third sighting.
    """
    groups: list[list[SignalEvent]] = []
    reasons: list[set[MatchReason]] = []
    checks: list[SignalEvent] = []
    found = 0
    for e in events:
        if not e.is_found:
            checks.append(e)
            continue
        found += 1
        for members, why in zip(groups, reasons, strict=True):
            if any(_conflicts(e, m) for m in members):
                continue
            hits = {r for m in members if (r := match(e, m)) is not None}
            if hits:
                members.append(e)
                why |= hits
                break
        else:
            groups.append([e])
            reasons.append(set())

    signals = []
    for members, why in zip(groups, reasons, strict=True):
        primary = _primary(members)
        signals.append(
            MergedSignal(
                company_domain=primary.company_domain,
                type=primary.type,
                subject=next((m.subject for m in [primary, *members] if m.subject), None),
                occurred_on=primary.occurred_on,
                primary=primary,
                sightings=[
                    Provenance(
                        source_url=m.source_url,
                        source_name=m.source_name,
                        source_event_id=m.source_event_id,
                        observed_at=m.observed_at,
                        occurred_on=m.occurred_on,
                        evidence=m.evidence,
                    )
                    for m in members
                ],
                match_reasons=sorted(why),
                date_conflict=_date_conflict(members),
            )
        )
    return DedupeResult(
        signals=signals,
        checks=checks,
        input_found=found,
        duplicates_removed=found - len(signals),
    )
