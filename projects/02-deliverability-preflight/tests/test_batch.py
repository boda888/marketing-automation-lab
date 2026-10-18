import json
from datetime import date
from pathlib import Path

from deliverability_preflight import Lead, Mailbox, assign_batch, plan_capacity
from deliverability_preflight.api import create_app
from deliverability_preflight.dns import FakeResolver
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
ON = date(2026, 10, 20)
STEADY = date(2026, 9, 1)  # warmed and ramped by ON: 30/day


def _leads(n, prefix="L"):
    return [Lead(id=f"{prefix}-{i:03d}", email=f"p{i}@lead.example") for i in range(n)]


def _plan(*mailboxes, target=0):
    return plan_capacity(list(mailboxes), ON, target)


def codes(batch):
    return {f.code for f in batch.findings}


def test_round_robin_spreads_leads_over_mailboxes():
    a = Mailbox(address="a@acme.example", warmup_started=STEADY)
    b = Mailbox(address="b@acme.example", warmup_started=STEADY)
    batch = assign_batch(_plan(a, b), _leads(5))
    assert [x.mailbox[0] for x in batch.assignments] == ["a", "b", "a", "b", "a"]
    assert batch.by_mailbox == {"a@acme.example": 3, "b@acme.example": 2}
    assert batch.deferred == [] and batch.findings == []


def test_capped_at_capacity_today_rest_deferred_in_order():
    a = Mailbox(address="a@acme.example", warmup_started=STEADY, daily_cap=2)
    ramp = Mailbox(address="r@acme.example", warmup_started=date(2026, 10, 6))  # day 0: 5
    batch = assign_batch(_plan(a, ramp), _leads(10))
    assert batch.capacity_today == 7 == len(batch.assignments)
    assert batch.by_mailbox == {"a@acme.example": 2, "r@acme.example": 5}
    # once a mailbox is full the others keep taking leads
    assert [x.mailbox[0] for x in batch.assignments] == ["a", "r", "a", "r", "r", "r", "r"]
    assert batch.deferred == ["L-007", "L-008", "L-009"]
    assert "BATCH_DEFERRED" in codes(batch)


def test_paused_and_warming_mailboxes_get_nothing():
    paused = Mailbox(address="p@acme.example", warmup_started=STEADY, complaint_status="stop")
    warming = Mailbox(address="w@acme.example", warmup_started=date(2026, 10, 15))
    batch = assign_batch(_plan(paused, warming), _leads(3))
    assert batch.assignments == [] and batch.capacity_today == 0
    assert batch.deferred == ["L-000", "L-001", "L-002"]
    assert "BATCH_NO_CAPACITY_TODAY" in codes(batch)
    assert "BATCH_DEFERRED" not in codes(batch)


def test_duplicate_id_or_email_is_skipped_not_fatal():
    a = Mailbox(address="a@acme.example", warmup_started=STEADY)
    leads = [
        Lead(id="1", email="dana@northwind.example"),
        Lead(id="1", email="other@northwind.example"),  # same id
        Lead(id="2", email=" Dana@Northwind.example"),  # same person under another id
        Lead(id="3"),
        Lead(id="4"),  # no email: keyed by id only
    ]
    batch = assign_batch(_plan(a), leads)
    assert [x.lead_id for x in batch.assignments] == ["1", "3", "4"]
    assert batch.duplicates == ["1", "2"]
    assert "BATCH_DUPLICATE_LEAD" in codes(batch)


def test_no_leads_no_findings():
    a = Mailbox(address="a@acme.example", warmup_started=STEADY)
    batch = assign_batch(_plan(a), [])
    assert batch.assignments == [] and batch.deferred == [] and batch.findings == []


def test_schedule_endpoint_example():
    req = json.loads((ROOT / "examples" / "schedule_request.json").read_text())
    body = TestClient(create_app(FakeResolver({}))).post("/v1/schedule", json=req).json()
    # sam steady 30, lee ramp day 5 -> 15, ana still warming; 3 unique leads
    assert body["capacity_today"] == 45
    assert body["plan"]["target_daily"] == 3
    assert [a["lead_id"] for a in body["assignments"]] == ["L-001", "L-002", "L-003"]
    assert {a["mailbox"] for a in body["assignments"]} == {
        "sam@acme-outreach.example",
        "lee@acme-outreach.example",
    }
    assert body["duplicates"] == ["L-001", "L-004"]
    found = {f["code"] for f in body["findings"]}
    assert {"CAPACITY_MAILBOX_WARMING", "BATCH_DUPLICATE_LEAD"} <= found


def test_schedule_endpoint_validates_leads():
    client = TestClient(create_app(FakeResolver({})))
    bad = {"mailboxes": [], "leads": [{"id": ""}]}
    assert client.post("/v1/schedule", json=bad).status_code == 422


def test_n8n_capacity_workflow_matches_api_contract():
    wf = json.loads((ROOT / "n8n" / "capacity-before-schedule.json").read_text())
    nodes = {n["name"]: n for n in wf["nodes"]}
    assert len(nodes) == len(wf["nodes"])
    assert len({n["id"] for n in wf["nodes"]}) == len(nodes)
    for src, outputs in wf["connections"].items():
        assert src in nodes
        assert all(link["node"] in nodes for branch in outputs["main"] for link in branch)
    http = next(n for n in wf["nodes"] if n["type"] == "n8n-nodes-base.httpRequest")
    assert http["parameters"]["url"].endswith("/v1/schedule")
    assert http["retryOnFail"] is True and http["maxTries"] >= 2
    for field in ("mailboxes", "leads"):
        assert f"$json.body.{field}" in http["parameters"]["jsonBody"]
    gate = next(n for n in wf["nodes"] if n["type"] == "n8n-nodes-base.if")
    cond = gate["parameters"]["conditions"]["conditions"][0]
    assert "assignments.length" in cond["leftValue"] and cond["operator"]["operation"] == "gt"
    true_branch, false_branch = wf["connections"][gate["name"]]["main"]
    assert '"schedule"' in nodes[true_branch[0]["node"]]["parameters"]["responseBody"]
    assert '"hold"' in nodes[false_branch[0]["node"]]["parameters"]["responseBody"]
    webhook = next(n for n in wf["nodes"] if n["type"] == "n8n-nodes-base.webhook")
    assert webhook["parameters"]["responseMode"] == "responseNode"
