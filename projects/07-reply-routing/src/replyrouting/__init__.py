"""07 reply routing: webhook intake, deterministic classification, cross-channel routing."""

from .classify import classify, own_text
from .dates import ReturnDate, extract_return_date
from .intake import (
    EventKind,
    IntakeConfig,
    IntakeOutcome,
    IntakeResult,
    MemorySeenEvents,
    SqliteSeenEvents,
    VendorEvent,
    classify_event,
    handle,
    intake,
)
from .models import BounceKind, Channel, Classification, InboundReply, ReplyClass
from .routing import Action, ActionKind, RoutingDecision, RoutingPolicy, route

__all__ = [
    "Action",
    "ActionKind",
    "BounceKind",
    "Channel",
    "Classification",
    "EventKind",
    "InboundReply",
    "IntakeConfig",
    "IntakeOutcome",
    "IntakeResult",
    "MemorySeenEvents",
    "ReplyClass",
    "ReturnDate",
    "RoutingDecision",
    "RoutingPolicy",
    "SqliteSeenEvents",
    "VendorEvent",
    "classify",
    "classify_event",
    "extract_return_date",
    "handle",
    "intake",
    "own_text",
    "route",
]

__version__ = "0.0.2"
