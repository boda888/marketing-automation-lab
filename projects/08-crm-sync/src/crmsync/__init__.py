"""CRM sync: one upsert per person key, one created / updated / failed outcome per row."""

from .hubspot import (
    HUBSPOT_BATCH_LIMIT,
    HubSpotBatch,
    parse_upsert_response,
    plan_upserts,
)
from .models import CrmRow, Outcome, RowOutcome, normalise_email

__all__ = [
    "HUBSPOT_BATCH_LIMIT",
    "CrmRow",
    "HubSpotBatch",
    "Outcome",
    "RowOutcome",
    "normalise_email",
    "parse_upsert_response",
    "plan_upserts",
]
