"""Offline-testable deliverability preflight checks."""

from .dkim import (
    DkimKeyError,
    DkimKeyRecord,
    DkimReport,
    check_dkim,
    check_dkim_selectors,
    parse_dkim_key,
    rsa_key_bits,
)
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
from .unsubscribe import (
    UnsubscribeReport,
    check_one_click,
    dkim_signed_headers,
    headers_from_message,
)

__all__ = [
    "DkimKeyError",
    "DkimKeyRecord",
    "DkimReport",
    "check_dkim",
    "check_dkim_selectors",
    "parse_dkim_key",
    "rsa_key_bits",
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
    "UnsubscribeReport",
    "check_one_click",
    "check_spf",
    "dkim_signed_headers",
    "headers_from_message",
    "parse_spf",
    "worst",
]
__version__ = "0.0.1"
