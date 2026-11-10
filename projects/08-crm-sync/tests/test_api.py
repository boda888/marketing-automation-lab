"""FastAPI: /v1/write routes per row; /v1/webhooks/{crm} checks origin on raw bytes first."""

import base64
import hashlib
import hmac
import json
from collections import deque

import pytest
from crmsync import HttpResponse, MemoryIdMap, RateBudget, WebhookSecrets, hubspot_v3_signature
from crmsync.api import CrmService, create_app
from crmsync.hubspot import trace_id
from fastapi.testclient import TestClient

NOW_MS = 1_762_790_400_000
BASE = "https://hooks.example"
PEOPLE_UUID = "0f6c3b9e-6d1a-4c55-9a0e-2b8d7f1e4a10"
SECRETS = WebhookSecrets(
    hubspot_client_secret="hs-secret",
    attio_secret="attio-secret",
    pipedrive_user="pd",
    pipedrive_password="pw",
)


class Fake:
    def __init__(self, *script):
        self.script = deque(script)
        self.calls = []

    def send(self, call):
        self.calls.append(call)
        return self.script.popleft()


def make(fake=None, *, tokens=None, idmap=None, api_token=None):
    budgets = {
        crm: RateBudget(100, 1.0, clock=lambda: 0.0, sleep=lambda s: None)
        for crm in ("hubspot", "attio", "pipedrive")
    }
    service = CrmService(
        transport=fake or Fake(),
        tokens=tokens if tokens is not None else {"hubspot": "t", "pipedrive": "t"},
        secrets=SECRETS,
        public_base_url=BASE,
        idmap=idmap if idmap is not None else MemoryIdMap(),
        budgets=budgets,
        allow_fixtures=True,
        now_ms=lambda: NOW_MS,
        sleep=lambda s: None,
    )
    return service, TestClient(create_app(service, api_token=api_token))


def hubspot_post(client, body: bytes, *, path="/v1/webhooks/hubspot", ts=NOW_MS, secret=None):
    signature = hubspot_v3_signature(secret or "hs-secret", "POST", BASE + path, body, str(ts))
    headers = {
        "X-HubSpot-Signature-v3": signature,
        "X-HubSpot-Request-Timestamp": str(ts),
        "Content-Type": "application/json",
    }
    return client.post(path, content=body, headers=headers)


def merge_body(event_id=1, attempt=0, primary=202, merged=(101,)):
    return json.dumps(
        [
            {
                "eventId": event_id,
                "attemptNumber": attempt,
                "subscriptionType": "contact.merge",
                "primaryObjectId": primary,
                "mergedObjectIds": list(merged),
            }
        ]
    ).encode()


