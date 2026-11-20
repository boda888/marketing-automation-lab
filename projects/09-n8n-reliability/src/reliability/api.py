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

Dedupe keys (``reliability.dedupe.KeyStore``), so a key is marked done only after the
guarded call succeeded:

- ``POST /v1/keys/claim`` ``{scope, key, stale_after_seconds?}`` -> ``status``:
  ``reserved`` (go; keep ``token``), ``committed`` (skip), ``in_flight`` (skip this run),
  ``stale`` (may have landed: read back, then ``/v1/keys/resolve``);
- ``POST /v1/keys/commit`` / ``/v1/keys/release`` ``{scope, key, token}``: 409 for another
  execution's token or a committed key, 404 without a reservation;
- ``POST /v1/keys/resolve`` ``{scope, key, landed, force?}``: 409 while the reservation is
  younger than ``stale_after`` unless ``force``;
- ``GET /v1/keys/pending?scope=&older_than=`` for a watchdog.

Dead-letter rows (``reliability.dlq.DlqStore``), one per ``(scope, key)``:

- ``POST /v1/dlq`` ``{scope, row: <dlq_row from /v1/decide>, payload?, source?}`` -> the
  stored row (a repeat failure of the same key updates it; ``safe_to_resend`` is recomputed);
- ``GET /v1/dlq?state=open&scope=&outcome=`` and ``GET /v1/dlq/summary?scope=`` for alerts;
- ``POST /v1/dlq/replay`` ``{ids?, scope?, dry_run?}`` -> ``replay`` items with payloads plus
  refusals (``unsafe_to_resend`` for unknown / failed, ``not_due``, ``replay_limit``);
- ``POST /v1/dlq/{id}/resolve`` ``{landed, note?}`` after a read-back, and
  ``POST /v1/dlq/{id}/discard`` ``{note?}``: 404 unknown id, 409 closed row.

    uvicorn reliability.api:app_from_env --factory
    # RELIABILITY_API_TOKEN optional; RELIABILITY_DB (SQLite path, default in-memory);
    # RELIABILITY_STALE_AFTER seconds (default 900); RELIABILITY_DLQ_DB (default
    # RELIABILITY_DB); RELIABILITY_MAX_REPLAYS (default 3)
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

from .dedupe import AlreadyCommitted, KeyStore, NotReserved, StillInFlight, TokenMismatch
from .dlq import DlqStore, RowClosed, RowNotFound
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


class ClaimIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: str = Field(min_length=1, max_length=200)
    key: str = Field(min_length=1, max_length=512)
    stale_after_seconds: float | None = Field(default=None, gt=0)


class TokenIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: str = Field(min_length=1, max_length=200)
    key: str = Field(min_length=1, max_length=512)
    token: str = Field(min_length=1, max_length=200)


class ResolveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: str = Field(min_length=1, max_length=200)
    key: str = Field(min_length=1, max_length=512)
    landed: bool
    force: bool = False


class DlqIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: str = Field(min_length=1, max_length=200)
    row: dict[str, Any]
    payload: Any = None
    source: dict[str, Any] = Field(default_factory=dict)


class ReplayIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ids: list[int] | None = Field(default=None, max_length=1000)
    scope: str | None = Field(default=None, min_length=1, max_length=200)
    dry_run: bool = False


class ResolveRowIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    landed: bool
    note: str | None = Field(default=None, max_length=1000)


class NoteIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: str | None = Field(default=None, max_length=1000)


def _dlq_errors(call: Callable[[], Any]) -> Any:
    try:
        return call()
    except RowNotFound as exc:
        raise HTTPException(404, str(exc)) from None
    except RowClosed as exc:
        raise HTTPException(409, str(exc)) from None
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None


def _key_errors(call: Callable[[], Any]) -> Any:
    try:
        return call()
    except NotReserved as exc:
        raise HTTPException(404, str(exc)) from None
    except (TokenMismatch, StillInFlight, AlreadyCommitted) as exc:
        raise HTTPException(409, str(exc)) from None
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None


