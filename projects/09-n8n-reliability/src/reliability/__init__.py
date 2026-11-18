"""n8n reliability kit: per-item retries that never re-send what already landed."""

from .retry import (
    IDEMPOTENT_METHODS,
    RETRYABLE_STATUSES,
    Decision,
    HttpResult,
    ItemResult,
    RetryPolicy,
    RunReport,
    TransportError,
    decide,
    is_idempotent,
    retry_after_seconds,
    run_items,
)

__version__ = "0.0.1"

__all__ = [
    "IDEMPOTENT_METHODS",
    "RETRYABLE_STATUSES",
    "Decision",
    "HttpResult",
    "ItemResult",
    "RetryPolicy",
    "RunReport",
    "TransportError",
    "decide",
    "is_idempotent",
    "retry_after_seconds",
    "run_items",
]
