"""Webhook intake: authenticate, normalise and dedupe one vendor event before classification.

Order (each step can end the event; nothing is dropped without an outcome and a reason):

1. shape      the body must be a JSON object                          -> ``invalid``
2. auth       lemlist: ``secret`` echoed in the body, compared in constant time, then stripped
              (the hook is created with ``POST /hooks {targetUrl, secret}``); Smartlead and
              Instantly send no secret or signature, so the webhook URL carries a long random
              path token per vendor. Not configured = rejected (fail closed). -> ``rejected``
3. type       vendor event name -> kind. Both Smartlead spellings (``EMAIL_REPLY`` from the
              Activepieces piece, ``EMAIL_REPLIED`` from a community n8n node) and all five
              lemlist ``*Unsubscribed`` types are mapped; an unknown name is an ``alert``,
              never a silent fallback branch.
4. key        per-vendor event key: lemlist activity ``_id``; Smartlead and Instantly send no
              event id, so the key is derived from campaign + lead + kind + timestamp. The
              kind (not the raw name) is in the key, so a replay under the other spelling is
              still a duplicate.                                       -> ``invalid``
5. age        older than ``max_age`` (default 24 h, as the lemlist Activepieces piece) ->
              ``stale``: sent to review, not dropped, because a replay after an outage
              (Smartlead ``retrigger-failed-events``, Instantly ``/resume``) is legitimately old.
6. dedupe     first sighting of the key wins (``SeenEvents.add``)    -> ``duplicate``
7. outcome    reply with text -> ``accepted``; reply without text -> ``needs_body`` (fetch it
              from the inbox API, never classify an empty body); vendor unsubscribe / bounce
              -> ``accepted`` with a direct class; label changes -> ``label_only`` (context);
              sender-health events -> ``alert``; sends / opens / clicks -> ``ignored``.

Payload field names come from integration packages, not vendor docs (vendor doc sites were
unreachable from the run environment, research R4 2026-11-05 and 2026-11-13). Fields that no
opened sample shows (reply text for every vendor, Instantly's lead e-mail) are looked up from
the candidate lists in ``FIELDS``; send one real event per vendor and check them before use.
"""

from __future__ import annotations

import hmac
import sqlite3
import threading
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol

from .classify import classify
from .models import BounceKind, Channel, Classification, InboundReply, ReplyClass
from .routing import RoutingDecision, RoutingPolicy, route


class EventKind(StrEnum):
    REPLY = "reply"
    BOUNCE = "bounce"
    UNSUBSCRIBE = "unsubscribe"
    LABEL = "label"  # vendor's interest / category label: context, never a class
    SENDER_ALERT = "sender_alert"  # mailbox / account health, not about a lead
    IGNORED = "ignored"  # sends, opens, clicks


class IntakeOutcome(StrEnum):
    ACCEPTED = "accepted"
    NEEDS_BODY = "needs_body"
    LABEL_ONLY = "label_only"
    IGNORED = "ignored"
    DUPLICATE = "duplicate"
    STALE = "stale"
    ALERT = "alert"
    REJECTED = "rejected"
    INVALID = "invalid"


SMARTLEAD_EVENTS: dict[str, EventKind] = {
    "EMAIL_REPLY": EventKind.REPLY,
    "EMAIL_REPLIED": EventKind.REPLY,
    "LEAD_UNSUBSCRIBED": EventKind.UNSUBSCRIBE,
    "EMAIL_UNSUBSCRIBED": EventKind.UNSUBSCRIBE,
    "EMAIL_BOUNCED": EventKind.BOUNCE,
    "LEAD_CATEGORY_UPDATED": EventKind.LABEL,
    "EMAIL_SENT": EventKind.IGNORED,
    "EMAIL_OPEN": EventKind.IGNORED,
    "EMAIL_OPENED": EventKind.IGNORED,
    "EMAIL_LINK_CLICK": EventKind.IGNORED,
    "EMAIL_CLICKED": EventKind.IGNORED,
}

INSTANTLY_EVENTS: dict[str, EventKind] = {
    "reply_received": EventKind.REPLY,
    "email_bounced": EventKind.BOUNCE,
    "email_unsubscribed": EventKind.UNSUBSCRIBE,
    "lead_interested": EventKind.LABEL,
    "email_sent": EventKind.IGNORED,
    "email_opened": EventKind.IGNORED,
    "link_clicked": EventKind.IGNORED,
    "campaign_completed": EventKind.IGNORED,
}

LEMLIST_UNSUBSCRIBE_TYPES = (
    "emailsUnsubscribed",
    "entityUnsubscribed",
    "variableUnsubscribed",
    "apiUnsubscribed",
    "manualUnsubscribed",
)

