"""Deterministic, explainable ICP fit + engagement scoring.

Fit (0-100) answers "should we ever sell to this account?" and is built from
firmographic and technographic criteria. Engagement (0-100) answers "is now a
good time?" and is built from time-decayed behavioural events. The two are kept
separate and only combined at the tier step, so every routing decision can be
traced back to individual signals.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from datetime import UTC, datetime

from .config import CategoricalCriterion, ICPConfig, RangeCriterion, TechnographicCriterion
from .models import Company, Contribution, EngagementEvent, ScoreRequest, ScoreResult

TIER_ACTIONS: dict[int, str] = {
    1: "ae_one_to_one",
    2: "nurture_sequence",
    3: "programmatic_outreach",
    4: "deprioritize",
}


def range_match(value: float, c: RangeCriterion) -> float:
    """Trapezoid membership: 1 inside the ideal band, linear falloff to the hard bounds."""
    if value < c.hard_min or value > c.hard_max:
        return 0.0
    if c.ideal_min <= value <= c.ideal_max:
        return 1.0

    def t(x: float) -> float:
        return math.log10(x) if c.log_scale else x

    if value < c.ideal_min:
        lo, hi = t(c.hard_min), t(c.ideal_min)
        return 1.0 if hi == lo else (t(value) - lo) / (hi - lo)
    lo, hi = t(c.ideal_max), t(c.hard_max)
    return 1.0 if hi == lo else (hi - t(value)) / (hi - lo)


def categorical_match(value: str, c: CategoricalCriterion) -> float:
    return c.values.get(value.strip().lower(), c.default)


def tech_match(
    techs: Iterable[str], c: TechnographicCriterion
) -> tuple[float, list[str], list[str]]:
    normalised = {t.strip().lower() for t in techs}
    hits = sorted(t for t in normalised if t in c.positive)
    rivals = sorted(t for t in normalised if t in c.competitors)
    score = min(1.0, sum(c.positive[t] for t in hits))
    if rivals:
        score = max(0.0, score - c.competitor_penalty)
    return score, hits, rivals


def find_disqualifiers(company: Company, cfg: ICPConfig) -> list[str]:
    d = cfg.disqualifiers
    reasons: list[str] = []
    if company.domain in d.domains:
        reasons.append(f"domain {company.domain} is on the exclusion list")
    for suffix in d.domain_suffixes:
        if company.domain.endswith(suffix):
            reasons.append(f"domain suffix {suffix} is excluded")
    if company.industry and company.industry.strip().lower() in d.industries:
        reasons.append(f"industry '{company.industry}' is excluded")
    if company.country and company.country in d.countries:
        reasons.append(f"country {company.country} is excluded")
    return reasons


def _contribution(
    signal: str, value, match: float, weight: float, total: float, reason: str
) -> Contribution:
    max_points = 100.0 * weight / total
    return Contribution(
        signal=signal,
        input=value,
        match=round(match, 4),
        points=round(match * max_points, 2),
        max_points=round(max_points, 2),
        reason=reason,
    )


def score_fit(company: Company, cfg: ICPConfig) -> tuple[float, list[Contribution], list[str]]:
    """Return (fit_score, contributions, missing_fields)."""
    total = cfg.total_weight
    credit = cfg.missing_value_credit
    out: list[Contribution] = []
    missing: list[str] = []

    def unknown(signal: str, field: str, weight: float) -> None:
        missing.append(field)
        out.append(
            _contribution(signal, None, credit, weight, total, f"{field} unknown; partial credit")
        )

    if company.employee_count is None:
        unknown("employees", "employee_count", cfg.employees.weight)
    else:
        m = range_match(company.employee_count, cfg.employees)
        out.append(
            _contribution(
                "employees",
                company.employee_count,
                m,
                cfg.employees.weight,
                total,
                f"ideal {cfg.employees.ideal_min:g}-{cfg.employees.ideal_max:g}",
            )
        )

    if company.annual_revenue_usd is None:
        unknown("revenue_usd", "annual_revenue_usd", cfg.revenue_usd.weight)
    else:
        m = range_match(company.annual_revenue_usd, cfg.revenue_usd)
        out.append(
            _contribution(
                "revenue_usd",
                company.annual_revenue_usd,
                m,
                cfg.revenue_usd.weight,
                total,
                f"ideal {cfg.revenue_usd.ideal_min:,.0f}-{cfg.revenue_usd.ideal_max:,.0f} USD",
            )
        )

    if not company.industry:
        unknown("industry", "industry", cfg.industry.weight)
    else:
        m = categorical_match(company.industry, cfg.industry)
        known = company.industry.strip().lower() in cfg.industry.values
        out.append(
            _contribution(
                "industry",
                company.industry,
                m,
                cfg.industry.weight,
                total,
                "listed in ICP" if known else "not listed; default value",
            )
        )

    if not company.country:
        unknown("country", "country", cfg.country.weight)
    else:
        m = categorical_match(company.country, cfg.country)
        known = company.country.lower() in cfg.country.values
        out.append(
            _contribution(
                "country",
                company.country,
                m,
                cfg.country.weight,
                total,
                "listed in ICP" if known else "not listed; default value",
            )
        )

    if company.technologies is None:
        unknown("technographics", "technologies", cfg.technographics.weight)
    else:
        m, hits, rivals = tech_match(company.technologies, cfg.technographics)
        parts = [f"matched: {', '.join(hits) or 'none'}"]
        if rivals:
            parts.append(f"competitor: {', '.join(rivals)}")
        out.append(
            _contribution(
                "technographics",
                sorted(company.technologies),
                m,
                cfg.technographics.weight,
                total,
                "; ".join(parts),
            )
        )

    fit = sum(c.match * 100.0 * _weight_of(c.signal, cfg) / total for c in out)
    return round(fit, 2), out, missing


def _weight_of(signal: str, cfg: ICPConfig) -> float:
    return {
        "employees": cfg.employees.weight,
        "revenue_usd": cfg.revenue_usd.weight,
        "industry": cfg.industry.weight,
        "country": cfg.country.weight,
        "technographics": cfg.technographics.weight,
    }[signal]


def _as_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def score_engagement(events: Iterable[EngagementEvent], cfg: ICPConfig, as_of: datetime) -> float:
    e = cfg.engagement
    now = _as_utc(as_of)
    raw = 0.0
    contacts: set[str] = set()
    for ev in events:
        weight = e.event_weights.get(ev.type, 0.0)
        if weight <= 0:
            continue
        age_days = max(0.0, (now - _as_utc(ev.occurred_at)).total_seconds() / 86400)
        if age_days > e.max_age_days:
            continue
        raw += weight * 0.5 ** (age_days / e.half_life_days)
        if ev.contact_id:
            contacts.add(ev.contact_id)
    if raw == 0:
        return 0.0
    multiplier = min(
        e.max_stakeholder_multiplier, 1 + e.stakeholder_bonus * max(0, len(contacts) - 1)
    )
    raw *= multiplier
    return round(100.0 * (1 - math.exp(-raw / e.saturation)), 2)


def assign_tier(fit: float, engagement: float, disqualified: bool, cfg: ICPConfig) -> int:
    t = cfg.tiers
    if disqualified or fit < t.fit_medium:
        return 4
    high_eng = engagement >= t.engagement_high
    if fit >= t.fit_high:
        return 1 if high_eng else 2
    return 3 if high_eng else 4


def score(request: ScoreRequest, cfg: ICPConfig) -> ScoreResult:
    company = request.company
    as_of = request.as_of or datetime.now(UTC)
    disq = find_disqualifiers(company, cfg)
    fit, contributions, missing = score_fit(company, cfg)
    engagement = score_engagement(request.events, cfg, as_of)
    missing_weight = sum(_weight_of(c.signal, cfg) for c in contributions if c.input is None)
    if disq:
        fit = 0.0
    tier = assign_tier(fit, engagement, bool(disq), cfg)
    return ScoreResult(
        domain=company.domain,
        fit_score=fit,
        engagement_score=engagement,
        tier=tier,
        action=TIER_ACTIONS[tier],
        disqualified=bool(disq),
        disqualifiers=disq,
        contributions=contributions,
        missing_fields=missing,
        needs_enrichment=not disq and missing_weight / cfg.total_weight >= cfg.enrichment_threshold,
        config_version=cfg.version,
    )
