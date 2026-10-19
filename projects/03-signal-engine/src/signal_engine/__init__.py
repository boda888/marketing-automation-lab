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

__version__ = "0.0.2"

__all__ = [
    "DatePrecision",
    "DedupeResult",
    "EventDate",
    "MatchReason",
    "MergedSignal",
    "Provenance",
    "SignalEvent",
    "SignalStatus",
    "SignalType",
    "__version__",
    "dedupe",
    "normalise_subject",
]
