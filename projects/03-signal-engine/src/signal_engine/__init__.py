"""Signal engine: evidence-backed buying-signal events for signal-based outbound."""

from signal_engine.dedupe import (
    DedupeResult,
    MatchReason,
    MergedSignal,
    Provenance,
    dedupe,
    normalise_subject,
)
from signal_engine.models import (
    DatePrecision,
    EventDate,
    SignalEvent,
    SignalStatus,
    SignalType,
)
from signal_engine.priority import (
    DEFAULT_WINDOWS,
    AccountPriority,
    Assessment,
    Band,
    Tier,
    Window,
    prioritise,
    prioritise_events,
)

__version__ = "0.0.3"

__all__ = [
    "DEFAULT_WINDOWS",
    "AccountPriority",
    "Assessment",
    "Band",
    "DatePrecision",
    "DedupeResult",
    "EventDate",
    "MatchReason",
    "MergedSignal",
    "Provenance",
    "SignalEvent",
    "SignalStatus",
    "SignalType",
    "Tier",
    "Window",
    "__version__",
    "dedupe",
    "normalise_subject",
    "prioritise",
    "prioritise_events",
]
