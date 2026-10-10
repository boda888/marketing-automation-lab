import csv
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from icp_scoring.api import MAX_BATCH, create_app
from icp_scoring.cli import main

ROOT = Path(__file__).resolve().parents[1]

ACCOUNT = {
    "company": {
        "domain": "northwind.example",
        "industry": "Software",
        "employee_count": 120,
        "annual_revenue_usd": 15000000,
        "country": "US",
        "technologies": ["HubSpot"],
    },
    "events": [{"type": "demo_request", "occurred_at": "2026-10-09T10:00:00Z", "contact_id": "a"}],
    "as_of": "2026-10-10T10:00:00Z",
}


@pytest.fixture(scope="module")
def client():
    return TestClient(create_app())


def test_health_and_config(client):
    assert client.get("/healthz").json()["status"] == "ok"
    assert client.get("/v1/config").json()["version"].startswith("example")


def test_score_endpoint(client):
    r = client.post("/v1/score", json=ACCOUNT)
    assert r.status_code == 200
    body = r.json()
    assert body["tier"] in (1, 2) and body["fit_score"] > 70
    assert {c["signal"] for c in body["contributions"]} == {
        "employees",
        "revenue_usd",
        "industry",
        "country",
        "technographics",
    }


def test_score_validation_error(client):
    r = client.post("/v1/score", json={"company": {"domain": "x.io", "employee_count": -1}})
    assert r.status_code == 422


def test_batch_endpoint_and_limit(client):
    r = client.post("/v1/score/batch", json={"items": [ACCOUNT, ACCOUNT]})
    assert r.status_code == 200
    assert sum(r.json()["tier_counts"].values()) == 2
    big = {"items": [{"company": {"domain": "a.io"}}] * (MAX_BATCH + 1)}
    assert client.post("/v1/score/batch", json=big).status_code == 413


def test_cli_scores_example_csv(tmp_path):
    out = tmp_path / "scored.csv"
    assert main([str(ROOT / "examples" / "accounts.csv"), "-o", str(out)]) == 0
    rows = {r["domain"]: r for r in csv.DictReader(out.open())}
    assert rows["northwind-saas.example"]["tier"] in {"2"}  # no events in CSV -> not tier 1
    assert rows["casino-royale.example"]["disqualified"] == "True"
    assert rows["state-agency.gov"]["disqualified"] == "True"
    assert rows["unknown-co.example"]["needs_enrichment"] == "True"
    assert float(rows["bigcorp.example"]["fit_score"]) < float(
        rows["northwind-saas.example"]["fit_score"]
    )


def test_n8n_workflow_is_well_formed():
    wf = json.loads((ROOT / "n8n" / "icp-scoring-webhook.json").read_text())
    names = [n["name"] for n in wf["nodes"]]
    assert len(names) == len(set(names))
    assert len({n["id"] for n in wf["nodes"]}) == len(names)
    for node in wf["nodes"]:
        assert node["type"].startswith("n8n-nodes-base.")
        assert isinstance(node["typeVersion"], (int, float))
        assert len(node["position"]) == 2
    for src, outputs in wf["connections"].items():
        assert src in names
        for branch in outputs["main"]:
            for link in branch:
                assert link["node"] in names
    http = next(n for n in wf["nodes"] if n["type"] == "n8n-nodes-base.httpRequest")
    assert http["parameters"]["url"].endswith("/v1/score")
    webhook = next(n for n in wf["nodes"] if n["type"] == "n8n-nodes-base.webhook")
    assert webhook["parameters"]["responseMode"] == "responseNode"
    assert any(n["type"] == "n8n-nodes-base.respondToWebhook" for n in wf["nodes"])
