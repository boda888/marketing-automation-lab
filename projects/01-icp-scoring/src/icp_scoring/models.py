"""Request / response models shared by the library, the API and the CLI."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class Company(BaseModel):
    domain: str = Field(min_length=3, examples=["acme.io"])
    name: str | None = None
    industry: str | None = None
    employee_count: int | None = Field(default=None, ge=0)
    annual_revenue_usd: float | None = Field(default=None, ge=0)
    country: str | None = Field(default=None, description="ISO 3166-1 alpha-2")
    technologies: list[str] | None = Field(
        default=None, description="None = unknown; [] = known to use nothing relevant"
    )

    @field_validator("domain")
    @classmethod
    def _clean_domain(cls, v: str) -> str:
        v = v.strip().lower()
        for prefix in ("https://", "http://", "www."):
            v = v.removeprefix(prefix)
        return v.split("/", 1)[0]

    @field_validator("country")
    @classmethod
    def _upper_country(cls, v: str | None) -> str | None:
        return v.strip().upper() if v else v


class EngagementEvent(BaseModel):
    type: str
    occurred_at: datetime
    contact_id: str | None = None


class ScoreRequest(BaseModel):
    company: Company
    events: list[EngagementEvent] = Field(default_factory=list)
    as_of: datetime | None = Field(
        default=None, description="Reference time for decay; defaults to now (UTC)."
    )


class Contribution(BaseModel):
    signal: str
    input: str | float | int | list[str] | None
    match: float = Field(description="0..1 match for this criterion")
    points: float = Field(description="Points added to the 0-100 fit score")
    max_points: float
    reason: str


Tier = Literal[1, 2, 3, 4]


class ScoreResult(BaseModel):
    domain: str
    fit_score: float
    engagement_score: float
    tier: Tier
    action: str
    disqualified: bool
    disqualifiers: list[str]
    contributions: list[Contribution]
    missing_fields: list[str]
    needs_enrichment: bool
    config_version: str
