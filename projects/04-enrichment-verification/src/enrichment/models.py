"""Records and results that move through an enrichment waterfall.

Rules the model enforces (see the README for the failure each one prevents):

- Placeholder strings (``"Unknown"``, ``"N/A"``, ``"-"``, empty) are not data: they become
  ``None`` on the way in, so they can never be sent to a paid provider as an input, and a
  provider answering ``"Unknown"`` counts as no answer.
- Synthetic records (reserved test domains such as ``.example``) say so via ``is_fixture``;
  the waterfall never sends them to a paid provider.
- Attempts log which input *fields* were used, never their values.
"""

from __future__ import annotations

from enum import StrEnum
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, field_validator

PLACEHOLDERS = frozenset(
    {"", "unknown", "n/a", "na", "none", "null", "nil", "-", "--", "tbd", "not found", "undefined"}
)

# RFC 2606 / RFC 6761 reserved names: never a real company or mailbox.
_RESERVED_TLDS = (".example", ".test", ".invalid", ".localhost")
_RESERVED_DOMAINS = ("example.com", "example.net", "example.org")


def clean(value: object) -> str | None:
    """Strip a value; placeholders and blanks become ``None``."""
    if value is None:
        return None
    text = str(value).strip()
    return None if text.lower() in PLACEHOLDERS else text


def _host(value: str) -> str | None:
    if " " in value:
        return None
    if "://" in value:
        host = urlsplit(value).hostname
    elif "@" in value:
        host = value.rsplit("@", 1)[1]
    elif "." in value:
        host = value.split("/", 1)[0]
    else:
        return None
    return host.lower().rstrip(".") if host else None


def is_reserved_host(host: str) -> bool:
    host = host.lower().rstrip(".")
    if host in {t.lstrip(".") for t in _RESERVED_TLDS} or host.endswith(_RESERVED_TLDS):
        return True
    return any(host == d or host.endswith("." + d) for d in _RESERVED_DOMAINS)


class LeadRecord(BaseModel):
    """One row to enrich: a stable id plus named fields (``company_domain``, ``email``, ...)."""

    record_id: str = Field(min_length=1, examples=["lead-0001"])
    fields: dict[str, str | None] = Field(default_factory=dict)

    @field_validator("record_id")
    @classmethod
    def _id_not_placeholder(cls, v: str) -> str:
        cleaned = clean(v)
        if cleaned is None:
            raise ValueError(f"record_id must be a real id, not {v!r}")
        return cleaned

    @field_validator("fields", mode="before")
    @classmethod
    def _clean_fields(cls, v: object) -> object:
        if isinstance(v, dict):
            return {str(k).strip(): clean(val) for k, val in v.items()}
        return v

    def get(self, name: str) -> str | None:
        return self.fields.get(name)

    @property
    def is_fixture(self) -> bool:
        """Any field points at a reserved test domain: never send to paid providers."""
        for value in self.fields.values():
            host = _host(value) if value else None
            if host and is_reserved_host(host):
                return True
        return False


class AttemptStatus(StrEnum):
    FOUND = "found"
    REJECTED = "rejected"  # answered, but the value failed the waterfall's acceptance rule
    NOT_FOUND = "not_found"
    ERROR = "error"
    OUTCOME_UNKNOWN = "outcome_unknown"  # timed out: may have been processed and billed
    SKIPPED_MISSING_INPUTS = "skipped_missing_inputs"
    SKIPPED_FIXTURE = "skipped_fixture"
    SKIPPED_BUDGET = "skipped_budget"  # the call could cost more than the record has left


# Attempts that never reached the provider.
SKIPPED = frozenset(
    {
        AttemptStatus.SKIPPED_MISSING_INPUTS,
        AttemptStatus.SKIPPED_FIXTURE,
        AttemptStatus.SKIPPED_BUDGET,
    }
)


class WaterfallStatus(StrEnum):
    FOUND = "found"
    NOT_FOUND = "not_found"  # at least one provider was called, none gave an acceptable value
    NOT_RUN = "not_run"  # the run condition was false (e.g. the field is already filled)
    NO_ELIGIBLE_PROVIDER = "no_eligible_provider"  # every provider was skipped before a call
    BUDGET_SPENT = "budget_spent"  # nothing found, and a provider was skipped for budget
    OUTCOME_UNKNOWN = "outcome_unknown"  # nothing found, and a paid call timed out: inspect first


class ProviderResult(BaseModel):
    """What one provider call returned. ``value=None`` means the provider had no answer."""

    value: str | None = None
    cost: float = Field(default=0.0, ge=0, description="Credits charged for this call")
    provider_status: str | None = Field(
        default=None, description="The provider's own status/label, kept as context only"
    )

    @field_validator("value", mode="before")
    @classmethod
    def _placeholder_is_none(cls, v: object) -> object:
        return clean(v)


class ProviderError(Exception):
    """A provider fault (HTTP 5xx, timeout, bad payload). The waterfall moves on."""

    def __init__(self, message: str, *, cost: float = 0.0) -> None:
        super().__init__(message)
        self.cost = cost


class ProviderTimeout(ProviderError):
    """No answer in time. The provider may still have processed and billed the call, so it
    is recorded as an unknown outcome, never as a free miss that is safe to replay."""


class Attempt(BaseModel):
    provider: str
    status: AttemptStatus
    inputs_used: list[str] = Field(default_factory=list, description="Field names, not values")
    cost: float = 0.0
    detail: str | None = None
    cached: bool = Field(default=False, description="Replayed from the cache: no call, no credits")


class WaterfallOutcome(BaseModel):
    record_id: str
    field: str
    status: WaterfallStatus
    value: str | None = None
    answered_by: str | None = Field(default=None, description="Provider whose value was kept")
    attempts: list[Attempt] = Field(default_factory=list)

    @property
    def cost(self) -> float:
        return sum(a.cost for a in self.attempts)

    @property
    def calls(self) -> int:
        """Provider calls actually made (skips and cache replays excluded)."""
        return sum(a.status not in SKIPPED and not a.cached for a in self.attempts)
