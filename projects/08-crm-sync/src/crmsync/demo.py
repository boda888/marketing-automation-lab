"""Offline demo: the service wired to in-memory fake HubSpot, Attio and Pipedrive.

    uvicorn crmsync.demo:app --port 8008

No network and no account: ``FakeCrms`` answers the requests the real writers build, on the
writers' default URLs, so ``POST /v1/write`` runs plan → send → parse → IdMap end to end and
``POST /v1/webhooks/{crm}`` takes merges signed with the demo secrets below.

Scripted faults show every outcome branch the n8n workflow routes:

* an e-mail whose local part starts with ``reject+`` is refused by the CRM (HubSpot
  ``errors[]`` in a 207, Attio ``400 value_not_found``, Pipedrive ``400``) → ``failed``,
  not retryable → DLQ;
* one starting with ``busy+`` never lands: HubSpot leaves it out of both ``results`` and
  ``errors`` (``not_in_response``), Attio and the Pipedrive search answer 503 on every
  attempt → ``failed``, retryable → retry queue (upserts and searches are idempotent);
* Pipedrive already holds ``ana.ruiz@northwind.example`` (id 501, so that row is an
  update) and two people sharing ``office@contoso.example`` (``ambiguous_match``).

``.example`` rows are allowed only because the transport is fake (``allow_fixtures``).
"""

from __future__ import annotations

import itertools
import urllib.parse
from dataclasses import dataclass, field

from .api import CrmService, create_app
from .executor import HttpCall, HttpResponse, RateBudget
from .sync import WriteOptions
from .webhook_auth import WebhookSecrets

PUBLIC_BASE_URL = "https://hooks.crm-demo.example"
PEOPLE_OBJECT_ID = "6d1f3a52-8c7e-4b0a-9f21-3e5d7c9b1a04"  # fictional Attio people object
DEMO_SECRETS = WebhookSecrets(
    hubspot_client_secret="demo-hubspot-client-secret",
    attio_secret="demo-attio-webhook-secret",
    pipedrive_user="demo",
    pipedrive_password="demo-password",
)


def _local(email: str) -> str:
    return email.split("@", 1)[0]


@dataclass
class FakeCrms:
    """One in-memory CRM per vendor host; every request is recorded."""

    hubspot: dict[str, str] = field(default_factory=dict)  # email -> contact id
    attio: dict[str, str] = field(default_factory=dict)  # email -> record id
    pipedrive: dict[str, list[str]] = field(
        default_factory=lambda: {
            "501": ["ana.ruiz@northwind.example"],
            "502": ["office@contoso.example"],
            "503": ["office@contoso.example"],
        }
    )
    calls: list[HttpCall] = field(default_factory=list)
    _ids: itertools.count = field(default_factory=lambda: itertools.count(9001))

    def send(self, call: HttpCall) -> HttpResponse:
        self.calls.append(call)
        host = urllib.parse.urlsplit(call.url).hostname
        if host == "api.hubapi.com":
            return self._hubspot(call)
        if host == "api.attio.com":
            return self._attio(call)
        if host == "api.pipedrive.com":
            return self._pipedrive(call)
        return HttpResponse(404, {"message": f"no fake for {host}"})

    def _hubspot(self, call: HttpCall) -> HttpResponse:
        results, errors = [], []
        for item in call.json["inputs"]:
            email = item["id"].lower()
            if _local(email).startswith("busy+"):
                continue  # neither in results nor errors: a row the batch silently dropped
            if _local(email).startswith("reject+"):
                errors.append(
                    {
                        "status": "error",
                        "category": "VALIDATION_ERROR",
                        "message": "Property values were not valid",
                        "context": {"ids": [email]},
                    }
                )
                continue
            new = email not in self.hubspot
            cid = self.hubspot.setdefault(email, str(next(self._ids)))
            results.append(
                {"id": cid, "_new": new, "objectWriteTraceId": item["objectWriteTraceId"]}
            )
        body = {"status": "COMPLETE", "results": results, "errors": errors}
        return HttpResponse(207 if errors else 200, body)

    def _attio(self, call: HttpCall) -> HttpResponse:
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(call.url).query)
        (attribute,) = query["matching_attribute"]
        (value,) = call.json["data"]["values"][attribute]
        email = str(value["email_address"] if isinstance(value, dict) else value).lower()
        if _local(email).startswith("busy+"):
            return HttpResponse(503, {"message": "service unavailable"})
        if _local(email).startswith("reject+"):
            return HttpResponse(400, {"code": "value_not_found", "message": "Unknown option"})
        rid = self.attio.setdefault(email, f"rec-{next(self._ids)}")
        ident = {"workspace_id": "ws-demo", "object_id": PEOPLE_OBJECT_ID, "record_id": rid}
        return HttpResponse(200, {"data": {"id": ident, "values": {}}})

    def _pipedrive(self, call: HttpCall) -> HttpResponse:
        parts = urllib.parse.urlsplit(call.url)
        path = parts.path.removeprefix("/api/v2")
        if call.method == "GET" and path == "/persons/search":
            (term,) = urllib.parse.parse_qs(parts.query)["term"]
            if _local(term).startswith("busy+"):
                return HttpResponse(503, {"error": "service unavailable"})
            items = [
                {"item": {"id": int(pid), "emails": emails}}
                for pid, emails in self.pipedrive.items()
                if term.lower() in emails
            ]
            return HttpResponse(200, {"success": True, "data": {"items": items}})
        if call.method == "POST" and path == "/persons":
            email = call.json["emails"][0]["value"].lower()
            if _local(email).startswith("reject+"):
                return HttpResponse(400, {"success": False, "error": "Invalid field value"})
            pid = str(next(self._ids))
            self.pipedrive[pid] = [email]
            return HttpResponse(201, {"success": True, "data": {"id": int(pid)}})
        if call.method == "PATCH" and path.startswith("/persons/"):
            pid = path.rsplit("/", 1)[1]
            if pid not in self.pipedrive:
                return HttpResponse(404, {"success": False, "error": "Person not found"})
            return HttpResponse(200, {"success": True, "data": {"id": int(pid)}})
        return HttpResponse(404, {"success": False, "error": "no such route"})


def demo_service(fake: FakeCrms | None = None) -> CrmService:
    return CrmService(
        transport=fake or FakeCrms(),
        tokens={"hubspot": "demo-token", "attio": "demo-token", "pipedrive": "demo-token"},
        secrets=DEMO_SECRETS,
        public_base_url=PUBLIC_BASE_URL,
        budgets={
            crm: RateBudget(1000, 1.0, sleep=lambda _s: None)
            for crm in ("hubspot", "attio", "pipedrive")
        },
        options={"attio": WriteOptions(attio_object_id=PEOPLE_OBJECT_ID)},
        allow_fixtures=True,  # fake transport only: .example rows never reach a vendor
        sleep=lambda _s: None,
    )


app = create_app(demo_service())
