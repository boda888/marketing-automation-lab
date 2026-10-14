import json
from pathlib import Path

import pytest
from deliverability_preflight.api import create_app, load_fixture_resolver
from deliverability_preflight.dns import FakeResolver
from fastapi.testclient import TestClient

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
REQUEST = json.loads((EXAMPLES / "request.json").read_text())


@pytest.fixture()
def client():
    return TestClient(create_app(load_fixture_resolver(EXAMPLES / "dns_fixture.json")))


def test_healthz(client):
    assert client.get("/healthz").json() == {"status": "ok"}


def test_clean_example_passes(client):
    r = client.post("/v1/preflight", json=REQUEST)
    assert r.status_code == 200
    body = r.json()
    assert body["verdict"] == "pass", body["findings"]
    assert body["alignment"]["dkim_aligned"] is True


def test_missing_selector_blocks_with_stable_code(client):
    r = client.post("/v1/preflight", json={**REQUEST, "dkim_selectors": []})
    body = r.json()
    assert body["verdict"] == "block"
    assert "DKIM_NO_SELECTORS" in {f["code"] for f in body["findings"]}


def test_headers_and_complaints_are_used(client):
    req = {
        **REQUEST,
        "headers": [["List-Unsubscribe", "<mailto:u@acme.example>"]],
        "complaint_stats": [{"delivered": 10000, "complaints": 50}],
    }
    body = client.post("/v1/preflight", json=req).json()
    assert body["verdict"] == "block"
    assert body["unsubscribe"] is not None
    assert body["complaints"][0]["status"] == "stop"


def test_validation_error(client):
    assert client.post("/v1/preflight", json={"from_domain": ""}).status_code == 422
    bad = {**REQUEST, "complaint_stats": [{"delivered": 1, "complaints": 2}]}
    assert client.post("/v1/preflight", json=bad).status_code == 422


def test_dns_temp_failure_blocks_with_finding():
    resolver = FakeResolver({}, fail=[("_dmarc.acme.example", "TXT")])
    body = TestClient(create_app(resolver)).post("/v1/preflight", json=REQUEST).json()
    assert body["verdict"] == "block"
    assert "DMARC_DNS_TEMPERROR" in {f["code"] for f in body["findings"]}


def test_bad_fixture_key_rejected(tmp_path):
    p = tmp_path / "f.json"
    p.write_text('{"nonsense": []}')
    with pytest.raises(ValueError):
        load_fixture_resolver(p)


def test_n8n_workflow_matches_api_contract():
    wf = json.loads((EXAMPLES.parent / "n8n" / "preflight-before-send.json").read_text())
    names = [n["name"] for n in wf["nodes"]]
    assert len(names) == len(set(names))
    assert len({n["id"] for n in wf["nodes"]}) == len(names)
    for src, outputs in wf["connections"].items():
        assert src in names
        assert all(link["node"] in names for branch in outputs["main"] for link in branch)
    http = next(n for n in wf["nodes"] if n["type"] == "n8n-nodes-base.httpRequest")
    assert http["parameters"]["url"].endswith("/v1/preflight")
    switch = next(n for n in wf["nodes"] if n["type"] == "n8n-nodes-base.switch")
    outputs = [r["outputKey"] for r in switch["parameters"]["rules"]["values"]]
    assert outputs == ["pass", "review", "block"]  # branch order = connection order
    targets = [b[0]["node"] for b in wf["connections"]["Switch on verdict"]["main"]]
    assert len(targets) == 3 and len(set(targets)) == 3
    webhook = next(n for n in wf["nodes"] if n["type"] == "n8n-nodes-base.webhook")
    assert webhook["parameters"]["responseMode"] == "responseNode"


def test_content_lint_via_api(client):
    content = {"subject": "Hi {{first_name}}", "text": "Short note https://bit.ly/x"}
    body = client.post("/v1/preflight", json={**REQUEST, "content": content}).json()
    assert body["verdict"] == "block"
    found = {f["code"] for f in body["content"]["findings"]}
    assert {"CONTENT_UNRENDERED_MERGE_TAG", "CONTENT_LINK_SHORTENER"} <= found
