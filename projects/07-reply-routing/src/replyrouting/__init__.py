"""07 reply routing: deterministic classification and cross-channel routing of replies."""

from .classify import classify, own_text
from .dates import ReturnDate, extract_return_date
from .models import BounceKind, Channel, Classification, InboundReply, ReplyClass
from .routing import Action, ActionKind, RoutingDecision, RoutingPolicy, route

__all__ = [
    "Action",
    "ActionKind",
    "BounceKind",
    "Channel",
    "Classification",
    "InboundReply",
    "ReplyClass",
    "ReturnDate",
    "RoutingDecision",
    "RoutingPolicy",
    "classify",
    "extract_return_date",
    "own_text",
    "route",
]

__version__ = "0.0.1"
