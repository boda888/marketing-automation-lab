"""FastAPI service: vendor webhook -> intake -> classify (+ optional LLM stage) -> route plan.

The caller wires secrets, the dedupe store and (optionally) an LLM client, so the service
never picks a vendor account or a live model on its own:

    from replyrouting.api import ReplyService, create_app
    app = create_app(ReplyService(IntakeConfig(lemlist_secret="...",
                                               path_tokens={"smartlead": "<random>"}),
                                  seen=SqliteSeenEvents("seen.db")), api_token="...")

or ``uvicorn replyrouting.api:app_from_env --factory`` (see ``app_from_env``).

``POST /v1/intake/{vendor}/{token}``   Smartlead / Instantly / lemlist body as delivered
``POST /v1/intake/{vendor}``           (lemlist: secret in the body, no path token needed)
``POST /v1/linkedin/{vendor}/{token}`` HeyReach / Aimfox envelope mapped by n8n
``POST /v1/replies``                   ``{event_key, body_text}``: fill a ``needs_body`` event
                                       once, then classify + route (bearer token)
``GET  /v1/replies/pending``           ``needs_body`` events still waiting (bearer token)

HTTP status is chosen for the sender's retry logic, the outcome for n8n's Switch:

- 401 ``rejected`` (token / secret wrong or not configured): Smartlead retries a 401 but not
  a 400 (search summary of its help-centre webhook-failures page; page not opened), so a
  delivery refused during a mis-configuration can still be retriggered after the fix;
- 404 unknown vendor, 400 body that is not JSON (a retry cannot fix either);
- 503 when classifying / routing raised *after* the event key was recorded: the key is
  released first, so the vendor's retry (or a retrigger) is processed, not dropped as a
  duplicate;
- 200 for every other outcome (``accepted``, ``needs_body``, ``duplicate``, ``stale``,
  ``alert``, ``invalid``, ``label_only``, ``ignored``) with ``outcome`` + ``reason``: a non-2xx
  answer would only make the vendor resend an event the lab already decided about.

Senders want a fast 2xx (Smartlead: answer quickly, process heavy work in the background;
a 30 s Instantly timeout appears only on a third-party skill page — both search summaries,
not opened). Without an LLM client the route does only in-process work; with one, the model
call is inline, so keep its timeout well under the sender's or put n8n's Respond-to-Webhook
node in front.
"""

from __future__ import annotations

import hmac
import json
import os
import threading
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .intake import (
    EVENT_MAPS,
    LINKEDIN_EVENT_MAPS,
    IntakeConfig,
    IntakeOutcome,
    IntakeResult,
    MemorySeenEvents,
    SeenEvents,
    SqliteSeenEvents,
    VendorEvent,
    classify_event,
    intake,
    linkedin_intake,
    to_reply,
)
from .llm import IntentPolicy, LLMClient, classify_intent
from .routing import RoutingPolicy, route

__version__ = "0.0.6"
MAX_BODY_BYTES = 256_000  # a reply webhook is a few KB; refuse megabyte bodies before parsing
MAX_REPLY_CHARS = 50_000


class ProcessingError(Exception):
    """Classifying / routing failed after the key was recorded; the key has been released."""


