"""Waterfall enrichment with provenance and input gates, email verification, CRM dedupe."""

from enrichment.fakes import FakeProvider
from enrichment.models import (
    PLACEHOLDERS,
    Attempt,
    AttemptStatus,
    LeadRecord,
    ProviderError,
    ProviderResult,
    WaterfallOutcome,
    WaterfallStatus,
    clean,
    is_reserved_host,
)
from enrichment.waterfall import Provider, Waterfall, select_inputs

__version__ = "0.0.1"

__all__ = [
    "PLACEHOLDERS",
    "Attempt",
    "AttemptStatus",
    "FakeProvider",
    "LeadRecord",
    "Provider",
    "ProviderError",
    "ProviderResult",
    "Waterfall",
    "WaterfallOutcome",
    "WaterfallStatus",
    "__version__",
    "clean",
    "is_reserved_host",
    "select_inputs",
]