def create_app(
    *,
    api_token: str | None = None,
    store: KeyStore | None = None,
    dlq: DlqStore | None = None,
    now: Callable[[], float] = time.time,
    rand: Callable[[], float] = random.random,
) -> FastAPI:
    app = FastAPI(title="n8n reliability kit", version="0.0.5")
    keys = store if store is not None else KeyStore(clock=now)
    dead = dlq if dlq is not None else DlqStore(clock=now)

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

    @app.post("/v1/keys/claim", dependencies=[Depends(check_token)])
    def claim_route(body: ClaimIn) -> dict[str, Any]:
        c = _key_errors(
            lambda: keys.claim(body.scope, body.key, stale_after=body.stale_after_seconds)
        )
        return {
            "status": c.status,
            "go": c.go,
            "token": c.token,
            "since": c.since,
            "age_seconds": round(c.age_seconds, 3),
        }

    @app.post("/v1/keys/commit", dependencies=[Depends(check_token)])
    def commit_route(body: TokenIn) -> dict[str, Any]:
        _key_errors(lambda: keys.commit(body.scope, body.key, body.token))
        return {"state": "committed"}

    @app.post("/v1/keys/release", dependencies=[Depends(check_token)])
    def release_route(body: TokenIn) -> dict[str, Any]:
        _key_errors(lambda: keys.release(body.scope, body.key, body.token))
        return {"state": "released"}

    @app.post("/v1/keys/resolve", dependencies=[Depends(check_token)])
    def resolve_route(body: ResolveIn) -> dict[str, Any]:
        state = _key_errors(
            lambda: keys.resolve(body.scope, body.key, landed=body.landed, force=body.force)
        )
        return {"state": state or "released"}

    @app.get("/v1/keys/pending", dependencies=[Depends(check_token)])
    def pending_route(scope: str | None = None, older_than: float = 0.0) -> dict[str, Any]:
        rows = keys.pending(scope=scope, older_than=older_than)
        return {
            "count": len(rows),
            "stale": sum(r.stale for r in rows),
            "reservations": [
                {
                    "scope": r.scope,
                    "key": r.key,
                    "reserved_at": r.reserved_at,
                    "age_seconds": round(r.age_seconds, 3),
                    "stale": r.stale,
                }
                for r in rows
            ],
        }

    @app.post("/v1/dlq", dependencies=[Depends(check_token)])
    def dlq_intake_route(body: DlqIn) -> dict[str, Any]:
        row = _dlq_errors(
            lambda: dead.intake(
                body.row, scope=body.scope, payload=body.payload, source=body.source
            )
        )
        return row.as_dict()

    @app.get("/v1/dlq", dependencies=[Depends(check_token)])
    def dlq_list_route(
        state: str | None = "open",
        scope: str | None = None,
        outcome: str | None = None,
        limit: int = 500,
    ) -> dict[str, Any]:
        if state == "all":
            state = None
        rows = dead.rows(state=state, scope=scope, outcome=outcome, limit=limit)  # type: ignore[arg-type]
        return {"count": len(rows), "rows": [r.as_dict() for r in rows]}

    @app.get("/v1/dlq/summary", dependencies=[Depends(check_token)])
    def dlq_summary_route(scope: str | None = None) -> dict[str, Any]:
        return dead.summary(scope=scope)

    @app.post("/v1/dlq/replay", dependencies=[Depends(check_token)])
    def dlq_replay_route(body: ReplayIn) -> dict[str, Any]:
        items = dead.replay(body.ids, scope=body.scope, dry_run=body.dry_run)
        listed = [
            {
                "id": i.id,
                "status": i.status,
                "go": i.go,
                "scope": i.scope,
                "key": i.key,
                "payload": i.payload,
                "retry_at": i.retry_at,
                "reason": i.reason,
            }
            for i in items
        ]
        return {
            "dry_run": body.dry_run,
            "replay": [i for i in listed if i["go"]],
            "refused": [i for i in listed if not i["go"]],
        }

    @app.post("/v1/dlq/{row_id}/resolve", dependencies=[Depends(check_token)])
    def dlq_resolve_route(row_id: int, body: ResolveRowIn) -> dict[str, Any]:
        return _dlq_errors(
            lambda: dead.resolve(row_id, landed=body.landed, note=body.note)
        ).as_dict()

    @app.post("/v1/dlq/{row_id}/discard", dependencies=[Depends(check_token)])
    def dlq_discard_route(row_id: int, body: NoteIn) -> dict[str, Any]:
        return _dlq_errors(lambda: dead.discard(row_id, note=body.note)).as_dict()

    return app


def app_from_env() -> FastAPI:
    token = os.environ.get("RELIABILITY_API_TOKEN") or None
    store = KeyStore(
        os.environ.get("RELIABILITY_DB") or ":memory:",
        stale_after=float(os.environ.get("RELIABILITY_STALE_AFTER") or 900),
    )
    dlq = DlqStore(
        os.environ.get("RELIABILITY_DLQ_DB") or os.environ.get("RELIABILITY_DB") or ":memory:",
        max_replays=int(os.environ.get("RELIABILITY_MAX_REPLAYS") or 3),
    )
    return create_app(api_token=token, store=store, dlq=dlq)
