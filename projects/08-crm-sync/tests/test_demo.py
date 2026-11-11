"""Offline demo: every outcome branch reachable on all three fake CRMs, merges re-point ids."""

import json
from pathlib import Path

import pytest
from crmsync import hubspot_v3_signature
from crmsync.api import create_app
from crmsync.demo import DEMO_SECRETS, PEOPLE_OBJECT_ID, PUBLIC_BASE_URL, FakeCrms, demo_service
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
DATASET = json.loads((ROOT / "examples" / "demo-write.json").read_text())


def client(fake=None):
    service = demo_service(fake)
    return service, TestClient(create_app(service))


def by_row(body):
    return {o["row_id"]: (o["outcome"], o["reason"], o["retryable"]) for o in body["outcomes"]}


def test_demo_dataset_gets_one_outcome_per_row_in_order():
    fake = FakeCrms()
    _, c = client(fake)
    body = c.post("/v1/write", json=DATASET).json()
    assert [o["row_id"] for o in body["outcomes"]] == [r["row_id"] for r in DATASET["rows"]]
    rows = by_row(body)
    assert rows["sheet-2"][0] == rows["sheet-3"][0] == "created"
    assert rows["sheet-4"] == ("skipped", "duplicate_key:first_row=sheet-2", False)
    assert rows["sheet-5"] == ("skipped", "missing_key:email", False)
    assert rows["sheet-6"][0] == "failed" and rows["sheet-6"][2] is False
    assert rows["sheet-7"] == ("failed", "not_in_response", True)
    assert rows["sheet-8"][0] == "skipped" and rows["sheet-8"][1].startswith("invalid_property")
    assert body["idmap_recorded"] == 2
    sent = fake.calls[0].json["inputs"]
    ben = next(i for i in sent if i["id"] == "ben.okafor@contoso.example")
    assert "jobtitle" not in ben["properties"]  # "Unknown" never blanks a CRM field


def test_second_post_of_same_rows_updates_instead_of_creating():
    _, c = client()
    c.post("/v1/write", json=DATASET)
    rows = by_row(c.post("/v1/write", json=DATASET).json())
    assert rows["sheet-2"][0] == rows["sheet-3"][0] == "updated"


def test_attio_demo_upserts_refuses_and_keeps_retryable_apart():
    service, c = client()
    rows = [
        {"row_id": "a1", "email": "ana.ruiz@northwind.example", "properties": {"job_title": "COO"}},
        {"row_id": "a2", "email": "reject+kim@fabrikam.example", "properties": {}},
        {"row_id": "a3", "email": "busy+dana@tailspin.example", "properties": {}},
    ]
    body = c.post("/v1/write", json={"crm": "attio", "rows": rows}).json()
    out = by_row(body)
    assert out["a1"][0] == "upserted"
    assert out["a2"] == ("failed", "http_400 value_not_found: Unknown option", False)
    assert out["a3"] == ("failed", "http_503", True)
    rec = body["outcomes"][0]["crm_id"]
    assert service.idmap.get(f"attio:{PEOPLE_OBJECT_ID}", "ana.ruiz@northwind.example") == rec


def test_pipedrive_demo_update_create_ambiguous_and_retryable_search():
    fake = FakeCrms()
    _, c = client(fake)
    rows = [
        {"row_id": "p1", "email": "ana.ruiz@northwind.example", "properties": {"name": "Ana"}},
        {"row_id": "p2", "email": "ben@contoso.example", "properties": {"name": "Ben"}},
        {"row_id": "p3", "email": "office@contoso.example", "properties": {"name": "Desk"}},
        {"row_id": "p4", "email": "reject+kim@fabrikam.example", "properties": {"name": "K"}},
        {"row_id": "p5", "email": "busy+dana@tailspin.example", "properties": {"name": "D"}},
    ]
    out = by_row(c.post("/v1/write", json={"crm": "pipedrive", "rows": rows}).json())
    assert out["p1"] == ("updated", None, False)
    assert out["p2"] == ("created", None, False)
    assert out["p3"] == ("failed", "ambiguous_match:502,503", False)
    assert out["p4"] == ("failed", "http_400: Invalid field value", False)
    assert out["p5"][0] == "failed" and out["p5"][2] is True
    posts = [call for call in fake.calls if call.method == "POST"]
    assert len(posts) == 2  # ben + rejected kim; the 503 search never led to a blind create


def test_signed_merge_on_demo_repoints_the_written_contact():
    service, c = client()
    c.post("/v1/write", json=DATASET)
    loser = service.idmap.get("hubspot:contact", "ana.ruiz@northwind.example")
    body = json.dumps(
        [
            {
                "eventId": 7,
                "attemptNumber": 0,
                "subscriptionType": "contact.merge",
                "primaryObjectId": 4242,
                "mergedObjectIds": [int(loser)],
            }
        ]
    ).encode()
    ts = str(service.now_ms())
    path = "/v1/webhooks/hubspot"
    sig = hubspot_v3_signature(
        DEMO_SECRETS.hubspot_client_secret, "POST", PUBLIC_BASE_URL + path, body, ts
    )
    headers = {"X-HubSpot-Signature-v3": sig, "X-HubSpot-Request-Timestamp": ts}
    assert c.post(path, content=body, headers=headers).status_code == 200
    assert service.idmap.get("hubspot:contact", "ana.ruiz@northwind.example") == "4242"


@pytest.mark.parametrize("host", ["api.hubapi.com", "api.attio.com", "api.pipedrive.com"])
def test_fake_is_reached_on_the_writers_default_hosts_only(host):
    fake = FakeCrms()
    _, c = client(fake)
    crm = {"api.hubapi.com": "hubspot", "api.attio.com": "attio"}.get(host, "pipedrive")
    row = {"row_id": "x", "email": "ana.ruiz@northwind.example", "properties": {"name": "A"}}
    if crm == "hubspot":
        row["properties"] = {"firstname": "A"}
    if crm == "attio":
        row["properties"] = {}
    c.post("/v1/write", json={"crm": crm, "rows": [row]})
    assert fake.calls and all(f"//{host}/" in call.url for call in fake.calls)