class ReplyService:
    """Holds the dedupe store, the ``needs_body`` waiting room and the optional LLM client.

    ``pending`` is in-process and bounded (oldest evicted first): a restart loses waiting
    ``needs_body`` events, which then have to be re-sent by the vendor (retrigger) after
    their key is released from the dedupe store. ``seen`` may be shared across processes
    (``SqliteSeenEvents``); ``pending`` may not.
    """

    def __init__(
        self,
        config: IntakeConfig,
        seen: SeenEvents | None = None,
        *,
        llm: LLMClient | None = None,
        intent_policy: IntentPolicy | None = None,
        routing_policy: RoutingPolicy | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        max_pending: int = 10_000,
    ) -> None:
        self.config = config
        self.seen = seen if seen is not None else MemorySeenEvents(max_keys=100_000)
        self.llm = llm
        self.intent_policy = intent_policy or IntentPolicy()
        self.routing_policy = routing_policy or RoutingPolicy()
        self.clock = clock
        self.max_pending = max_pending
        self._pending: OrderedDict[str, VendorEvent] = OrderedDict()
        self._lock = threading.Lock()

    # --- intake -------------------------------------------------------------------------

    def receive(self, vendor: str, body: Any, token: str | None, *, linkedin: bool) -> dict:
        now = self.clock()
        if linkedin:
            res = linkedin_intake(
                vendor, body, now=now, config=self.config, seen=self.seen, token=token
            )
        else:
            res = intake(
                vendor, body, now=now, config=self.config, seen=self.seen, path_token=token
            )
        if res.outcome is IntakeOutcome.ACCEPTED:
            return self._decide(res, None)
        if res.outcome is IntakeOutcome.NEEDS_BODY and res.event and res.event.event_key:
            with self._lock:
                self._pending[res.event.event_key] = res.event
                while len(self._pending) > self.max_pending:
                    self._pending.popitem(last=False)
        return _result_dict(res)

    def fill_body(self, event_key: str, body_text: str) -> dict:
        """Classify a ``needs_body`` event once its text was fetched from the inbox API.
        Unknown or already filled key -> KeyError (the caller answers 404 / 409)."""
        with self._lock:
            event = self._pending.pop(event_key, None)
        if event is None:
            raise KeyError(event_key)
        res = IntakeResult(IntakeOutcome.ACCEPTED, "body filled in", event)
        try:
            return self._decide(res, body_text, release=False)
        except ProcessingError:
            with self._lock:  # put it back: the fill can be retried
                self._pending[event_key] = event
            raise

    def pending(self) -> list[dict]:
        with self._lock:
            events = list(self._pending.values())
        return [_event_dict(e) for e in events]

    # --- classify + route ---------------------------------------------------------------

    def _decide(self, res: IntakeResult, body_text: str | None, *, release: bool = True) -> dict:
        event = res.event
        assert event is not None and event.event_key
        try:
            classification = classify_event(event, body_text)
            reply = to_reply(event, body_text)
            llm_usage = None
            if classification.needs_llm and self.llm is not None:
                ir = classify_intent(reply, classification, self.llm, self.intent_policy)
                classification = ir.classification
                llm_usage = {
                    "calls": len(ir.attempts),
                    "input_tokens": ir.input_tokens,
                    "output_tokens": ir.output_tokens,
                }
            decision = route(reply, classification, self.routing_policy)
        except Exception as exc:
            if release:
                self.seen.release(event.event_key)
            raise ProcessingError(f"{type(exc).__name__} while deciding {event.event_key}") from exc

        out = _result_dict(res)
        out.update(
            reply_class=classification.reply_class.value,
            rule=classification.rule,
            needs_human_review=classification.needs_human_review,
            review_reason=classification.review_reason,
            bounce_kind=classification.bounce_kind.value if classification.bounce_kind else None,
            return_date=classification.return_date.isoformat()
            if classification.return_date
            else None,
            actions=[
                {
                    **{k: v for k, v in asdict(a).items() if k != "until"},
                    "kind": a.kind.value,
                    "channel": a.channel.value if a.channel else None,
                    "until": a.until.isoformat() if a.until else None,
                }
                for a in decision.actions
            ],
            warnings=list(decision.warnings),
            llm=llm_usage,
        )
        return out


def _event_dict(event: VendorEvent) -> dict:
    # No payload and no body text: the response feeds n8n logs, not an archive of replies.
    return {
        "event_key": event.event_key,
        "vendor": event.vendor,
        "event_type": event.event_type,
        "kind": event.kind.value,
        "channel": event.channel.value if event.channel else None,
        "occurred_at": event.occurred_at.isoformat() if event.occurred_at else None,
        "person_email": event.person_email,
        "person_linkedin_url": event.person_linkedin_url,
        "campaign_id": event.campaign_id,
        "vendor_label": event.vendor_label,
    }


def _result_dict(res: IntakeResult) -> dict:
    out: dict[str, Any] = {"outcome": res.outcome.value, "reason": res.reason}
    out["event"] = _event_dict(res.event) if res.event else None
    return out


class FillBody(BaseModel):
    event_key: str = Field(min_length=1, max_length=512)
    body_text: str = Field(min_length=1, max_length=MAX_REPLY_CHARS)


