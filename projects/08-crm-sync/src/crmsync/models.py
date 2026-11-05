"""Rows going into a CRM and the one outcome each row gets back."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum

PLACEHOLDERS = frozenset(
    {"", "unknown", "n/a", "na", "none", "null", "nil", "-", "--", "tbd", "not found", "undefined"}
)
# RFC 2606 / RFC 6761 reserved names: synthetic rows must never reach a real CRM by accident.
_RESERVED_TLDS = (".example", ".test", ".invalid", ".localhost")
_RESERVED_DOMAINS = ("example.com", "example.net", "example.org")
_EMAIL = re.compile(r"^[^@\s]+@([^@\s]+\.[^@\s]+)$")
# HubSpot internal property names are lower-case snake case (e.g. ``lifecyclestage``).
PROPERTY_NAME = re.compile(r"^[a-z][a-z0-9_]{0,99}$")

Scalar = str | int | float | bool | None


def clean(value: object) -> str | None:
    """Stripped text; blanks and placeholder strings (``"Unknown"``, ``"N/A"``) become ``None``."""
    if value is None:
        return None
    text = str(value).strip()
    return None if text.lower() in PLACEHOLDERS else text


def normalise_email(value: object) -> str | None:
    text = clean(value)
    if text is None:
        return None
    text = text.lower()
    return text if _EMAIL.match(text) else None


def is_fixture_email(email: str) -> bool:
    host = email.rsplit("@", 1)[-1].rstrip(".")
    if host.endswith(_RESERVED_TLDS) or host in {t.lstrip(".") for t in _RESERVED_TLDS}:
        return True
    return any(host == d or host.endswith("." + d) for d in _RESERVED_DOMAINS)


def property_value(value: Scalar) -> str | None:
    """CRM property value as the API's string, or ``None`` = do not send (never blank a field).

    Booleans become ``"true"`` / ``"false"``; ``0`` and ``False`` are real values and are kept.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return repr(value) if isinstance(value, float) else str(value)
    return clean(value)


@dataclass(frozen=True)
class CrmRow:
    """One person to write. ``row_id`` is the caller's id (sheet row, Clay row, lead id)."""

    row_id: str
    email: str | None = None
    properties: dict[str, Scalar] = field(default_factory=dict)


class Outcome(StrEnum):
    CREATED = "created"
    UPDATED = "updated"
    # Landed, but the response did not say whether it was new (``_new`` missing).
    UPSERTED = "upserted"
    FAILED = "failed"
    # Never sent: no key, fixture, duplicate key, bad property name.
    SKIPPED = "skipped"


@dataclass(frozen=True)
class RowOutcome:
    row_id: str
    key: str | None
    outcome: Outcome
    crm_id: str | None = None
    reason: str | None = None
    # True only when re-sending the same upsert cannot create a second record.
    retryable: bool = False


def count_outcomes(outcomes: list[RowOutcome]) -> dict[str, int]:
    counts = {o.value: 0 for o in Outcome}
    for item in outcomes:
        counts[item.outcome.value] += 1
    return counts
