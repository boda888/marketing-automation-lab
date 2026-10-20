"""Deterministic priority rules: deduplicated signals -> Tier 1 / 2 / 3 / Hold / Review.

PRACTICE: an LLM asked "is this round within 12 months?" got the arithmetic wrong on a
few rows, and the tier was trusted anyway. Here every window is checked in code against
the *whole* interval a date can mean (``EventDate.age_days``):

- each signal type has a ``strong`` and a ``medium`` age window (days);
- a signal is settled when its youngest and oldest possible ages fall in the same band;
  otherwise it is pending (straddles a boundary, has no date, or its sightings disagree);
- the account tier is computed twice, with every pending signal at its worst band and at
  its best band. Equal → that tier. Different → the worst-case tier stands (never send
  on an unsettled signal) and ``upgrade_possible`` lists why; if the worst case is Hold,
  the account is ``Review`` instead: maybe a good lead, but nothing settled to send on.
- an ``unknown`` check for a type with no found signal is pending too (best case strong);
  ``not_found_in_sample`` is settled: no signal.

Tier rules (worst-case or best-case, same function): Tier 1 = a strong signal plus at
least one more qualifying signal of another type; Tier 2 = a strong signal; Tier 3 = a
medium signal only; Hold = none.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date
from enum import StrEnum

from pydantic import BaseModel, Field, model_validator

from signal_engine.dedupe import MergedSignal, dedupe
from signal_engine.models import SignalEvent, SignalStatus, SignalType

BEST_SIGNAL_MAX_WORDS = 30
REASON_MAX_WORDS = 45


class Band(StrEnum):
    STRONG = "strong"
    MEDIUM = "medium"
    NONE = "none"


_BAND_RANK = {Band.STRONG: 2, Band.MEDIUM: 1, Band.NONE: 0}


class Tier(StrEnum):
    TIER_1 = "Tier 1"
    TIER_2 = "Tier 2"
    TIER_3 = "Tier 3"
    HOLD = "Hold"
    REVIEW = "Review"


_TIER_RANK = {Tier.TIER_1: 3, Tier.TIER_2: 2, Tier.TIER_3: 1, Tier.HOLD: 0}


class Window(BaseModel):
    """Age bands in days: ``0..strong_days`` strong, ``..medium_days`` medium, older none.

    ``strong_days=None`` caps a type at medium (context, not intent).
    """

    model_config = {"frozen": True}

    strong_days: int | None = Field(default=None, ge=0)
    medium_days: int = Field(ge=0)

    @model_validator(mode="after")
    def _ordered(self) -> Window:
        if self.strong_days is not None and self.strong_days > self.medium_days:
            raise ValueError("strong_days must be <= medium_days")
        return self

    def band(self, age_days: int) -> Band:
        if self.strong_days is not None and age_days <= self.strong_days:
            return Band.STRONG
        return Band.MEDIUM if age_days <= self.medium_days else Band.NONE

    def describe(self, band: Band) -> str:
        if band is Band.STRONG:
            return f"<={self.strong_days}d"
        if band is Band.MEDIUM:
            low = 0 if self.strong_days is None else self.strong_days + 1
            return f"{low}-{self.medium_days}d"
        return f">{self.medium_days}d"


# Defaults are starting points to calibrate (project 12), not facts. Funding <=12 / 13-24
# months follows PRACTICE; a job post proves recruiting now, so hiring decays fast; tech
# stack is context, so it never counts as strong.
DEFAULT_WINDOWS: dict[SignalType, Window] = {
    SignalType.FUNDING: Window(strong_days=365, medium_days=730),
    SignalType.HIRING: Window(strong_days=60, medium_days=180),
    SignalType.EXPANSION: Window(strong_days=180, medium_days=365),
    SignalType.LEADERSHIP: Window(strong_days=120, medium_days=365),
    SignalType.TECH_CHANGE: Window(strong_days=None, medium_days=180),
    SignalType.SITE_VISIT: Window(strong_days=14, medium_days=30),
    # Dated by the latest reported month; growth itself is gated in headcount.py.
    SignalType.HEADCOUNT_GROWTH: Window(strong_days=90, medium_days=180),
}


class Assessment(BaseModel):
    """One signal (or one unknown check) placed against its window."""

    type: SignalType
    subject: str | None = None
    source_url: str | None = None
    occurred_on: str | None = None
    age_days: tuple[int, int] | None = Field(default=None, description="(youngest, oldest)")
    worst: Band
    best: Band
    pending_reason: str | None = Field(
        default=None, description="Why best != worst: straddles, undated, date conflict, unknown"
    )

    @property
    def settled(self) -> bool:
        return self.best is self.worst


class AccountPriority(BaseModel):
    company_domain: str
    as_of: date
    tier: Tier
    worst_case_tier: Tier
    best_case_tier: Tier
    upgrade_possible: bool
    best_signal: str | None = Field(description=f"<= {BEST_SIGNAL_MAX_WORDS} words; None = none")
    reason: str = Field(description=f"<= {REASON_MAX_WORDS} words")
    assessments: list[Assessment]


def _cap_words(text: str, limit: int) -> str:
    words = text.split()
    return text if len(words) <= limit else " ".join(words[: limit - 1]) + " …"


def _age_span(signal: MergedSignal, as_of: date) -> tuple[int, int] | None:
    """Age interval of the signal; with a date conflict, the span of all sightings."""
    if signal.date_conflict:
        dates = [p.occurred_on for p in signal.sightings if p.occurred_on]
        ages = [d.age_days(as_of) for d in dates]
        return min(a[0] for a in ages), max(a[1] for a in ages)
    return signal.occurred_on.age_days(as_of) if signal.occurred_on else None


def assess(signal: MergedSignal, as_of: date, windows: dict[SignalType, Window]) -> Assessment:
    w = windows[signal.type]
    ages = _age_span(signal, as_of)
    base = {
        "type": signal.type,
        "subject": signal.subject,
        "source_url": signal.primary.source_url,
        "occurred_on": str(signal.occurred_on) if signal.occurred_on else None,
        "age_days": ages,
    }
    best_possible = w.band(0)
    if ages is None:
        return Assessment(**base, worst=Band.NONE, best=best_possible, pending_reason="undated")
    worst, best = w.band(ages[1]), w.band(ages[0])
    pending = None
    if worst is not best:
        pending = "dates disagree" if signal.date_conflict else "date straddles a window edge"
    return Assessment(**base, worst=worst, best=best, pending_reason=pending)


def tier_for(bands: Iterable[tuple[SignalType, Band]]) -> Tier:
    by_type: dict[SignalType, Band] = {}
    for t, b in bands:
        if _BAND_RANK[b] > _BAND_RANK[by_type.get(t, Band.NONE)]:
            by_type[t] = b
    strong = sum(b is Band.STRONG for b in by_type.values())
    qualifying = sum(b is not Band.NONE for b in by_type.values())
    if strong and qualifying >= 2:
        return Tier.TIER_1
    if strong:
        return Tier.TIER_2
    return Tier.TIER_3 if qualifying else Tier.HOLD


def _label(a: Assessment) -> str:
    what = a.type.value.replace("_", " ")
    if a.subject:
        what = f"{what}: {a.subject}"
    if a.occurred_on:
        what += f" ({a.occurred_on})"
    return what


def prioritise(
    company_domain: str,
    signals: list[MergedSignal],
    as_of: date,
    checks: Iterable[SignalEvent] = (),
    windows: dict[SignalType, Window] | None = None,
) -> AccountPriority:
    """Tier one account from its deduplicated signals and its non-found checks."""
    windows = windows or DEFAULT_WINDOWS
    domain = company_domain.strip().lower()
    assessments = [assess(s, as_of, windows) for s in signals if s.company_domain == domain]
    found_types = {a.type for a in assessments}
    for c in checks:
        if c.company_domain != domain or c.status is not SignalStatus.UNKNOWN:
            continue
        if c.type not in found_types:
            found_types.add(c.type)
            assessments.append(
                Assessment(
                    type=c.type,
                    worst=Band.NONE,
                    best=windows[c.type].band(0),
                    pending_reason="check result unknown",
                )
            )

    worst = tier_for((a.type, a.worst) for a in assessments)
    best = tier_for((a.type, a.best) for a in assessments)
    upgrade = _TIER_RANK[best] > _TIER_RANK[worst]
    tier = Tier.REVIEW if upgrade and worst is Tier.HOLD else worst

    settled = sorted(
        (a for a in assessments if a.worst is not Band.NONE),
        key=lambda a: (-_BAND_RANK[a.worst], a.age_days[0] if a.age_days else 0),
    )
    best_signal = None
    if settled:
        top = settled[0]
        young, old = top.age_days
        age = f"{young}" if young == old else f"{young}-{old}"
        best_signal = _cap_words(
            f"{_label(top)}, {age} days old, "
            f"{top.worst.value} ({windows[top.type].describe(top.worst)})",
            BEST_SIGNAL_MAX_WORDS,
        )

    parts = []
    if settled:
        counts = {b: sum(a.worst is b for a in settled) for b in (Band.STRONG, Band.MEDIUM)}
        parts.append(
            f"{tier.value}: {counts[Band.STRONG]} strong, {counts[Band.MEDIUM]} medium settled"
        )
    else:
        parts.append(f"{tier.value}: no settled signal in window")
    pending = [a for a in assessments if not a.settled]
    if pending:
        parts.append(
            f"Could reach {best.value}: "
            + "; ".join(f"{a.type.value} {a.pending_reason}" for a in pending)
        )
    reason = _cap_words(". ".join(parts) + ".", REASON_MAX_WORDS)

    return AccountPriority(
        company_domain=domain,
        as_of=as_of,
        tier=tier,
        worst_case_tier=worst,
        best_case_tier=best,
        upgrade_possible=upgrade,
        best_signal=best_signal,
        reason=reason,
        assessments=assessments,
    )


def prioritise_events(
    events: list[SignalEvent], as_of: date, windows: dict[SignalType, Window] | None = None
) -> list[AccountPriority]:
    """Dedupe raw events, then tier every company that appears, in first-seen order."""
    result = dedupe(events)
    domains = list(dict.fromkeys(e.company_domain for e in events))
    return [prioritise(d, result.signals, as_of, result.checks, windows) for d in domains]
