"""FastAPI service: ``decide()`` over HTTP, so an n8n loop can retry one item at a time.

n8n's own Retry On Fail re-sends every item of the node; this endpoint lets a workflow ask,
for **one** item after **one** attempt, what to do next:

    HTTP Request (Never Error + full response, or continue-on-error output)
      -> POST /v1/decide {attempt, method, status | error, headers, request_headers, key}
      -> Switch on ``action``: done | retry (Wait ``wait_ms``, same call, attempt + 1) | stop

``POST /v1/decide`` body:

- ``attempt`` (1-based) and ``method`` of the call that was just made;
- either ``status`` (alias ``statusCode``, as n8n's full response names it) plus the
  response ``headers`` (``Retry-After`` is read from them), or ``error`` when there was no
  HTTP answer: ``{"code": "ECONNREFUSED"}`` and/or ``{"reached_vendor": false}``;
- ``request_headers``: an ``Idempotency-Key`` there makes the call keyed (409 = still in
  flight, 422 = key reused). Send it only for vendors that document honouring the key;
- ``idempotent``: set ``true`` for a POST upsert on a unique key; left out, it follows the
  method (RFC 9110 §9.2.2) or the key;
- ``policy`` (optional): ``RetryPolicy`` fields; ``key`` (optional): the item's key for the
  DLQ row.

Error codes count as "never left this machine" only for DNS, refused / unreachable and TLS
certificate failures (the same split as 08's transport). Any other or missing code is
treated as "may have reached the vendor", so a plain create ends ``unknown``, never retried
blind.

Response: ``action``, ``outcome`` (on done / stop), ``wait_ms`` + ``next_attempt`` (on
retry), ``retry_at`` (epoch seconds) and ``retry_at_iso`` (on deferred), ``reason``,
``safe_to_resend`` and a flat ``dlq_row`` on every stop that is not ``ok``.

Answers are 200 for every decision; 422 only for a body that cannot be decided (both or
neither of ``status`` / ``error``, a bad policy). With ``api_token`` set the route needs
``Authorization: Bearer <token>``.

    uvicorn reliability.api:app_from_env --factory   # RELIABILITY_API_TOKEN optional
"""

from __future__ import annotations

import hmac
import os
import random
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator

from .retry import HttpResult, ItemResult, RetryPolicy, TransportError, decide, is_idempotent

# Node.js / OpenSSL error codes raised before any request bytes reach the vendor.
NOT_SENT_CODES = frozenset(
    {
        "ENOTFOUND",
        "EAI_AGAIN",
        "ECONNREFUSED",
        "EHOSTUNREACH",
        "ENETUNREACH",
        "ERR_INVALID_URL",
        "CERT_HAS_EXPIRED",
        "DEPTH_ZERO_SELF_SIGNED_CERT",
        "SELF_SIGNED_CERT_IN_CHAIN",
        "UNABLE_TO_VERIFY_LEAF_SIGNATURE",
        "UNABLE_TO_GET_ISSUER_CERT_LOCALLY",
        "ERR_TLS_CERT_ALTNAME_INVALID",
    }
)


def reached_vendor_from_code(code: str | None) -> bool:
    """False only for codes that prove the request never left; unknown codes -> True."""
    return not (code and code.strip().upper() in NOT_SENT_CODES)


class CallError(BaseModel):
    code: str | None = None
    reached_vendor: bool | None = None  # explicit value wins over the code
    message: str | None = None


class PolicyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_attempts: int = 4
    base_seconds: float = 1.0
    factor: float = 2.0
    max_backoff_seconds: float = 60.0
    max_wait_seconds: float = 300.0
    jitter: float = 0.0