LEMLIST_EVENTS: dict[str, EventKind] = {
    "emailsReplied": EventKind.REPLY,
    "linkedinReplied": EventKind.REPLY,
    "whatsappReplied": EventKind.REPLY,
    "smsReplied": EventKind.REPLY,
    "emailsBounced": EventKind.BOUNCE,
    **dict.fromkeys(LEMLIST_UNSUBSCRIBE_TYPES, EventKind.UNSUBSCRIBE),
    "emailsInterested": EventKind.LABEL,
    "apiInterested": EventKind.LABEL,
    "manualNotInterested": EventKind.LABEL,
    "aircallInterested": EventKind.LABEL,
    "sendLimitReached": EventKind.SENDER_ALERT,
    "connectionIssue": EventKind.SENDER_ALERT,
    "customDomainErrors": EventKind.SENDER_ALERT,
    "lemwarmPaused": EventKind.SENDER_ALERT,
    "apiFailed": EventKind.SENDER_ALERT,
    "linkedinInviteAccepted": EventKind.IGNORED,
    "aircallEnded": EventKind.IGNORED,
}

EVENT_MAPS: dict[str, dict[str, EventKind]] = {
    "smartlead": SMARTLEAD_EVENTS,
    "instantly": INSTANTLY_EVENTS,
    "lemlist": LEMLIST_EVENTS,
}

# Event names: only those seen in an opened package or CLI (R4 2026-10-25 / 2026-11-05); any
# other name is an ``alert`` until someone adds it here from a real delivery.

# Candidate payload paths (dotted), first non-empty wins. "confirmed" = seen in an opened
# integration sample (R4 2026-11-05); the rest are guesses to check against a real event.
FIELDS: dict[str, dict[str, tuple[str, ...]]] = {
    "smartlead": {
        "event_type": ("event_type",),  # confirmed
        "campaign": ("campaign_id",),  # confirmed
        "lead": ("sl_email_lead_id",),  # confirmed
        "email": ("to_email", "sl_lead_email"),  # to_email confirmed (the lead)
        "timestamp": ("event_timestamp",),  # confirmed
        "subject": ("subject",),  # confirmed
        "body": ("reply_message.text", "reply_body", "preview_text"),  # unconfirmed
        "label": ("lead_category.new_name", "category"),  # unconfirmed
    },
    "instantly": {
        "event_type": ("event_type",),  # confirmed
        "campaign": ("campaign_id",),  # confirmed
        "email": ("lead_email", "email"),  # unconfirmed
        "timestamp": ("timestamp",),  # confirmed
        "subject": ("reply_subject", "subject"),  # unconfirmed
        "body": ("reply_text", "reply_text_snippet", "body.text"),  # unconfirmed
        "label": ("event_type",),
    },
    "lemlist": {
        "event_type": ("type",),  # confirmed
        "id": ("_id",),  # confirmed
        "campaign": ("campaignId",),  # confirmed
        "email": ("leadEmail",),  # confirmed
        "linkedin": ("linkedinUrl", "leadLinkedinUrl"),  # unconfirmed
        "timestamp": ("createdAt",),  # confirmed
        "subject": ("subject",),  # confirmed
        "body": ("text", "messagePreview", "message"),  # unconfirmed
        "label": ("type",),
    },
}


@dataclass(frozen=True)
class IntakeConfig:
    lemlist_secret: str | None = None
    path_tokens: Mapping[str, str] = field(default_factory=dict)  # vendor -> URL token
    max_age: timedelta = timedelta(hours=24)
    max_future_skew: timedelta = timedelta(minutes=5)


@dataclass(frozen=True)
class VendorEvent:
    vendor: str
    event_type: str
    kind: EventKind
    event_key: str | None = None
    occurred_at: datetime | None = None
    channel: Channel | None = Channel.EMAIL
    person_email: str | None = None
    person_linkedin_url: str | None = None
    campaign_id: str | None = None
    subject: str = ""
    body_text: str = ""
    vendor_label: str | None = None
    payload: Mapping[str, Any] = field(default_factory=dict)  # secret already stripped


@dataclass(frozen=True)
class IntakeResult:
    outcome: IntakeOutcome
    reason: str
    event: VendorEvent | None = None


class SeenEvents(Protocol):
    def add(self, key: str) -> bool:
        """Record ``key``; True only for its first sighting."""

    def release(self, key: str) -> None:
        """Forget ``key`` so a replay is processed again (call when downstream failed)."""


class MemorySeenEvents:
    """In-process store; keeps the newest ``max_keys`` keys (None = unbounded)."""

    def __init__(self, max_keys: int | None = None) -> None:
        self._keys: OrderedDict[str, None] = OrderedDict()
        self._max = max_keys
        self._lock = threading.Lock()

    def add(self, key: str) -> bool:
        with self._lock:
            if key in self._keys:
                return False
            self._keys[key] = None
            if self._max is not None and len(self._keys) > self._max:
                self._keys.popitem(last=False)
            return True

    def release(self, key: str) -> None:
        with self._lock:
            self._keys.pop(key, None)


