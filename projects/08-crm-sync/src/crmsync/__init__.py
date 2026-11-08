"""CRM sync: one upsert per person key, one created / updated / failed outcome per row.

HubSpot contacts batch upsert and Attio assert.
"""

from .attio import AssertPlan, AttioAssert, execute_asserts, parse_assert_response, plan_asserts
from .executor import (
    HttpCall,
    HttpResponse,
    RateBudget,
    TransportError,
    UpsertReport,
    execute_upserts,
    retry_after_seconds,
)
from .hubspot import (
    HUBSPOT_BATCH_LIMIT,
    HubSpotBatch,
    parse_upsert_response,
    plan_upserts,
)
from .models import CrmRow, Outcome, RowOutcome, normalise_email
from .transport import UrllibTransport

__all__ = [
    "AssertPlan",
    "AttioAssert",
    "execute_asserts",
    "parse_assert_response",
    "plan_asserts",
    "retry_after_seconds",
    "HUBSPOT_BATCH_LIMIT",
    "CrmRow",
    "HttpCall",
    "HttpResponse",
    "RateBudget",
    "TransportError",
    "UpsertReport",
    "UrllibTransport",
    "execute_upserts",
    "HubSpotBatch",
    "Outcome",
    "RowOutcome",
    "normalise_email",
    "parse_upsert_response",
    "plan_upserts",
]
