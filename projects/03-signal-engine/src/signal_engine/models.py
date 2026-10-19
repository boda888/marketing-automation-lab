"""Signal events: one researched fact about one company, with its evidence.

Rules the model enforces (see the README for the failure each one prevents):

- A ``found`` event carries the URL that supports *that* claim (not a bare homepage),
  a quote/summary of the evidence and the time we observed it.
- ``not_found_in_sample`` and ``unknown`` are first-class states, never ``0`` or ``False``:
  "we searched the careers page and saw no openings" is not the same as "we could not check".
- The date an event happened is kept with its precision (day / month / year), so recency
  windows are computed in code over the whole interval the date can mean.
"""

from __future__ import annotations

import calendar
import re
from datetime import date, datetime
from enum import StrEnum
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, field_validator, model_validator


class SignalType(StrEnum):
    HIRING = "hiring"
    FUNDING = "funding"
    EXPANSION = "expansion"
    LEADERSHIP = "leadership"
    TECH_CHANGE = "tech_change"
    SITE_VISIT = "site_visit"


class SignalStatus(StrEnum):
    FOUND = "found"
    NOT_FOUND_IN_SAMPLE = "not_found_in_sample"
    UNKNOWN = "unknown"


class DatePrecision(StrEnum):
    DAY = "day"
    MONTH = "month"
    YEAR = "year"


# ISO 8601 calendar dates at reduced precision: YYYY, YYYY-MM, YYYY-MM-DD
# (schema.org `Date`, e.g. JobPosting.datePosted, uses ISO 8601 dates).
_ISO_DATE = re.compile(r"^(\d{4})(?:-(\d{2})(?:-(\d{2}))?)?$")


class EventDate(BaseModel):
    """When an event happened, as precisely as the source states it.

    ``EventDate.parse("2026-03")`` means "some day in March 2026": ``earliest`` is
    2026-03-01, ``latest`` 2026-03-31. Never pad a month to the 1st and treat it as a day.
    """

    model_config = {"frozen": True}

    value: date = Field(description="First day of the interval the source's date covers")
    precision: DatePrecision

    @classmethod
    def parse(cls, text: str) -> EventDate:
        m = _ISO_DATE.match(text.strip())
        if not m:
            raise ValueError(f"not an ISO 8601 date (YYYY, YYYY-MM or YYYY-MM-DD): {text!r}")
        year, month, day = m.groups()
        if day is not None:
            return cls(value=date(int(year), int(month), int(day)), precision=DatePrecision.DAY)
        if month is not None:
            return cls(value=date(int(year), int(month), 1), precision=DatePrecision.MONTH)
        return cls(value=date(int(year), 1, 1), precision=DatePrecision.YEAR)

    @model_validator(mode="after")
    def _value_starts_interval(self) -> EventDate:
        v = self.value
        if self.precision is DatePrecision.MONTH and v.day != 1:
            raise ValueError("month precision: value must be the 1st of the month")
        if self.precision is DatePrecision.YEAR and (v.month, v.day) != (1, 1):
            raise ValueError("year precision: value must be January 1st")
        return self

    @property
    def earliest(self) -> date:
        return self.value

    @property
    def latest(self) -> date:
        v = self.value
        if self.precision is DatePrecision.DAY:
            return v
        if self.precision is DatePrecision.MONTH:
            return v.replace(day=calendar.monthrange(v.year, v.month)[1])
        return v.replace(month=12, day=31)

    def age_days(self, as_of: date) -> tuple[int, int]:
        """(youngest, oldest) possible age in days on ``as_of``; youngest is never < 0."""
        return max(0, (as_of - self.latest).days), max(0, (as_of - self.earliest).days)

    def __str__(self) -> str:
        v = self.value
        if self.precision is DatePrecision.DAY:
            return v.isoformat()
        if self.precision is DatePrecision.MONTH:
            return f"{v.year:04d}-{v.month:02d}"
        return f"{v.year:04d}"


