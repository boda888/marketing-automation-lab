"""FastAPI service: write rows to a CRM, take merge webhooks, keep one IdMap true.

The caller wires tokens, transport, secrets and the public URL, so the service never picks an
account or a live transport on its own:

    from crmsync.api import CrmService, create_app
    app = create_app(CrmService(transport=UrllibTransport(), tokens={"hubspot": "..."},
                                secrets=WebhookSecrets(hubspot_client_secret="..."),
                                public_base_url="https://hooks.example"), api_token="...")

``POST /v1/write``           ``write(rows, crm=...)``: one outcome per row for n8n routing;
                             a CRM without a token → 404, a bad request (duplicate row_id,
                             Attio slug with no object UUID) → 422 before any call.
``POST /v1/webhooks/{crm}``  origin check on the **raw body bytes** first (HubSpot v3 HMAC
                             over the *public* URL, Attio HMAC, Pipedrive Basic): refused →
                             401, no secret configured → 500 (the CRM retries later); then
                             parse merge events, apply them to the IdMap, answer at once.
                             HubSpot redeliveries are deduped on ``eventId`` (never on
                             ``attemptNumber``, which changes per retry); Attio and Pipedrive
                             replays are harmless because ``apply_merges`` is idempotent.

The webhook route has no bearer token: CRMs cannot send one, their own signature is the auth.
HubSpot gives a webhook about 5 s to answer (search summary of its error-handling page; page
not opened), so the route does only in-memory work.
"""

from __future__ import annotations

import hmac
import json
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .executor import RateBudget, Transport
from .idmap import (
    IdMap,
    MemoryIdMap,
    MergeEvent,
    apply_merges,
    parse_attio_merges,
    parse_hubspot_merges,
    parse_pipedrive_merge,
)
from .models import CrmRow
from .sync import WriteOptions, write
from .webhook_auth import WebhookSecrets, verify_webhook

__version__ = "0.0.9"
MAX_ROWS = 1000
Crm = Literal["hubspot", "attio", "pipedrive"]


def default_budgets() -> dict[str, RateBudget]:
    """Lab assumptions, not vendor-confirmed limits (see README limits)."""
    return {
        "hubspot": RateBudget(100, 10.0),
        "attio": RateBudget(25, 1.0),
        "pipedrive": RateBudget(10, 2.0),
    }


class LockedIdMap:
    """Serialises each IdMap call: write routes run in a thread pool, webhooks on the loop."""

    def __init__(self, inner: IdMap) -> None:
        self.inner = inner
        self._lock = threading.Lock()

    def get(self, crm: str, key: str) -> str | None:
        with self._lock:
            return self.inner.get(crm, key)

    def put(self, crm: str, key: str, crm_id: str) -> None:
        with self._lock:
            self.inner.put(crm, key, crm_id)

    def evict(self, crm: str, key: str) -> None:
        with self._lock:
            self.inner.evict(crm, key)

    def keys_for(self, crm: str, crm_id: str) -> list[str]:
        with self._lock:
            return self.inner.keys_for(crm, crm_id)


class SeenEvents:
    """Bounded memory of delivery ids already applied (per process, oldest dropped first)."""

    def __init__(self, max_size: int = 50_000) -> None:
        self.max_size = max_size
        self._ids: OrderedDict[str, None] = OrderedDict()
        self._lock = threading.Lock()

    def seen(self, event_id: str) -> bool:
        with self._lock:
            return event_id in self._ids

    def add(self, event_id: str) -> None:
        with self._lock:
            self._ids[event_id] = None
            self._ids.move_to_end(event_id)
            while len(self._ids) > self.max_size:
                self._ids.popitem(last=False)


@dataclass
class CrmService:
    transport: Transport
    tokens: dict[str, str]  # crm -> API token; a CRM without one is not served
    secrets: WebhookSecrets
    public_base_url: str  # the URL the CRM calls; HubSpot v3 signs it, not our host header
    idmap: IdMap = field(default_factory=MemoryIdMap)
    budgets: dict[str, RateBudget] = field(default_factory=default_budgets)
    options: dict[str, WriteOptions] = field(default_factory=dict)
    allow_fixtures: bool = False  # only with a fake transport (tests, demo)
    seen_events: SeenEvents = field(default_factory=SeenEvents)
    now_ms: Callable[[], int] = field(default=lambda: int(time.time() * 1000))
    sleep: Callable[[float], None] = time.sleep
    pipedrive_lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        if not self.public_base_url.startswith("https://"):
            raise ValueError("public_base_url must be the https:// URL the CRM calls")
        self.idmap = LockedIdMap(self.idmap)