class SqliteSeenEvents:
    """Key store shared by every process that opens the same file (unique key + INSERT OR
    IGNORE, so two workers receiving the same replay cannot both win)."""

    def __init__(self, path: str) -> None:
        self._path = path
        with self._connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS seen_events "
                "(event_key TEXT PRIMARY KEY, first_seen TEXT NOT NULL)"
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._path, timeout=30)

    def add(self, key: str) -> bool:
        db = self._connect()
        try:
            with db:
                cur = db.execute(
                    "INSERT OR IGNORE INTO seen_events (event_key, first_seen) VALUES (?, ?)",
                    (key, datetime.now(UTC).isoformat()),
                )
            return cur.rowcount == 1
        finally:
            db.close()

    def release(self, key: str) -> None:
        db = self._connect()
        try:
            with db:
                db.execute("DELETE FROM seen_events WHERE event_key = ?", (key,))
        finally:
            db.close()


def _get(payload: Mapping[str, Any], path: str) -> Any:
    value: Any = payload
    for part in path.split("."):
        if not isinstance(value, Mapping) or part not in value:
            return None
        value = value[part]
    return value


def _first(payload: Mapping[str, Any], vendor: str, name: str) -> str | None:
    for path in FIELDS[vendor].get(name, ()):
        value = _get(payload, path)
        if value is None or isinstance(value, Mapping | list | bool):
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def parse_timestamp(value: Any) -> datetime | None:
    """ISO 8601 (``Z`` allowed) or epoch seconds / milliseconds. Naive ISO -> UTC."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float):
        seconds = value / 1000 if value > 10**11 else value
        try:
            return datetime.fromtimestamp(seconds, UTC)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        return parse_timestamp(int(text))
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _iso(ts: datetime) -> str:
    return ts.astimezone(UTC).isoformat(timespec="milliseconds")


def _authenticate(
    vendor: str, payload: dict[str, Any], path_token: str | None, config: IntakeConfig
) -> str | None:
    """Return a rejection reason, or None when authentic. Strips lemlist's secret."""
    if vendor == "lemlist":
        expected = config.lemlist_secret
        supplied = payload.pop("secret", None)
        if not expected:
            return "lemlist secret not configured: refusing unauthenticated events"
        if not isinstance(supplied, str) or not hmac.compare_digest(
            supplied.encode(), expected.encode()
        ):
            return "lemlist secret missing or wrong"
        return None
    expected = config.path_tokens.get(vendor)
    if not expected:
        return f"{vendor} path token not configured: refusing unauthenticated events"
    if not path_token or not hmac.compare_digest(path_token.encode(), expected.encode()):
        return f"{vendor} path token missing or wrong"
    return None


def _event_key(vendor: str, payload: Mapping[str, Any], kind: EventKind, ts: datetime) -> str:
    if vendor == "lemlist":
        activity = _first(payload, vendor, "id")
        return f"lemlist:{activity}" if activity else ""
    campaign = _first(payload, vendor, "campaign")
    if vendor == "smartlead":
        lead = _first(payload, vendor, "lead")
    else:
        lead = (_first(payload, vendor, "email") or "").lower() or None
    if not campaign or not lead:
        return ""
    return f"{vendor}:{campaign}:{lead}:{kind}:{_iso(ts)}"


