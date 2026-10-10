"""ICP configuration: what a "good fit" account looks like, expressed as data.

Everything that a RevOps person would want to tune lives here, so the scoring
code itself never needs to change when the ICP changes.
"""

from __future__ import annotations

import json
from importlib import resources
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RangeCriterion(_Strict):
    """Continuous criterion with a trapezoid shape.

    Values inside [ideal_min, ideal_max] score 1.0. Between the ideal edge and
    the hard bound the score falls linearly to 0.0. Outside the hard bounds the
    score is 0.0. With ``log_scale`` the interpolation happens on log10 values,
    which suits quantities that span orders of magnitude (headcount, revenue):
    a 40-person company is "closer" to a 50-person ideal than 4,000 is to 5,000.
    """

    weight: float = Field(ge=0)
    hard_min: float = Field(ge=0)
    ideal_min: float = Field(ge=0)
    ideal_max: float = Field(ge=0)
    hard_max: float = Field(ge=0)
    log_scale: bool = True

    @model_validator(mode="after")
    def _ordered(self) -> RangeCriterion:
        if not (self.hard_min <= self.ideal_min <= self.ideal_max <= self.hard_max):
            raise ValueError("expected hard_min <= ideal_min <= ideal_max <= hard_max")
        if self.log_scale and self.hard_min <= 0:
            raise ValueError("log_scale requires hard_min > 0")
        return self


class CategoricalCriterion(_Strict):
    """Maps a category (industry, country) to a 0..1 match value."""

    weight: float = Field(ge=0)
    values: dict[str, float]
    default: float = Field(default=0.0, ge=0, le=1)

    @model_validator(mode="after")
    def _bounded(self) -> CategoricalCriterion:
        for key, val in self.values.items():
            if not 0 <= val <= 1:
                raise ValueError(f"value for {key!r} must be within [0, 1]")
        self.values = {k.strip().lower(): v for k, v in self.values.items()}
        return self


class TechnographicCriterion(_Strict):
    """Positive technologies add up (capped at 1.0); competitor tech subtracts."""

    weight: float = Field(ge=0)
    positive: dict[str, float] = Field(default_factory=dict)
    competitors: list[str] = Field(default_factory=list)
    competitor_penalty: float = Field(default=0.5, ge=0, le=1)

    @model_validator(mode="after")
    def _normalise(self) -> TechnographicCriterion:
        self.positive = {k.strip().lower(): v for k, v in self.positive.items()}
        self.competitors = [c.strip().lower() for c in self.competitors]
        return self


class Disqualifiers(_Strict):
    """Hard rules: if any matches, fit is 0 and the account goes to tier 4."""

    industries: list[str] = Field(default_factory=list)
    countries: list[str] = Field(default_factory=list)
    domain_suffixes: list[str] = Field(default_factory=list)
    domains: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _normalise(self) -> Disqualifiers:
        self.industries = [i.strip().lower() for i in self.industries]
        self.countries = [c.strip().upper() for c in self.countries]
        self.domain_suffixes = [s.strip().lower() for s in self.domain_suffixes]
        self.domains = [d.strip().lower() for d in self.domains]
        return self


class EngagementConfig(_Strict):
    """Behavioural signals with exponential time decay.

    raw = sum(weight(event) * 0.5 ** (age_days / half_life_days))
    raw *= 1 + stakeholder_bonus * (distinct_contacts - 1)   (capped)
    score = 100 * (1 - exp(-raw / saturation))
    """

    event_weights: dict[str, float]
    half_life_days: float = Field(default=14.0, gt=0)
    max_age_days: float = Field(default=90.0, gt=0)
    saturation: float = Field(default=10.0, gt=0)
    stakeholder_bonus: float = Field(default=0.15, ge=0)
    max_stakeholder_multiplier: float = Field(default=1.6, ge=1)


class TierThresholds(_Strict):
    fit_high: float = Field(default=70, ge=0, le=100)
    fit_medium: float = Field(default=45, ge=0, le=100)
    engagement_high: float = Field(default=50, ge=0, le=100)

    @model_validator(mode="after")
    def _ordered(self) -> TierThresholds:
        if self.fit_medium > self.fit_high:
            raise ValueError("fit_medium must be <= fit_high")
        return self


class ICPConfig(_Strict):
    version: str
    employees: RangeCriterion
    revenue_usd: RangeCriterion
    industry: CategoricalCriterion
    country: CategoricalCriterion
    technographics: TechnographicCriterion
    disqualifiers: Disqualifiers = Field(default_factory=Disqualifiers)
    engagement: EngagementConfig
    tiers: TierThresholds = Field(default_factory=TierThresholds)
    missing_value_credit: float = Field(
        default=0.3,
        ge=0,
        le=1,
        description="Partial credit for a criterion whose input is unknown.",
    )
    enrichment_threshold: float = Field(
        default=0.25,
        ge=0,
        le=1,
        description="Flag needs_enrichment when this share of total weight is missing.",
    )

    @model_validator(mode="after")
    def _some_weight(self) -> ICPConfig:
        if self.total_weight <= 0:
            raise ValueError("at least one criterion needs a positive weight")
        return self

    @property
    def total_weight(self) -> float:
        return (
            self.employees.weight
            + self.revenue_usd.weight
            + self.industry.weight
            + self.country.weight
            + self.technographics.weight
        )


def load_config(path: str | Path | None = None) -> ICPConfig:
    """Load a config from ``path`` or fall back to the packaged example ICP."""
    if path is None:
        text = resources.files("icp_scoring").joinpath("default_icp.json").read_text("utf-8")
    else:
        text = Path(path).read_text("utf-8")
    return ICPConfig.model_validate(json.loads(text))