class RowIn(BaseModel):
    row_id: str = Field(min_length=1)
    email: str | None = None
    properties: dict[str, Any] = Field(default_factory=dict)


class WriteRequest(BaseModel):
    crm: Crm
    rows: list[RowIn] = Field(min_length=1, max_length=MAX_ROWS)


class RowResult(BaseModel):
    row_id: str
    key: str | None
    outcome: str
    crm_id: str | None = None
    reason: str | None = None
    retryable: bool = False


class WriteResponse(BaseModel):
    crm: Crm
    summary: dict[str, Any]
    idmap_recorded: int
    outcomes: list[RowResult]


def _hubspot_events(
    service: CrmService, body: object
) -> tuple[list[MergeEvent], list[str], int, list[str]]:
    """Drop items whose ``eventId`` was already applied (or repeats in this body)."""
    items = body if isinstance(body, list) else [body]
    fresh: list[object] = []
    fresh_ids: list[str] = []
    duplicates = 0
    for item in items:
        event_id = item.get("eventId") if isinstance(item, dict) else None
        key = f"hubspot:{event_id}" if event_id is not None else None
        if key is not None and (service.seen_events.seen(key) or key in fresh_ids):
            duplicates += 1
            continue
        fresh.append(item)
        if key is not None:
            fresh_ids.append(key)
    events, ignored = parse_hubspot_merges(fresh)
    return events, ignored, duplicates, fresh_ids


def create_app(service: CrmService, *, api_token: str | None = None) -> FastAPI:
    """``api_token`` set → ``/v1/write`` needs ``Authorization: Bearer <token>``."""
    app = FastAPI(title="crm-sync", version=__version__)

    def auth(authorization: str | None = Header(default=None)) -> None:
        if api_token is None:
            return
        given = (authorization or "").removeprefix("Bearer ").strip()
        if not hmac.compare_digest(given.encode(), api_token.encode()):
            raise HTTPException(status_code=401, detail="bad or missing bearer token")

    @app.get("/healthz")
    def healthz() -> dict[str, object]:
        return {"ok": True, "version": __version__, "crms": sorted(service.tokens)}

    @app.post("/v1/write", response_model=WriteResponse, dependencies=[Depends(auth)])
    def write_rows(req: WriteRequest) -> WriteResponse:
        token = service.tokens.get(req.crm)
        if not token:
            raise HTTPException(status_code=404, detail=f"{req.crm} is not configured")
        rows = [CrmRow(r.row_id, r.email, dict(r.properties)) for r in req.rows]
        try:
            result = write(
                rows,
                crm=req.crm,
                transport=service.transport,
                token=token,
                budget=service.budgets[req.crm],
                idmap=service.idmap,
                options=service.options.get(req.crm),
                allow_fixtures=service.allow_fixtures,
                lock=service.pipedrive_lock if req.crm == "pipedrive" else None,
                sleep=service.sleep,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return WriteResponse(
            crm=req.crm,
            summary=result.summary,
            idmap_recorded=result.idmap_recorded,
            outcomes=[
                RowResult(
                    row_id=o.row_id,
                    key=o.key,
                    outcome=o.outcome.value,
                    crm_id=o.crm_id,
                    reason=o.reason,
                    retryable=o.retryable,
                )
                for o in result.outcomes
            ],
        )

    @app.post("/v1/webhooks/{crm}")
    async def webhook(crm: str, request: Request) -> JSONResponse:
        raw = await request.body()  # exact bytes: re-serialised JSON breaks signatures
        url = service.public_base_url.rstrip("/") + request.url.path
        if request.url.query:
            url += "?" + request.url.query
        verdict = verify_webhook(
            crm,
            headers=dict(request.headers),
            raw_body=raw,
            method=request.method,
            url=url,
            secrets=service.secrets,
            now_ms=service.now_ms(),
        )
        if not verdict.ok:
            return JSONResponse({"error": verdict.reason}, status_code=verdict.status)
        try:
            body = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return JSONResponse({"error": "invalid_json"}, status_code=400)

        duplicates = 0
        fresh_ids: list[str] = []
        if crm == "hubspot":
            events, ignored, duplicates, fresh_ids = _hubspot_events(service, body)
        elif crm == "attio":
            events, ignored = parse_attio_merges(body)
        else:
            events, ignored = parse_pipedrive_merge(body)
        report = apply_merges(service.idmap, events)
        for event_id in fresh_ids:  # only after the merge landed in the map
            service.seen_events.add(event_id)
        return JSONResponse(
            {
                "crm": crm,
                "merge": report.summary(),
                "duplicates": duplicates,
                "ignored": ignored,
            }
        )

    return app