def intake(
    vendor: str,
    body: Any,
    *,
    now: datetime,
    config: IntakeConfig,
    seen: SeenEvents,
    path_token: str | None = None,
) -> IntakeResult:
    vendor = vendor.strip().lower()
    if vendor not in EVENT_MAPS:
        return IntakeResult(IntakeOutcome.INVALID, f"unknown vendor {vendor!r}")
    if not isinstance(body, Mapping):
        return IntakeResult(IntakeOutcome.INVALID, "body is not a JSON object")
    payload = dict(body)

    rejected = _authenticate(vendor, payload, path_token, config)
    if rejected:
        return IntakeResult(IntakeOutcome.REJECTED, rejected)

    event_type = _first(payload, vendor, "event_type") or ""
    kind = EVENT_MAPS[vendor].get(event_type)
    if kind is None:
        return IntakeResult(
            IntakeOutcome.ALERT,
            f"unknown {vendor} event type {event_type!r}: add it to the event map",
        )
    if kind is EventKind.IGNORED:
        return IntakeResult(IntakeOutcome.IGNORED, f"{vendor} {event_type} is not a reply event")

    occurred = parse_timestamp(_get(payload, FIELDS[vendor]["timestamp"][0]))
    if occurred is None:
        return IntakeResult(IntakeOutcome.INVALID, f"{vendor} {event_type}: no readable timestamp")
    key = _event_key(vendor, payload, kind, occurred)
    if not key:
        return IntakeResult(
            IntakeOutcome.INVALID, f"{vendor} {event_type}: fields for the event key missing"
        )

    channel: Channel | None = Channel.EMAIL
    if event_type == "linkedinReplied":
        channel = Channel.LINKEDIN
    elif event_type in ("whatsappReplied", "smsReplied"):
        channel = None
    email = _first(payload, vendor, "email")
    event = VendorEvent(
        vendor=vendor,
        event_type=event_type,
        kind=kind,
        event_key=key,
        occurred_at=occurred,
        channel=channel,
        person_email=email.lower() if email else None,
        person_linkedin_url=_first(payload, vendor, "linkedin"),
        campaign_id=_first(payload, vendor, "campaign"),
        subject=_first(payload, vendor, "subject") or "",
        body_text=_first(payload, vendor, "body") or "",
        vendor_label=_first(payload, vendor, "label") if kind is EventKind.LABEL else None,
        payload=payload,
    )

    age = now - occurred
    if age > config.max_age:
        return IntakeResult(
            IntakeOutcome.STALE,
            f"event is {age} old (> {config.max_age}): review it, it may be a replay",
            event,
        )
    if -age > config.max_future_skew:
        return IntakeResult(IntakeOutcome.STALE, "event timestamp is in the future", event)

    about_a_person = kind in (EventKind.REPLY, EventKind.BOUNCE, EventKind.UNSUBSCRIBE)
    if about_a_person and event.person_email is None and event.person_linkedin_url is None:
        return IntakeResult(IntakeOutcome.INVALID, "no person key (email / LinkedIn URL)", event)

    if not seen.add(key):
        return IntakeResult(IntakeOutcome.DUPLICATE, f"event {key} already processed", event)

    if kind is EventKind.SENDER_ALERT:
        return IntakeResult(IntakeOutcome.ALERT, f"sender health: {vendor} {event_type}", event)
    if kind is EventKind.LABEL:
        return IntakeResult(
            IntakeOutcome.LABEL_ONLY,
            "vendor label is context only: the lab classifies the reply message itself",
            event,
        )
    if kind is EventKind.REPLY:
        if channel is None:
            return IntakeResult(
                IntakeOutcome.ALERT, f"{event_type}: channel not supported, route by hand", event
            )
        if not event.body_text:
            return IntakeResult(
                IntakeOutcome.NEEDS_BODY,
                "reply text not in the payload: fetch it from the inbox API, then classify",
                event,
            )
    return IntakeResult(IntakeOutcome.ACCEPTED, f"{vendor} {event_type}", event)


def to_reply(event: VendorEvent, body_text: str | None = None) -> InboundReply:
    """The deduped event as the classifier's input; ``body_text`` fills a ``needs_body`` event."""
    return InboundReply(
        channel=event.channel or Channel.EMAIL,
        received_at=event.occurred_at or datetime.now(UTC),
        body_text=body_text if body_text is not None else event.body_text,
        subject=event.subject,
        vendor=event.vendor,
        person_email=event.person_email,
        person_linkedin_url=event.person_linkedin_url,
    )


def classify_event(event: VendorEvent, body_text: str | None = None) -> Classification:
    """Vendor unsubscribe / bounce events are decided by the event itself; replies by text."""
    if event.kind is EventKind.UNSUBSCRIBE:
        return Classification(
            ReplyClass.UNSUBSCRIBE, rule=f"vendor_event:{event.event_type}", evidence=event.vendor
        )
    if event.kind is EventKind.BOUNCE:
        # The vendor says "bounced" but not whether it was permanent: unknown, never hard.
        return Classification(
            ReplyClass.BOUNCE,
            rule=f"vendor_event:{event.event_type}",
            evidence=event.vendor,
            bounce_kind=BounceKind.UNKNOWN,
        )
    if event.kind is EventKind.REPLY:
        return classify(to_reply(event, body_text))
    raise ValueError(f"{event.kind} events are not classified")


def handle(
    result: IntakeResult,
    policy: RoutingPolicy | None = None,
    body_text: str | None = None,
) -> RoutingDecision | None:
    """intake -> classify -> route for accepted events (and ``needs_body`` once the text is
    supplied); None for every other outcome, which the caller routes by ``outcome``."""
    event = result.event
    if event is None:
        return None
    filled = result.outcome is IntakeOutcome.NEEDS_BODY and bool(body_text)
    if result.outcome is not IntakeOutcome.ACCEPTED and not filled:
        return None
    reply = to_reply(event, body_text)
    return route(reply, classify_event(event, body_text), policy or RoutingPolicy())
