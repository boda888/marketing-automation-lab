"""CRM sync: one upsert per person key, one created / updated / failed outcome per row."""

from .executor import (
    HttpCall,
    HttpResponse,
    RateBudget,
    TransportError,
    UpsertReport,
    execute_upserts,
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
