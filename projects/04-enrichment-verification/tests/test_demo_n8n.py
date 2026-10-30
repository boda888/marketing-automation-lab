import json
import re
from pathlib import Path

from enrichment import NextAction
from enrichment.api import EnrichBatchRequest
from enrichment.demo import app, demo_pipeline
from enrichment.pipeline import EMAIL_DOMAIN_MISMATCH, OUTCOME_UNKNOWN, BatchResult, PersonResult
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
DATASET = json.loads((ROOT / "examples" / "demo-batch.json").read_text())
WF = json.loads((ROOT / "n8n" / "enrich-and-route.json").read_text())
NODES = {n["name"]: n for n in WF["nodes"]}


def by_type(node_type):
    return [n for n in WF["nodes"] if n["type"] == node_type]


def targets(name, output):
    return [link["node"] for link in WF["connections"][name]["main"][output]]


def http_node():
    (node,) = by_type("n8n-nodes-base.httpRequest")
    return node


def switch_node():
    (node,) = by_type("n8n-nodes-base.switch")
    return node


def route(action):
    """Where the Switch sends an item with this action (first matching rule, else fallback)."""
    switch = switch_node()
    rules = switch["parameters"]["rules"]["values"]
    for i, rule in enumerate(rules):
        if rule["conditions"]["conditions"][0]["rightValue"] == action:
            return targets(switch["name"], i)
    return targets(switch["name"], len(rules))


def demo_batch():
    resp = TestClient(app).post("/v1/enrich/batch", json=DATASET)
    assert resp.status_code == 200
    return resp.json()


def test_dataset_is_fictional_and_valid():
    EnrichBatchRequest.model_validate(DATASET)
    for cand in DATASET["candidates"]:
        for key, value in cand["fields"].items():
            if key in ("email", "company_domain"):
                assert value.endswith(".example"), value


def test_demo_batch_produces_every_action():
    body = demo_batch()
    assert body["candidates_in"] == 8 and body["people"] == 7
    assert set(body["actions"]) == {a.value for a in NextAction}
    assert all(count >= 1 for count in body["actions"].values())
    results = {r["person"]["person_id"]: r for r in body["results"]}
    assert results["ada-apollo"]["action"] == "send"
    assert results["ada-apollo"]["person"]["candidate_ids"] == ["ada-apollo", "ada-csv"]
    assert results["bob-apollo"]["action"] == "verify"  # an address without a check
    assert results["cara-linkedin"]["findings"] == [EMAIL_DOMAIN_MISMATCH]
    assert results["jo-apollo"]["findings"] == [OUTCOME_UNKNOWN]
    assert (results["eve-csv"]["action"], results["dan-csv"]["action"]) == ("suppress", "no_email")


def test_demo_provider_is_free_and_offline():
    pipe = demo_pipeline()
    assert all(not p.paid for p in pipe.waterfall.providers)


def test_structure_is_valid():
    assert len(NODES) == len(WF["nodes"])
    assert len({n["id"] for n in WF["nodes"]}) == len(NODES)
    for src, outputs in WF["connections"].items():
        assert src in NODES
        assert all(link["node"] in NODES for branch in outputs["main"] for link in branch)
    (hook,) = by_type("n8n-nodes-base.webhook")
    assert hook["parameters"]["responseMode"] == "responseNode"


def test_http_call_matches_the_api_and_never_retries():
    http = http_node()
    path = http["parameters"]["url"].split("}}", 1)[1]
    assert path in {route.path for route in app.routes}
    sent = set(re.findall(r"(\w+): \$json\.body\.", http["parameters"]["jsonBody"]))
    assert sent == set(EnrichBatchRequest.model_fields)
    # The call can bill paid finders: a retry after a timeout buys the same lookups twice.
    assert http.get("retryOnFail") is not True
    assert http["parameters"]["options"]["timeout"] >= 60_000
    # A failed call answers the webhook and routes nobody.
    assert http["onError"] == "continueErrorOutput"
    (failed,) = targets(http["name"], 1)
    assert NODES[failed]["type"] == "n8n-nodes-base.respondToWebhook"
    assert NODES[failed]["parameters"]["options"]["responseCode"] >= 500
    assert len(WF["connections"][http["name"]]["main"]) == 2


def test_summary_and_split_read_fields_the_api_returns():
    http = http_node()
    ok = targets(http["name"], 0)
    (respond,) = [n for n in ok if NODES[n]["type"] == "n8n-nodes-base.respondToWebhook"]
    for field in re.findall(r"\$json\.(\w+)", NODES[respond]["parameters"]["responseBody"]):
        assert field in BatchResult.model_fields
    (split,) = by_type("n8n-nodes-base.splitOut")
    assert split["name"] in ok
    assert split["parameters"]["fieldToSplitOut"] == "results" in BatchResult.model_fields
    assert targets(split["name"], 0) == [switch_node()["name"]]


def test_switch_has_one_output_per_action_plus_fallback():
    switch = switch_node()
    rules = switch["parameters"]["rules"]["values"]
    assert sorted(r["conditions"]["conditions"][0]["rightValue"] for r in rules) == sorted(
        a.value for a in NextAction
    )
    for r in rules:
        cond = r["conditions"]["conditions"][0]
        assert cond["leftValue"] == "={{ $json.action }}"
        assert "action" in PersonResult.model_fields
        assert cond["operator"] == {"type": "string", "operation": "equals"}
        assert r["conditions"]["options"]["caseSensitive"] is True
    assert switch["parameters"]["options"]["fallbackOutput"] == "extra"
    assert len(WF["connections"][switch["name"]]["main"]) == len(rules) + 1


def test_only_send_reaches_the_sequencer_and_unknown_goes_to_review():
    sequencer = route("send")
    assert all("sequencer" in name.lower() for name in sequencer)
    for action in ("verify", "review", "suppress", "no_email", "SEND", "unexpected"):
        assert not set(route(action)) & set(sequencer), action
    assert route("unexpected") == route("review")
    assert all("review" in name.lower() for name in route("review"))


def test_every_demo_person_lands_on_the_queue_for_its_action():
    expected = {
        "send": "sequencer",
        "verify": "verify",
        "review": "review",
        "suppress": "suppress",
        "no_email": "no address",
    }
    for result in demo_batch()["results"]:
        (queue,) = route(result["action"])
        assert expected[result["action"]] in queue.lower()
