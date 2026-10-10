"""Offline-testable deliverability preflight checks."""

from .dns import DNSTempError, FakeResolver, Resolver
from .findings import Finding, worst
from .spf import SpfReport, SpfSyntaxError, check_spf, parse_spf

__all__ = [
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