def test_write_returns_one_outcome_per_row_and_records_ids():
    body = {
        "status": "COMPLETE",
        "results": [{"id": "101", "_new": True, "objectWriteTraceId": trace_id("a@co.example")}],
        "errors": [],
    }
    service, client = make(Fake(HttpResponse(207, body)))
    resp = client.post(
        "/v1/write",
        json={
            "crm": "hubspot",
            "rows": [
                {"row_id": "r1", "email": "A@co.example", "properties": {"firstname": "Ann"}},
                {"row_id": "r2", "email": None},
            ],
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert [(o["row_id"], o["outcome"]) for o in data["outcomes"]] == [
        ("r1", "created"),
        ("r2", "skipped"),
    ]
    assert data["idmap_recorded"] == 1
    assert service.idmap.get("hubspot:contact", "a@co.example") == "101"


def test_write_needs_bearer_token_when_configured():
    _, client = make(api_token="lab-token")
    req = {"crm": "hubspot", "rows": [{"row_id": "r1", "email": "a@co.example"}]}
    assert client.post("/v1/write", json=req).status_code == 401
    wrong = {"Authorization": "Bearer nope"}
    assert client.post("/v1/write", json=req, headers=wrong).status_code == 401


def test_write_unconfigured_crm_is_404_and_bad_request_is_422_before_any_call():
    fake = Fake()
    _, client = make(fake, tokens={"hubspot": "t", "attio": "t"})
    row = {"row_id": "r1", "email": "a@co.example"}
    assert client.post("/v1/write", json={"crm": "pipedrive", "rows": [row]}).status_code == 404
    # Attio with the shared IdMap but only the "people" slug: refused, nothing sent.
    resp = client.post("/v1/write", json={"crm": "attio", "rows": [row]})
    assert resp.status_code == 422 and "attio_object_id" in resp.json()["detail"]
    dup = client.post("/v1/write", json={"crm": "hubspot", "rows": [row, row]})
    assert dup.status_code == 422 and "not unique" in dup.json()["detail"]
    assert fake.calls == []


def test_signed_hubspot_merge_remaps_and_redelivery_is_deduped_on_event_id():
    idmap = MemoryIdMap()
    idmap.put("hubspot:contact", "a@co.example", "101")
    service, client = make(idmap=idmap)

    first = hubspot_post(client, merge_body(event_id=7, attempt=0))
    assert first.status_code == 200
    assert first.json()["merge"]["remapped"] == 1
    assert idmap.get("hubspot:contact", "a@co.example") == "202"

    # Same eventId, next attemptNumber, freshly signed: acknowledged, not re-applied.
    again = hubspot_post(client, merge_body(event_id=7, attempt=1), ts=NOW_MS + 1000)
    assert again.status_code == 200
    assert again.json()["duplicates"] == 1 and again.json()["merge"]["events"] == 0


def test_hubspot_signature_is_over_the_public_url_not_the_test_host():
    _, client = make()
    body = merge_body()
    wrong_url = hubspot_v3_signature(
        "hs-secret", "POST", "http://testserver/v1/webhooks/hubspot", body, str(NOW_MS)
    )
    headers = {"X-HubSpot-Signature-v3": wrong_url, "X-HubSpot-Request-Timestamp": str(NOW_MS)}
    resp = client.post("/v1/webhooks/hubspot", content=body, headers=headers)
    assert resp.status_code == 401 and resp.json() == {"error": "bad_signature"}


@pytest.mark.parametrize(
    ("kwargs", "status", "reason"),
    [
        ({"secret": "forged"}, 401, "bad_signature"),
        ({"ts": NOW_MS - 301_000}, 401, "stale_timestamp"),
    ],
)
def test_refused_hubspot_delivery_never_touches_the_idmap(kwargs, status, reason):
    idmap = MemoryIdMap()
    idmap.put("hubspot:contact", "a@co.example", "101")
    _, client = make(idmap=idmap)
    resp = hubspot_post(client, merge_body(), **kwargs)
    assert (resp.status_code, resp.json()["error"]) == (status, reason)
    assert idmap.get("hubspot:contact", "a@co.example") == "101"


def test_missing_secret_answers_500_so_the_crm_retries_later():
    service, client = make()
    service.secrets = WebhookSecrets()
    resp = hubspot_post(client, merge_body())
    assert resp.status_code == 500 and resp.json()["error"] == "not_configured"


def test_signed_attio_merge_and_unsigned_one():
    idmap = MemoryIdMap()
    idmap.put(f"attio:{PEOPLE_UUID}", "a@co.example", "rec-a")
    _, client = make(idmap=idmap)
    body = json.dumps(
        {
            "webhook_id": "wh",
            "events": [
                {
                    "event_type": "record.merged",
                    "id": {"object_id": PEOPLE_UUID, "record_id": "rec-b"},
                    "duplicate_record_id": "rec-a",
                }
            ],
        }
    ).encode()
    assert client.post("/v1/webhooks/attio", content=body).status_code == 401
    sig = hmac.new(b"attio-secret", body, hashlib.sha256).hexdigest()
    resp = client.post("/v1/webhooks/attio", content=body, headers={"Attio-Signature": sig})
    assert resp.status_code == 200 and resp.json()["merge"]["remapped"] == 1
    assert idmap.get(f"attio:{PEOPLE_UUID}", "a@co.example") == "rec-b"


def test_pipedrive_basic_auth_merge_and_invalid_json():
    idmap = MemoryIdMap()
    idmap.put("pipedrive:person", "a@co.example", "5")
    _, client = make(idmap=idmap)
    auth = {"Authorization": "Basic " + base64.b64encode(b"pd:pw").decode()}
    body = {"meta": {"action": "change", "entity": "person", "entity_id": "9"}}
    body["data"] = {"id": 9, "merged_from_id": 5}
    resp = client.post("/v1/webhooks/pipedrive", content=json.dumps(body), headers=auth)
    assert resp.status_code == 200 and resp.json()["merge"]["remapped"] == 1
    assert idmap.get("pipedrive:person", "a@co.example") == "9"
    bad = client.post("/v1/webhooks/pipedrive", content=b"{not json", headers=auth)
    assert bad.status_code == 400 and bad.json() == {"error": "invalid_json"}


def test_unknown_crm_webhook_and_non_https_base_url_are_refused():
    _, client = make()
    assert client.post("/v1/webhooks/salesforce", content=b"{}").status_code == 401
    with pytest.raises(ValueError, match="https"):
        CrmService(transport=Fake(), tokens={}, secrets=SECRETS, public_base_url="http://x.example")


def test_healthz_lists_configured_crms_only():
    _, client = make(tokens={"pipedrive": "t"})
    assert client.get("/healthz").json()["crms"] == ["pipedrive"]
