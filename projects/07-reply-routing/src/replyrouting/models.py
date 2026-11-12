"""Inbound reply events and the classification vocabulary.

A reply arrives from an email sequencer (Smartlead / Instantly / lemlist) or a LinkedIn tool
(HeyReach, ...). The vendor's own label ("Interested", Instantly interest code 0 = Out of
Office) is kept as *context* only: the lab classifies from the message itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum


class Channel(StrEnum):
    EMAIL = "email"
    LINKEDIN = "linkedin"


class ReplyClass(StrEnum):
    BOUNCE = "bounce"
    OUT_OF_OFFICE = "out_of_office"
    UNSUBSCRIBE = "unsubscribe"
    WRONG_PERSON = "wrong_person"
    REFERRAL = "referral"
    NO_MEANINGFUL_REPLY = "no_meaningful_reply"
    # Intent classes the deterministic stage never assigns: they need the (schema-validated)
    # LLM stage. They exist here so both stages share one vocabulary.
    INTERESTED = "interested"
    QUESTION = "question"
    OBJECTION = "objection"
    NOT_INTERESTED = "not_interested"
    UNCLASSIFIED = "unclassified"


class BounceKind(StrEnum):
    HARD = "hard"  # 5.x.x permanent failure
    SOFT = "soft"  # 4.x.x transient failure
    UNKNOWN = "unknown"  # a DSN without a readable status: never promoted to hard


@dataclass(frozen=True)
class InboundReply:
    """One inbound message, already deduped at intake.

    ``headers`` keys are matched case-insensitively. ``body_text`` is the plain-text body
    including any quoted thread; the classifier strips the quote itself.
    """

    channel: Channel
    received_at: datetime
    body_text: str = ""
    subject: str = ""
    from_address: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    vendor: str = ""
    vendor_label: str | None = None
    person_email: str | None = None
    person_linkedin_url: str | None = None

    def header(self, name: str) -> str | None:
        wanted = name.lower()
        for key, value in self.headers.items():
            if key.lower() == wanted:
                return value
        return None


@dataclass(frozen=True)
class Classification:
    reply_class: ReplyClass
    rule: str
    evidence: str = ""
    needs_llm: bool = False
    auto_generated: bool = False
    bounce_kind: BounceKind | None = None
    bounce_status: str | None = None
    return_date: date | None = None
    return_date_note: str | None = None
    referral_email: str | None = None
    vendor_label: str | None = None
    vendor_label_disagrees: bool = False
