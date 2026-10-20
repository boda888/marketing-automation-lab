"""Headcount growth from raw monthly counts -> a ``headcount_growth`` signal event.

PRACTICE: headcount growth (6/12-month %, e.g. >= 20% strong, <= -10% contraction) is a
recurring signal, and two failures keep coming back: an LLM doing the percentage
arithmetic wrong, and a missing month stored as ``0`` and read as "no growth". Here:

- the caller passes the raw counts one source reported per month (providers return
  counts at fixed offsets such as 1/3/6/9/12 months back, so gaps are normal);
- growth over each horizon is ``(latest - base) / base`` for the *same* source, with the
  thresholds compared exactly (no float rounding at the 20% edge);
- a missing month, a ``None`` count or a base of 0 makes that horizon ``unknown`` —
  never 0, never interpolated from a neighbouring month;
- contraction is its own state, not "no data" and not "steady";
- counts from different sources are never mixed in one figure: providers count
  observed profiles and disagree with each other, so one series = one source.

Overall state: a strong horizon and a contracting one -> ``mixed`` (check by hand);
any strong -> ``strong_growth``; any contraction -> ``contraction``; any unknown ->
``unknown`` (the missing horizon could still be strong); else ``steady``.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from fractions import Fraction
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, field_validator, model_validator

from signal_engine.models import DatePrecision, EventDate, SignalEvent, SignalStatus, SignalType

DEFAULT_HORIZONS = (6, 12)
SUBJECT = "headcount"


class HeadcountState(StrEnum):
    STRONG_GROWTH = "strong_growth"
    STEADY = "steady"
    CONTRACTION = "contraction"
    MIXED = "mixed"
    UNKNOWN = "unknown"


class HeadcountThresholds(BaseModel):
    """Growth >= ``strong_growth_pct`` is strong; <= ``contraction_pct`` is contraction."""

    model_config = {"frozen": True}

    strong_growth_pct: float = Field(default=20.0, gt=0)
    contraction_pct: float = Field(default=-10.0, lt=0)


def _month(text: str) -> date:
    d = EventDate.parse(text)
    if d.precision is not DatePrecision.MONTH:
        raise ValueError(f"headcount months must be YYYY-MM: {text!r}")
    return d.value


def _shift(month: date, back: int) -> date:
    index = month.year * 12 + month.month - 1 - back
    return date(index // 12, index % 12 + 1, 1)


def _label(month: date) -> str:
    return f"{month.year:04d}-{month.month:02d}"


class HeadcountSeries(BaseModel):
    """Raw monthly employee counts for one company from one source."""

    company_domain: str = Field(min_length=3, examples=["northwind.example"])
    source_name: str = Field(min_length=1, examples=["linkedin_company_page"])
    source_url: str = Field(description="Page or record the counts were read from")
    observed_at: datetime = Field(description="When the source was read (timezone-aware)")
    counts: dict[str, int | None] = Field(
        description="YYYY-MM -> employee count; None or an absent month = not reported",
        examples=[{"2025-10": 40, "2026-04": 50, "2026-10": 62}],
    )

    @field_validator("company_domain")
    @classmethod
    def _clean_domain(cls, v: str) -> str:
        v = v.strip().lower()
        for prefix in ("https://", "http://", "www."):
            v = v.removeprefix(prefix)
        return v.split("/", 1)[0].rstrip(".")

    @field_validator("source_url")
    @classmethod
    def _http_url(cls, v: str) -> str:
        v = v.strip()
        parts = urlsplit(v)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError(f"source_url must be an absolute http(s) URL: {v!r}")
        return v

    @field_validator("observed_at")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None or v.utcoffset() is None:
            raise ValueError("observed_at must be timezone-aware")
        return v

    @field_validator("counts")
    @classmethod
    def _months_and_counts(cls, v: dict[str, int | None]) -> dict[str, int | None]:
        clean: dict[str, int | None] = {}
        for key, count in v.items():
            month = _label(_month(key))
            if month in clean:
                raise ValueError(f"month given twice: {month}")
            if count is not None and count < 0:
                raise ValueError(f"{month}: employee count cannot be negative")
            clean[month] = count
        return dict(sorted(clean.items()))

    @model_validator(mode="after")
    def _not_in_future(self) -> HeadcountSeries:
        seen = self.observed_at.date().replace(day=1)
        late = [m for m in self.counts if _month(m) > seen]
        if late:
            raise ValueError(f"counts after observed_at: {', '.join(late)}")
        return self

    def count(self, month: date) -> int | None:
        return self.counts.get(_label(month))

    @property
    def latest_month(self) -> date | None:
        known = [m for m, c in self.counts.items() if c is not None]
        return _month(known[-1]) if known else None


class Growth(BaseModel):
    """Growth over one horizon, or why it is unknown."""

    months: int
    base_month: str
    latest_month: str
    base: int | None
    latest: int
    pct: float | None = Field(description="Rounded to 0.1 for display; None = unknown")
    state: HeadcountState
    unknown_reason: str | None = None


class HeadcountTrend(BaseModel):
    company_domain: str
    source_name: str
    source_url: str
    observed_at: datetime
    latest_month: str | None
    latest_count: int | None
    growth: list[Growth]
    state: HeadcountState
    reason: str

    def to_event(self) -> SignalEvent:
        """One ``headcount_growth`` check: found (strong), not found (steady /
        contraction, with the figures as the sample) or unknown (missing / mixed)."""
        base = {
            "company_domain": self.company_domain,
            "type": SignalType.HEADCOUNT_GROWTH,
            "observed_at": self.observed_at,
            "source_url": self.source_url,
            "source_name": self.source_name,
        }
        if self.state is HeadcountState.STRONG_GROWTH:
            return SignalEvent(
                **base,
                status=SignalStatus.FOUND,
                subject=SUBJECT,
                occurred_on=self.latest_month,
                evidence=f"{self.source_name}: {self.reason}",
            )
        if self.state in (HeadcountState.STEADY, HeadcountState.CONTRACTION):
            return SignalEvent(
                **base,
                status=SignalStatus.NOT_FOUND_IN_SAMPLE,
                sample=f"monthly headcount from {self.source_name}: {self.reason}",
            )
        return SignalEvent(**base, status=SignalStatus.UNKNOWN)


def _classify(base: int, latest: int, t: HeadcountThresholds) -> HeadcountState:
    change = Fraction(latest - base) * 100
    if change >= Fraction(str(t.strong_growth_pct)) * base:
        return HeadcountState.STRONG_GROWTH
    if change <= Fraction(str(t.contraction_pct)) * base:
        return HeadcountState.CONTRACTION
    return HeadcountState.STEADY


def _growth(series: HeadcountSeries, latest_month: date, months: int, t: HeadcountThresholds):
    base_month = _shift(latest_month, months)
    latest = series.count(latest_month)
    base = series.count(base_month)
    common = {
        "months": months,
        "base_month": _label(base_month),
        "latest_month": _label(latest_month),
        "base": base,
        "latest": latest,
    }
    if base is None:
        why = "not reported" if _label(base_month) in series.counts else "missing"
        return Growth(
            **common, pct=None, state=HeadcountState.UNKNOWN, unknown_reason=f"base month {why}"
        )
    if base == 0:
        return Growth(
            **common, pct=None, state=HeadcountState.UNKNOWN, unknown_reason="base count is 0"
        )
    pct = round(float(Fraction(latest - base, base) * 100), 1)
    return Growth(**common, pct=pct, state=_classify(base, latest, t))


def _overall(states: list[HeadcountState]) -> HeadcountState:
    if HeadcountState.STRONG_GROWTH in states and HeadcountState.CONTRACTION in states:
        return HeadcountState.MIXED
    for state in (
        HeadcountState.STRONG_GROWTH,
        HeadcountState.CONTRACTION,
        HeadcountState.UNKNOWN,
    ):
        if state in states:
            return state
    return HeadcountState.STEADY


def _describe(g: Growth) -> str:
    if g.pct is None:
        return f"{g.months}m unknown ({g.base_month}: {g.unknown_reason})"
    return (
        f"{g.base} ({g.base_month}) -> {g.latest} ({g.latest_month}): "
        f"{g.pct:+.1f}% over {g.months} months"
    )


def assess_headcount(
    series: HeadcountSeries,
    thresholds: HeadcountThresholds | None = None,
    horizons: tuple[int, ...] = DEFAULT_HORIZONS,
) -> HeadcountTrend:
    """Growth over each horizon (months back from the latest reported month)."""
    t = thresholds or HeadcountThresholds()
    if not horizons or any(h < 1 for h in horizons):
        raise ValueError("horizons must be positive month counts")
    common = {
        "company_domain": series.company_domain,
        "source_name": series.source_name,
        "source_url": series.source_url,
        "observed_at": series.observed_at,
    }
    latest_month = series.latest_month
    if latest_month is None:
        return HeadcountTrend(
            **common,
            latest_month=None,
            latest_count=None,
            growth=[],
            state=HeadcountState.UNKNOWN,
            reason="no month with a reported count",
        )
    growth = [_growth(series, latest_month, h, t) for h in sorted(set(horizons))]
    state = _overall([g.state for g in growth])
    return HeadcountTrend(
        **common,
        latest_month=_label(latest_month),
        latest_count=series.count(latest_month),
        growth=growth,
        state=state,
        reason=f"{state.value}: " + "; ".join(_describe(g) for g in growth),
    )
