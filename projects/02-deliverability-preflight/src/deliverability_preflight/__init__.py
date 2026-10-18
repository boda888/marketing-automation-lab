"""Offline-testable deliverability preflight checks."""

from .batch import Assignment, BatchPlan, Lead, assign_batch
from .capacity import (
    CapacityPlan,
    CapacityPolicy,
    Mailbox,
    mailbox_capacity,
    plan_capacity,
)
from .complaints import (
    ComplaintReport,
    ComplaintStats,
    check_complaint_rate,
)
from .content import (
    ContentReport,
    Link,
    MessageContent,
    lint_content,
)
from .dkim import (
    DkimKeyError,
    DkimKeyRecord,
    DkimReport,
    check_dkim,
    check_dkim_selectors,
    parse_dkim_key,
    rsa_key_bits,
)
from .dkim_signature import (
    DkimSignature,
    DkimSignatureError,
    DkimSignatureReport,
    check_dkim_signature,
    dkim_signatures,
    parse_dkim_signature,
)
from .dmarc import (
    DmarcRecord,
    DmarcReport,
    DmarcSyntaxError,
    TreeWalkOrgDomain,
    aligned,
    check_alignment,
    check_dmarc,
    parse_dmarc,
    select_org_domain,
    tree_walk_org_domain,
    tree_walk_targets,
)
from .dns import DNSTempError, FakeResolver, Resolver
from .findings import Finding, worst
from .live_dns import DnsPythonResolver
from .preflight import PreflightReport, preflight
from .psl import PublicSuffixList
from .spf import SpfReport, SpfSyntaxError, check_spf, parse_spf
from .unsubscribe import (
    UnsubscribeReport,
    check_one_click,
    dkim_signed_headers,
    headers_from_message,
)

__all__ = [
    "Assignment",
    "BatchPlan",
    "Lead",
    "assign_batch",
    "CapacityPlan",
    "CapacityPolicy",
    "Mailbox",
    "mailbox_capacity",
    "plan_capacity",
    "ComplaintReport",
    "ComplaintStats",
    "check_complaint_rate",
    "ContentReport",
    "Link",
    "MessageContent",
    "lint_content",
    "PreflightReport",
    "PublicSuffixList",
    "preflight",
    "DkimKeyError",
    "DkimKeyRecord",
    "DkimReport",
    "check_dkim",
    "check_dkim_selectors",
    "parse_dkim_key",
    "rsa_key_bits",
    "DkimSignature",
    "DkimSignatureError",
    "DkimSignatureReport",
    "check_dkim_signature",
    "dkim_signatures",
    "parse_dkim_signature",
    "DmarcRecord",
    "DmarcReport",
    "DmarcSyntaxError",
    "TreeWalkOrgDomain",
    "aligned",
    "check_alignment",
    "check_dmarc",
    "parse_dmarc",
    "select_org_domain",
    "tree_walk_org_domain",
    "tree_walk_targets",
    "DNSTempError",
    "DnsPythonResolver",
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
__version__ = "0.0.9"
