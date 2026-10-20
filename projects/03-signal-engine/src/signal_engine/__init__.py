"""Signal engine: evidence-backed buying-signal events for signal-based outbound."""

from signal_engine.dedupe import (
    DedupeResult,
    MatchReason,
    MergedSignal,
    Provenance,
    dedupe,
    normalise_subject,
)
from signal_engine.headcount import (
    Growth,
    HeadcountSeries,
    HeadcountState,
    HeadcountThresholds,
    HeadcountTrend,
    assess_headcount,
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

__version__ = "0.0.4"

__all__ = [
    "DEFAULT_WINDOWS",
    "AccountPriority",
    "Assessment",
    "Band",
    "DatePrecision",
    "DedupeResult",
    "EventDate",
    "Growth",
    "HeadcountSeries",
    "HeadcountState",
    "HeadcountThresholds",
    "HeadcountTrend",
    "MatchReason",
    "MergedSignal",
    "Provenance",
    "SignalEvent",
    "SignalStatus",
    "SignalType",
    "Tier",
    "Window",
    "__version__",
    "assess_headcount",
    "dedupe",
    "normalise_subject",
    "prioritise",
    "prioritise_events",
]