def _is_bare_homepage(url: str) -> bool:
    parts = urlsplit(url)
    return parts.path in ("", "/") and not parts.query and not parts.fragment


class SignalEvent(BaseModel):
    """One signal check for one company: found (with evidence), not found, or unknown."""

    company_domain: str = Field(min_length=3, examples=["northwind.example"])
    type: SignalType
    status: SignalStatus
    observed_at: datetime = Field(description="When the source was read (timezone-aware)")
    source_url: str | None = Field(
        default=None, description="Page supporting this specific claim; required when found"
    )
    source_name: str | None = Field(
        default=None, examples=["careers_page", "press_release", "crunchbase"]
    )
    source_event_id: str | None = Field(
        default=None,
        description="The source's own id for the event (job id, round id); unique per source only",
    )
    subject: str | None = Field(
        default=None,
        max_length=200,
        description="What the event is about, for cross-source dedupe",
        examples=["Series A", "Senior Data Engineer", "Lisbon office"],
    )
    occurred_on: EventDate | None = Field(
        default=None, description="When the event happened, with precision; None = not stated"
    )
    evidence: str | None = Field(
        default=None, max_length=500, description="Quote or close paraphrase of the source"
    )
    sample: str | None = Field(
        default=None,
        description="What was searched; required for not_found_in_sample",
        examples=["careers page + 2 job boards, last 90 days"],
    )

    @field_validator("company_domain")
    @classmethod
    def _clean_domain(cls, v: str) -> str:
        v = v.strip().lower()
        for prefix in ("https://", "http://", "www."):
            v = v.removeprefix(prefix)
        return v.split("/", 1)[0].rstrip(".")

    @field_validator("occurred_on", mode="before")
    @classmethod
    def _parse_date_string(cls, v: object) -> object:
        return EventDate.parse(v) if isinstance(v, str) else v

    @field_validator("observed_at")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None or v.utcoffset() is None:
            raise ValueError("observed_at must be timezone-aware")
        return v

    @field_validator("source_url")
    @classmethod
    def _http_url(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        parts = urlsplit(v)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError(f"source_url must be an absolute http(s) URL: {v!r}")
        return v

    @field_validator("evidence", "sample", "source_name", "source_event_id", "subject")
    @classmethod
    def _blank_is_none(cls, v: str | None) -> str | None:
        # Placeholder strings are not data: blank stays blank (PRACTICE: "Unknown" fed to a
        # paid provider). Status carries "unknown", text fields never do.
        if v is None:
            return None
        v = v.strip()
        if v.lower() in ("", "unknown", "n/a", "na", "none", "null", "-"):
            return None
        return v

    @model_validator(mode="after")
    def _status_rules(self) -> SignalEvent:
        if self.status is SignalStatus.FOUND:
            if not self.source_url:
                raise ValueError("found: source_url is required")
            if _is_bare_homepage(self.source_url):
                raise ValueError("found: source_url must point at the evidence, not a homepage")
            if not self.evidence:
                raise ValueError("found: evidence is required")
            if self.occurred_on and self.occurred_on.earliest > self.observed_at.date():
                raise ValueError("found: occurred_on is after observed_at")
            if self.sample:
                raise ValueError("found: sample is only for not_found_in_sample")
        else:
            if self.evidence or self.occurred_on or self.subject:
                raise ValueError(
                    f"{self.status}: evidence/occurred_on/subject only belong to found"
                )
            if self.status is SignalStatus.NOT_FOUND_IN_SAMPLE and not self.sample:
                raise ValueError("not_found_in_sample: sample (what was searched) is required")
        return self

    @property
    def is_found(self) -> bool:
        return self.status is SignalStatus.FOUND

    @property
    def is_fixture(self) -> bool:
        """Synthetic record (``.example`` domain): never send to paid providers."""
        return self.company_domain == "example" or self.company_domain.endswith(".example")
