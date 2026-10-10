"""Offline-testable deliverability preflight checks."""

from .dmarc import (
    DmarcRecord,
    DmarcReport,
    DmarcSyntaxError,
    aligned,
    check_alignment,
    check_dmarc,
    parse_dmarc,
)
from .dns import DNSTempError, FakeResolver, Resolver
from .findings import Finding, worst
from .spf import SpfReport, SpfSyntaxError, check_spf, parse_spf

__all__ = [
    "DmarcRecord",
    "DmarcReport",
    "DmarcSyntaxError",
    "aligned",
    "check_alignment",
    "check_dmarc",
    "parse_dmarc",
    "DNSTempError",
    "FakeResolver",
    "Finding",
    "Resolver",
    "SpfReport",
    "SpfSyntaxError",
    "check_spf",
    "parse_spf",
    "worst",
]
__version__ = "0.0.1"
