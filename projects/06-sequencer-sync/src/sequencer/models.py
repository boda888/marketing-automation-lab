"""Lead, campaign and dedupe-scope models shared by every sequencer adapter."""

from __future__ import annotations

import hashlib
import re
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator

PLACEHOLDERS = frozenset(
    {"", "unknown", "n/a", "na", "none", "null", "nil", "-", "--", "tbd", "not found", "undefined"}
)

# RFC 2606 / RFC 6761 reserved names: never a real mailbox.
_RESERVED_TLDS = (".example", ".test", ".invalid", ".localhost")
_RESERVED_DOMAINS = ("example.com", "example.net", "example.org")

_EMAIL = re.compile(r"^[^@\s]+@([^@\s]+\.[^@\s]+)$")
# Merge-tag names every sequencer accepts as a custom variable key.
VARIABLE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")

Scalar = str | int | float | bool | None


def clean(value: object) -> str | None:
    """Strip a value; placeholders and blanks become ``None``."""
    if value is None:
        return None
    text = str(value).strip()
    return None if text.lower() in PLACEHOLDERS else text


def is_reserved_host(host: str) -> bool:
    host = host.lower().rstrip(".")
    if host in {t.lstrip(".") for t in _RESERVED_TLDS} or host.endswith(_RESERVED_TLDS):
        return True
    return any(host == d or host.endswith("." + d) for d in _RESERVED_DOMAINS)


def normalise_email(value: object) -> str | None:
    """Lower-cased, stripped address, or ``None`` for blanks, placeholders and non-addresses."""
    text = clean(value)
    if text is None:
        return None
    text = text.lower()
    return text if _EMAIL.match(text) else None


class DedupeScope(StrEnum):
    """Where an existing lead blocks a new push. Always chosen by the caller, never defaulted.

    ``campaign``: only a lead already in *this* campaign is skipped.
    ``all_campaigns``: a lead in any campaign of the account is skipped.
    ``workspace``: a lead anywhere in the account (campaigns and lead lists) is skipped.
    """

    CAMPAIGN = "campaign"
    ALL_CAMPAIGNS = "all_campaigns"
    WORKSPACE = "workspace"


_SCOPE_RANK = {DedupeScope.CAMPAIGN: 0, DedupeScope.ALL_CAMPAIGNS: 1, DedupeScope.WORKSPACE: 2}


def stricter_or_equal(a: DedupeScope, b: DedupeScope) -> bool:
    """``a`` skips at least every lead that ``b`` skips."""
    return _SCOPE_RANK[a] >= _SCOPE_RANK[b]


class SequencerLead(BaseModel):
    """One person to push. ``email`` is the person key; a company is never a key."""

    model_config = ConfigDict(frozen=True)

    lead_id: str = Field(min_length=1, description="Stable id from the upstream pipeline")
    email: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    company_name: str | None = None
    website: str | None = None
    linkedin_url: str | None = None
    custom_variables: dict[str, Scalar] = Field(default_factory=dict)

    @field_validator("email", mode="before")
    @classmethod
    def _email(cls, value: object) -> str | None:
        return normalise_email(value)

    @field_validator(
        "first_name", "last_name", "company_name", "website", "linkedin_url", mode="before"
    )
    @classmethod
    def _text(cls, value: object) -> str | None:
        return clean(value)

    @field_validator("custom_variables", mode="before")
    @classmethod
    def _variables(cls, value: object) -> dict[str, Scalar]:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValueError("custom_variables must be an object")
        out: dict[str, Scalar] = {}
        for key, raw in value.items():
            if not isinstance(key, str) or not VARIABLE_NAME.match(key):
                raise ValueError(f"invalid custom variable name {key!r}")
            if isinstance(raw, dict | list | tuple | set):
                raise ValueError(f"custom variable {key!r} must be a scalar")
            out[key] = clean(raw) if isinstance(raw, str) else raw
        return out

    @property
    def is_fixture(self) -> bool:
        """The address sits on a reserved test domain: never push it to a real account."""
        if self.email is None:
            return False
        return is_reserved_host(self.email.rsplit("@", 1)[1])

    def missing_variables(self, required: frozenset[str] | set[str]) -> list[str]:
        """Required merge tags that are absent or blank for this lead (sorted)."""
        return sorted(k for k in required if self.custom_variables.get(k) in (None, ""))


class Campaign(BaseModel):
    """A campaign as listed by the sequencer: vendor id plus display name."""

    model_config = ConfigDict(frozen=True)

    campaign_id: str = Field(min_length=1)
    name: str
    status: str | None = None


def idempotency_key(vendor: str, campaign_id: str, email: str) -> str:
    """Stable key for "this person was pushed to this campaign of this vendor"."""
    raw = f"{vendor.lower()}\x1f{campaign_id}\x1f{email.lower()}"
    return hashlib.sha256(raw.encode()).hexdigest()
