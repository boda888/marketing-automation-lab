"""CRM sync: one upsert per person key, one created / updated / failed outcome per row.

HubSpot contacts batch upsert, Attio assert, Pipedrive cached search + update / create,
merge webhooks that re-point stored ids, and origin checks for those webhooks.
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
from .idmap import (
    IdMap,
    IdMapPersonCache,
    MemoryIdMap,
    MergeEvent,
    MergeReport,
    apply_merges,
    parse_attio_merges,
    parse_hubspot_merges,
    parse_pipedrive_merge,
)
from .models import CrmRow, Outcome, RowOutcome, normalise_email
from .pipedrive import (
    MemoryPersonCache,
    PersonPlan,
    PersonReport,
    execute_persons,
    plan_persons,
)
from .sync import CRMS, WriteOptions, WriteResult, idmap_namespace, write
from .transport import UrllibTransport
from .webhook_auth import (
    AuthResult,
    WebhookSecrets,
    hubspot_legacy_signature,
    hubspot_v3_signature,
    verify_attio,
    verify_basic_auth,
    verify_hubspot_v3,
    verify_webhook,
)

__all__ = [
    "CRMS",
    "WriteOptions",
    "WriteResult",
    "idmap_namespace",
    "write",
    "AuthResult",
    "WebhookSecrets",
    "hubspot_legacy_signature",
    "hubspot_v3_signature",
    "verify_attio",
    "verify_basic_auth",
    "verify_hubspot_v3",
    "verify_webhook",
    "IdMap",
    "IdMapPersonCache",
    "MemoryIdMap",
    "MergeEvent",
    "MergeReport",
    "apply_merges",
    "parse_attio_merges",
    "parse_hubspot_merges",
    "parse_pipedrive_merge",
    "MemoryPersonCache",
    "PersonPlan",
    "PersonReport",
    "execute_persons",
    "plan_persons",
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