class DecideIn(BaseModel):
    attempt: int = Field(ge=1)
    method: str = Field(min_length=1, max_length=16)
    status: int | None = Field(
        default=None, ge=100, le=599, validation_alias=AliasChoices("status", "statusCode")
    )
    headers: dict[str, Any] = Field(default_factory=dict)
    error: CallError | None = None
    request_headers: dict[str, Any] = Field(default_factory=dict)
    idempotent: bool | None = None
    policy: PolicyIn | None = None
    key: str | None = Field(default=None, max_length=512)

    @model_validator(mode="after")
    def _one_result(self) -> DecideIn:
        if (self.status is None) == (self.error is None):
            raise ValueError("send exactly one of status / statusCode or error")
        return self


def _str_headers(headers: dict[str, Any]) -> dict[str, str]:
    """n8n may hand multi-value headers as lists; keep the first value of each."""
    out: dict[str, str] = {}
    for name, value in headers.items():
        if isinstance(value, list):
            value = value[0] if value else ""
        if value is not None:
            out[str(name)] = str(value)
    return out


def decide_request(
    body: DecideIn,
    *,
    now: Callable[[], float] = time.time,
    rand: Callable[[], float] = random.random,
) -> dict[str, Any]:
    """The route's logic without HTTP: one attempt in, one decision out (as JSON)."""
    try:
        policy = RetryPolicy(**body.policy.model_dump()) if body.policy else None
    except ValueError as exc:
        raise HTTPException(422, f"bad policy: {exc}") from None

    request_headers = _str_headers(body.request_headers)
    keyed = any(k.lower() == "idempotency-key" and v.strip() for k, v in request_headers.items())
    idempotent = (
        body.idempotent
        if body.idempotent is not None
        else is_idempotent(body.method, request_headers)
    )

    result: HttpResult | TransportError
    if body.error is not None:
        reached = body.error.reached_vendor
        if reached is None:
            reached = reached_vendor_from_code(body.error.code)
        result = TransportError(body.error.message or body.error.code or "", reached_vendor=reached)
    else:
        assert body.status is not None
        result = HttpResult(status=body.status, headers=_str_headers(body.headers))

    d = decide(
        result,
        attempt=body.attempt,
        idempotent=idempotent,
        keyed=keyed,
        policy=policy,
        now=now,
        rand=rand,
    )
    out: dict[str, Any] = {
        "action": d.action,
        "outcome": d.outcome,
        "reason": d.reason,
        "attempt": body.attempt,
        "idempotent": idempotent or keyed,
        "wait_ms": d.wait_ms if d.action == "retry" else 0,
        "next_attempt": body.attempt + 1 if d.action == "retry" else None,
        "retry_at": d.retry_at,
        "retry_at_iso": (
            datetime.fromtimestamp(d.retry_at, UTC).isoformat() if d.retry_at is not None else None
        ),
        "safe_to_resend": None,
        "dlq_row": None,
    }
    if d.action == "stop":
        assert d.outcome is not None
        item = ItemResult(
            key=body.key if body.key is not None else "",
            outcome=d.outcome,
            attempts=body.attempt,
            reason=d.reason,
            last_status=body.status,
            retry_at=d.retry_at,
        )
        out["safe_to_resend"] = item.safe_to_resend
        out["dlq_row"] = item.dlq_row()
    return out


def create_app(
    *,
    api_token: str | None = None,
    now: Callable[[], float] = time.time,
    rand: Callable[[], float] = random.random,
) -> FastAPI:
    app = FastAPI(title="n8n reliability kit", version="0.0.3")

    def check_token(authorization: str | None = Header(default=None)) -> None:
        if api_token is None:
            return
        expected = f"Bearer {api_token}"
        if authorization is None or not hmac.compare_digest(
            authorization.encode(), expected.encode()
        ):
            raise HTTPException(401, "bearer token required")

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/decide", dependencies=[Depends(check_token)])
    def decide_route(body: DecideIn) -> dict[str, Any]:
        return decide_request(body, now=now, rand=rand)

    return app


def app_from_env() -> FastAPI:
    token = os.environ.get("RELIABILITY_API_TOKEN") or None
    return create_app(api_token=token)
