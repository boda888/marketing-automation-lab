"""n8n/write-and-route.json checked against the API models (structure only, no live n8n)."""

import json
import re
from pathlib import Path

from crmsync import Outcome
from crmsync.api import WriteRequest, create_app
from crmsync.demo import demo_service
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
WF = json.loads((ROOT / "n8n" / "write-and-route.json").read_text())
NODES = {n["name"]: n for n in WF["nodes"]}
DATASET = json.loads((ROOT / "examples" / "demo-write.json").read_text())


def of_type(suffix):
    return [n for n in WF["nodes"] if n["type"] == f"n8n-nodes-base.{suffix}"]


def targets(name, output=0):
    return [link["node"] for link in WF["connections"][name]["main"][output]]


def writes():
    return [n for n in of_type("httpRequest") if n["parameters"]["url"].endswith("/v1/write")]


def body_keys(node):
    body = node["parameters"]["jsonBody"].split("JSON.stringify({", 1)[1]
    return set(re.findall(r"(?:^|, )\s*(\w+):", body))


def route(outcome, retryable):
    (switch,) = of_type("switch")
    rules = switch["parameters"]["rules"]["values"]
    for i, rule in enumerate(rules):
        if rule["conditions"]["conditions"][0]["rightValue"] == f"{outcome}:{retryable}".lower():
            return targets(switch["name"], i)
    return targets(switch["name"], len(rules))


def test_write_calls_match_request_model_and_are_not_retried_by_n8n():
    nodes = writes()
    assert len(nodes) == 2
    for node in nodes:
        assert body_keys(node) == set(WriteRequest.model_fields)
        assert node["retryOnFail"] is False and node["onError"] == "continueErrorOutput"
        assert node["parameters"]["genericAuthType"] == "httpHeaderAuth"


def test_config_names_a_supported_crm():
    values = {
        a["name"]: a["value"] for a in NODES["Config"]["parameters"]["assignments"]["assignments"]
    }
    assert values["crm"] in {"hubspot", "attio", "pipedrive"} and values["crm_sync_url"]


def test_every_outcome_has_a_branch_and_unknown_ones_reach_the_dlq():
    (switch,) = of_type("switch")
    assert switch["parameters"]["options"]["fallbackOutput"] == "extra"  # V3 drops otherwise
    for landed in ("created", "updated", "upserted"):
        assert route(landed, False)[0].startswith("Log: landed")
    assert route(Outcome.SKIPPED, False)[0].startswith("Skip + log")
    assert route(Outcome.FAILED, False)[0].startswith("DLQ")
    assert route("something_new", False)[0].startswith("DLQ")
    (wait,) = route(Outcome.FAILED, True)
    assert NODES[wait]["type"] == "n8n-nodes-base.wait"
    (collect,) = targets(wait)
    assert NODES[collect]["parameters"]["aggregate"] == "aggregateIndividualFields"
    (retry,) = targets(collect)
    assert retry in {n["name"] for n in writes()}
    assert targets(retry, 1)[0].startswith("Alert")


def test_retry_reposts_only_original_rows_by_row_id():
    (first,) = [n for n in writes() if "$('Config')" not in n["parameters"]["url"]]
    (retry,) = [n for n in writes() if n is not first]
    body = retry["parameters"]["jsonBody"]
    webhook = of_type("webhook")[0]["name"]
    assert f"$('{webhook}').first().json.body.rows" in body
    assert "$json.row_id.includes(r.row_id)" in body
    assert route(Outcome.FAILED, True) != route(Outcome.FAILED, False)


def test_write_errors_respond_and_route_nothing():
    (first,) = [n for n in writes() if "$('Config')" not in n["parameters"]["url"]]
    assert set(targets(first["name"], 0)) == {"Respond: write summary", "One item per row"}
    (refused,) = targets(first["name"], 1)
    assert NODES[refused]["type"] == "n8n-nodes-base.respondToWebhook"
    assert of_type("splitOut")[0]["parameters"]["fieldToSplitOut"] == "outcomes"


def test_demo_response_lands_on_known_branches():
    body = TestClient(create_app(demo_service())).post("/v1/write", json=DATASET).json()
    branches = {route(o["outcome"], o["retryable"])[0] for o in body["outcomes"]}
    assert len(branches) == 4  # landed, skipped, retry wait, DLQ: every branch exercised
    assert all(b in NODES for b in branches)
