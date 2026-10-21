"""Waterfall enrichment with provenance and input gates, email verification, CRM dedupe."""

from enrichment.coverage import (
    CoverageReport,
    MappingFinding,
    MappingReport,
    ProviderCoverage,
    ProviderReadiness,
    Ratio,
    Severity,
    check_mappings,
    coverage,
)
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

__version__ = "0.0.2"

__all__ = [
    "PLACEHOLDERS",
    "Attempt",
    "AttemptStatus",
    "CoverageReport",
    "FakeProvider",
    "LeadRecord",
    "MappingFinding",
    "MappingReport",
    "Provider",
    "ProviderCoverage",
    "ProviderError",
    "ProviderReadiness",
    "ProviderResult",
    "Ratio",
    "Severity",
    "Waterfall",
    "WaterfallOutcome",
    "WaterfallStatus",
    "__version__",
    "check_mappings",
    "clean",
    "coverage",
    "is_reserved_host",
    "select_inputs",
]
