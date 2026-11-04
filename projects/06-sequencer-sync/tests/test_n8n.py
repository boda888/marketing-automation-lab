"""n8n/push-and-reconcile.json checked against the API models (structure only, no live n8n)."""

from __future__ import annotations

import json
import re
import typing
from pathlib import Path

from fastapi.testclient import TestClient
from sequencer import DedupeScope
from sequencer.api import Outcome, PushRequest, ReconcileRequest, create_app
from sequencer.demo import demo_service

ROOT = Path(__file__).resolve().parents[1]
WF = json.loads((ROOT / "n8n" / "push-and-reconcile.json").read_text())
NODES = {n["name"]: n for n in WF["nodes"]}
DATASET = json.loads((ROOT / "examples" / "demo-push.json").read_text())


def of_type(suffix):
    return [n for n in WF["nodes"] if n["type"] == f"n8n-nodes-base.{suffix}"]


def targets(name, output=0):
    return [link["node"] for link in WF["connections"][name]["main"][output]]


def http(path):
    (node,) = [n for n in of_type("httpRequest") if n["parameters"]["url"].endswith(path)]
    return node


def body_keys(node):
    return set(re.findall(r"(\w+):", node["parameters"]["jsonBody"].split("JSON.stringify(")[1]))


def route(outcome):
    (switch,) = of_type("switch")
    rules = switch["parameters"]["rules"]["values"]
    for i, rule in enumerate(rules):
        if rule["conditions"]["conditions"][0]["rightValue"] == outcome:
            return targets(switch["name"], i)
    return targets(switch["name"], len(rules))


def test_push_call_never_retried_and_matches_request_model():
    node = http("/v1/push")
    assert node["retryOnFail"] is False and node["onError"] == "continueErrorOutput"
    keys = body_keys(node)
    required = {n for n, f in PushRequest.model_fields.items() if f.is_required()}
    assert required <= keys <= set(PushRequest.model_fields)
    assert node["parameters"]["genericAuthType"] == "httpHeaderAuth"


def test_config_names_campaign_and_valid_scope():
    values = {
        a["name"]: a["value"] for a in NODES["Config"]["parameters"]["assignments"]["assignments"]
    }
    assert values["campaign_name"] and "campaign_id" not in values
    assert DedupeScope(values["dedupe_scope"])


def test_every_outcome_has_its_branch():
    assert set(typing.get_args(Outcome)) == {"sent", "skipped", "outcome_unknown", "failed"}
    assert route("sent") == ["Log: sent"]
    assert route("skipped")[0].startswith("Skip + log")
    assert route("failed")[0].startswith("DLQ")
    assert route("something_new")[0].startswith("DLQ")  # fallback, never dropped
    (wait,) = route("outcome_unknown")
    assert NODES[wait]["type"] == "n8n-nodes-base.wait"
    (agg,) = targets(wait)
    assert targets(agg) == [http("/v1/reconcile")["name"]]


def test_reconcile_call_retries_and_matches_request_model():
    node = http("/v1/reconcile")
    assert node["retryOnFail"] is True and node["maxTries"] >= 2
    keys = body_keys(node) - {"lead_id", "email"}  # per-lead fields inside .map()
    assert keys == {n for n, f in ReconcileRequest.model_fields.items() if f.is_required()}
    ok, failed = targets(node["name"], 0), targets(node["name"], 1)
    assert ok[0].startswith("Log: confirmed") and failed[0].startswith("Alert")


def test_push_errors_respond_and_route_nothing():
    node = http("/v1/push")
    assert set(targets(node["name"], 0)) == {"Respond: push summary", "One item per lead"}
    (refused,) = targets(node["name"], 1)
    assert NODES[refused]["type"] == "n8n-nodes-base.respondToWebhook"
    assert of_type("splitOut")[0]["parameters"]["fieldToSplitOut"] == "leads"


def test_demo_response_lands_on_known_branches():
    body = TestClient(create_app(demo_service())).post("/v1/push", json=DATASET).json()
    branches = {o["outcome"]: route(o["outcome"])[0] for o in body["leads"]}
    assert branches == {"sent": "Log: sent", "skipped": route("skipped")[0]}
