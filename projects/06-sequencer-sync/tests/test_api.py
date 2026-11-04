"""/v1/push and /v1/reconcile over the offline demo (fake Smartlead) and custom wirings."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sequencer import Campaign, InMemoryPushLog, LemlistAdapter, PushState, idempotency_key
from sequencer.api import PushService, VendorWiring, create_app
from sequencer.demo import demo_service
from sequencer.fakes import FakeTransport
from sequencer.push import HttpResponse

ROOT = Path(__file__).resolve().parents[1]
DATASET = json.loads((ROOT / "examples" / "demo-push.json").read_text())


@pytest.fixture
def service():
    return demo_service()


@pytest.fixture
def client(service):
    return TestClient(create_app(service))


def outcomes(body):
    return {o["lead_id"]: (o["outcome"], o["reason"]) for o in body["leads"]}


def push(client, leads, **extra):
    payload = {
        "vendor": "smartlead",
        "campaign_name": "Q4 Logistics DE",
        "dedupe_scope": "campaign",
        "leads": leads,
        **extra,
    }
    return client.post("/v1/push", json=payload)


def test_demo_dataset_one_outcome_per_lead(client, service):
    resp = client.post("/v1/push", json=DATASET)
    assert resp.status_code == 200
    body = resp.json()
    assert body["campaign"]["campaign_id"] == "101" and body["membership_checked"] is True
    assert outcomes(body) == {
        "lead-001": ("sent", "accepted"),
        "lead-002": ("sent", "accepted"),
        "lead-003": ("skipped", "duplicate_in_batch"),
        "lead-004": ("skipped", "suppressed"),
        "lead-005": ("skipped", "suppressed"),
        "lead-006": ("skipped", "missing_variables"),
        "lead-007": ("skipped", "missing_email"),
    }
    assert service.transport.members["101"] == [
        "ana.ruiz@northwind.example",
        "ben.okafor@contoso.example",
    ]
    # same batch again: the push log says already pushed, nothing is posted
    posts_before = sum(r.method == "POST" for r in service.transport.requests)
    again = outcomes(client.post("/v1/push", json=DATASET).json())
    assert again["lead-001"] == ("skipped", "already_pushed")
    assert sum(r.method == "POST" for r in service.transport.requests) == posts_before


def test_member_read_blocks_lead_already_in_campaign(client, service):
    service.transport.members["101"] = ["ben@contoso.example"]
    body = push(client, [{"lead_id": "b", "email": "BEN@contoso.example"}]).json()
    assert outcomes(body) == {"b": ("skipped", "already_in_campaign")}


def test_lost_response_then_reconcile_confirms(client, service):
    lead = {"lead_id": "x1", "email": "lost+eva@woodgrove.example"}
    body = push(client, [lead]).json()
    assert outcomes(body) == {"x1": ("outcome_unknown", "outcome_unknown")}
    # next run holds the lead instead of pushing it twice
    assert outcomes(push(client, [lead]).json()) == {"x1": ("skipped", "push_pending")}
    settled = client.post(
        "/v1/reconcile",
        json={"vendor": "smartlead", "campaign_name": "Q4 Logistics DE", "leads": [lead]},
    ).json()
    assert settled["confirmed"] == ["x1"] and settled["cleared"] == []
    assert outcomes(push(client, [lead]).json()) == {"x1": ("skipped", "already_pushed")}


def test_503_then_reconcile_clears_and_lead_can_be_pushed(client, service):
    lead = {"lead_id": "d1", "email": "down+raj@adatum.example"}
    assert outcomes(push(client, [lead]).json())["d1"][0] == "outcome_unknown"
    settled = client.post(
        "/v1/reconcile",
        json={"vendor": "smartlead", "campaign_name": "Q4 Logistics DE", "leads": [lead]},
    ).json()
    assert settled["cleared"] == ["d1"] and settled["confirmed"] == []
    key = idempotency_key("smartlead", "101", "down+raj@adatum.example")
    assert service.push_log.state(key) is None


def test_reconcile_ignores_leads_without_reservation(client):
    settled = client.post(
        "/v1/reconcile",
        json={
            "vendor": "smartlead",
            "campaign_name": "Q4 Logistics DE",
            "leads": [{"lead_id": "n", "email": "never@pushed.example"}],
        },
    ).json()
    assert settled["not_pending"] == ["n"] and settled["members_read"] == 0


@pytest.mark.parametrize(
    ("name", "status"),
    [("Q1 Retail", 409), ("Q3 Missing", 404), ("q4 logistics de ", 200)],
)
def test_campaign_by_exact_name(client, name, status):
    resp = push(client, [{"lead_id": "a", "email": "a@b.example"}], campaign_name=name)
    assert resp.status_code == status


def test_dedupe_scope_is_required_and_vendor_must_be_wired(client):
    lead = [{"lead_id": "a", "email": "a@b.example"}]
    no_scope = {"vendor": "smartlead", "campaign_name": "Q4 Logistics DE", "leads": lead}
    assert client.post("/v1/push", json=no_scope).status_code == 422
    assert push(client, lead, vendor="instantly").status_code == 404


def test_incomplete_member_read_pushes_nothing():
    service = demo_service()
    service.transport = FakeTransport([HttpResponse(401, {"message": "bad key"})])
    client = TestClient(create_app(service))
    resp = push(client, [{"lead_id": "a", "email": "a@b.example"}])
    assert resp.status_code == 502
    assert [r.method for r in service.transport.requests] == ["GET"]
    assert service.push_log.states == {}


def test_bearer_token():
    client = TestClient(create_app(demo_service(), api_token="s3cret"))
    lead = [{"lead_id": "a", "email": "a@b.example"}]
    assert push(client, lead).status_code == 401
    ok = client.post(
        "/v1/push",
        headers={"Authorization": "Bearer s3cret"},
        json={**DATASET, "leads": lead},
    )
    assert ok.status_code == 200
    assert client.get("/healthz").status_code == 200  # health stays open


def test_lemlist_without_reader_pushes_but_cannot_reconcile():
    transport = FakeTransport()
    service = PushService(
        vendors={
            "lemlist": VendorWiring(
                adapter=LemlistAdapter("k"),
                list_campaigns=lambda: [Campaign(campaign_id="cam_1", name="Q4 Logistics DE")],
            )
        },
        transport=transport,
        push_log=InMemoryPushLog(),
        allow_fixtures=True,
    )
    client = TestClient(create_app(service))
    body = push(client, [{"lead_id": "a", "email": "a@b.example"}], vendor="lemlist").json()
    assert body["membership_checked"] is False
    assert outcomes(body) == {"a": ("sent", "accepted")}
    key = idempotency_key("lemlist", "cam_1", "a@b.example")
    assert service.push_log.state(key) is PushState.COMMITTED
    resp = client.post(
        "/v1/reconcile",
        json={"vendor": "lemlist", "campaign_name": "Q4 Logistics DE", "leads": [{"lead_id": "a"}]},
    )
    assert resp.status_code == 501