def create_app(service: ReplyService, api_token: str | None = None) -> FastAPI:
    app = FastAPI(title="07 reply routing", version=__version__)

    def require_token(authorization: str | None = Header(default=None)) -> None:
        if api_token is None:
            return
        supplied = (authorization or "").removeprefix("Bearer ").encode()
        if not authorization or not hmac.compare_digest(supplied, api_token.encode()):
            raise HTTPException(401, "missing or wrong bearer token")

    async def _read_json(request: Request) -> Any:
        raw = await request.body()
        if len(raw) > MAX_BODY_BYTES:
            raise HTTPException(413, f"body over {MAX_BODY_BYTES} bytes")
        try:
            return json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise HTTPException(400, "body is not JSON") from None

    def _answer(vendor: str, body: Any, token: str | None, linkedin: bool) -> JSONResponse:
        maps = LINKEDIN_EVENT_MAPS if linkedin else EVENT_MAPS
        if vendor.strip().lower() not in maps:
            raise HTTPException(404, f"unknown vendor {vendor!r}")
        try:
            result = service.receive(vendor, body, token, linkedin=linkedin)
        except ProcessingError as exc:
            return JSONResponse({"outcome": "error", "reason": str(exc)}, status_code=503)
        status = 401 if result["outcome"] == IntakeOutcome.REJECTED.value else 200
        return JSONResponse(result, status_code=status)

    @app.post("/v1/intake/{vendor}/{token}")
    async def intake_with_token(vendor: str, token: str, request: Request) -> JSONResponse:
        return _answer(vendor, await _read_json(request), token, linkedin=False)

    @app.post("/v1/intake/{vendor}")
    async def intake_without_token(vendor: str, request: Request) -> JSONResponse:
        return _answer(vendor, await _read_json(request), None, linkedin=False)

    @app.post("/v1/linkedin/{vendor}/{token}")
    async def linkedin_with_token(vendor: str, token: str, request: Request) -> JSONResponse:
        return _answer(vendor, await _read_json(request), token, linkedin=True)

    @app.post("/v1/replies", dependencies=[Depends(require_token)])
    def fill_body(req: FillBody) -> JSONResponse:
        try:
            return JSONResponse(service.fill_body(req.event_key, req.body_text))
        except KeyError:
            raise HTTPException(
                404, "no needs_body event with this key (unknown, already filled or evicted)"
            ) from None
        except ProcessingError as exc:
            return JSONResponse({"outcome": "error", "reason": str(exc)}, status_code=503)

    @app.get("/v1/replies/pending", dependencies=[Depends(require_token)])
    def pending() -> dict:
        return {"pending": service.pending()}

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True, "version": __version__}

    return app


ENV_PREFIX = "REPLYROUTING_"
TOKEN_VENDORS = ("smartlead", "instantly", "heyreach", "aimfox")


def app_from_env(environ: dict[str, str] | None = None) -> FastAPI:
    """Build the app from environment variables (no LLM client: rule stage + routing only).

    ``REPLYROUTING_LEMLIST_SECRET``, ``REPLYROUTING_TOKEN_<VENDOR>`` for smartlead /
    instantly / heyreach / aimfox, ``REPLYROUTING_SEEN_DB`` (SQLite file shared by every
    worker; unset = in-memory, one process only), ``REPLYROUTING_API_TOKEN`` (bearer token for
    ``/v1/replies``; required — the app refuses to start without it). A vendor without a
    secret / token is refused at intake (fail closed), not accepted unauthenticated.
    """
    env = os.environ if environ is None else environ
    api_token = env.get(ENV_PREFIX + "API_TOKEN")
    if not api_token:
        raise RuntimeError(f"{ENV_PREFIX}API_TOKEN is not set: refusing to start without auth")
    tokens = {
        v: env[ENV_PREFIX + "TOKEN_" + v.upper()]
        for v in TOKEN_VENDORS
        if env.get(ENV_PREFIX + "TOKEN_" + v.upper())
    }
    config = IntakeConfig(lemlist_secret=env.get(ENV_PREFIX + "LEMLIST_SECRET"), path_tokens=tokens)
    db = env.get(ENV_PREFIX + "SEEN_DB")
    seen: SeenEvents = SqliteSeenEvents(db) if db else MemorySeenEvents(max_keys=100_000)
    return create_app(ReplyService(config, seen), api_token=api_token)
