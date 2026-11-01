"""Global suppression: people and domains that must never be pushed, whatever the campaign.

Kept separate from campaign dedupe on purpose. "Already in another campaign" is a
campaign-level choice (``DedupeScope``); "unsubscribed", "bounced" or "do not contact"
blocks every campaign, and no dedupe setting can turn it off.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from .models import normalise_email


class SuppressionReason(StrEnum):
    UNSUBSCRIBED = "unsubscribed"
    BOUNCED = "bounced"
    COMPLAINED = "complained"
    DO_NOT_CONTACT = "do_not_contact"
    CUSTOMER = "customer"
    MANUAL = "manual"


@dataclass(frozen=True)
class SuppressionHit:
    reason: SuppressionReason
    matched: str  # the address or domain entry that matched


@dataclass
class SuppressionList:
    """Exact addresses plus whole domains (a domain entry also covers its subdomains)."""

    emails: dict[str, SuppressionReason] = field(default_factory=dict)
    domains: dict[str, SuppressionReason] = field(default_factory=dict)

    def add_email(self, email: str, reason: SuppressionReason) -> None:
        normalised = normalise_email(email)
        if normalised is None:
            raise ValueError(f"not an email address: {email!r}")
        self.emails.setdefault(normalised, reason)

    def add_domain(self, domain: str, reason: SuppressionReason) -> None:
        host = domain.strip().lower().rstrip(".").removeprefix("@")
        if not host or "." not in host or "@" in host:
            raise ValueError(f"not a domain: {domain!r}")
        self.domains.setdefault(host, reason)

    def check(self, email: str | None) -> SuppressionHit | None:
        normalised = normalise_email(email)
        if normalised is None:
            return None
        if normalised in self.emails:
            return SuppressionHit(self.emails[normalised], normalised)
        host = normalised.rsplit("@", 1)[1]
        labels = host.split(".")
        for i in range(len(labels) - 1):
            candidate = ".".join(labels[i:])
            if candidate in self.domains:
                return SuppressionHit(self.domains[candidate], candidate)
        return None

    def __len__(self) -> int:
        return len(self.emails) + len(self.domains)
