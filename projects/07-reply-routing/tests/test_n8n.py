"""n8n/intake-and-route.json checked against the service (structure only, no live n8n).

The demo replays synthetic deliveries the way the workflow would: the Set nodes' field
expressions are resolved in Python, the request goes to the FastAPI app, the status code is
what the Respond node sends back and the outcome / action kinds pick the Switch branches.
"""

import json
import re
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from replyrouting.api import FillBody, ReplyService, create_app
from replyrouting.intake import IntakeConfig, IntakeOutcome, MemorySeenEvents
from replyrouting.routing import ActionKind

ROOT = Path(__file__).resolve().parents[1]
WF = json.loads((ROOT / "n8n" / "intake-and-route.json").read_text())
NODES = {n["name"]: n for n in WF["nodes"]}
DEMO = json.loads((ROOT / "examples" / "demo-deliveries.json").read_text())
SECRETS = {
    "SMARTLEAD_TOKEN": "demo-smartlead-token-0123456789",
    "HEYREACH_TOKEN": "demo-heyreach-token-0123456789",
    "LEMLIST_SECRET": "demo-lemlist-secret-0123456789",
}
API_TOKEN = "demo-api-token-0123456789"


def of_type(suffix):
    return [n for n in WF["nodes"] if n["type"] == f"n8n-nodes-base.{suffix}"]


def targets(name, output=0):
    main = WF["connections"].get(name, {}).get("main", [])
    return [link["node"] for link in main[output]] if output < len(main) else []


def switch_branch(name, value):
    rules = NODES[name]["parameters"]["rules"]["values"]
    for i, rule in enumerate(rules):
        if rule["conditions"]["conditions"][0]["rightValue"] == value:
            return targets(name, i)
    return targets(name, len(rules))  # fallback output ("extra")


def switch_values(name):
    rules = NODES[name]["parameters"]["rules"]["values"]
    return [r["conditions"]["conditions"][0]["rightValue"] for r in rules]


def assignments(node):
    return {a["name"]: a["value"] for a in node["parameters"]["assignments"]["assignments"]}


def resolve(path, item):
    """``$json.body.lead?.profile_url`` against the webhook item; missing -> None."""
    value = item
    for part in path.removeprefix("$json.").replace("?.", ".").split("."):
        value = value.get(part) if isinstance(value, dict) else None
    return value


def envelope(item):
    expr = assignments(NODES["LinkedIn: map envelope"])["payload"]
    fields = re.findall(r"(\w+): ((?:\$json[\w.?]+(?: \|\| )?)+)", expr)
    out = {}
    for key, alternatives in fields:
        out[key] = next(
            (v for p in alternatives.split(" || ") if (v := resolve(p, item)) is not None), None
        )
    return out


def substitute(value):
    if isinstance(value, dict):
        return {k: substitute(v) for k, v in value.items()}
    return SECRETS.get(value, value) if isinstance(value, str) else value


@pytest.fixture()
def client():
    now = datetime.fromisoformat(DEMO["now"].replace("Z", "+00:00"))
    config = IntakeConfig(
        lemlist_secret=SECRETS["LEMLIST_SECRET"],
        path_tokens={
            "smartlead": SECRETS["SMARTLEAD_TOKEN"],
            "heyreach": SECRETS["HEYREACH_TOKEN"],
        },
    )
    service = ReplyService(config, MemorySeenEvents(), clock=lambda: now)
    return TestClient(create_app(service, api_token=API_TOKEN))


def deliver(client, d):
    """What the two webhook paths + Set nodes + Intake node send, and what comes back."""
    item = {
        "params": {"vendor": d["vendor"], "token": substitute(d["token"])},
        "query": d.get("query", {}),
        "body": substitute(d["body"]),
    }
    route = "linkedin" if d["webhook"] == "linkedin" else "intake"
    payload = envelope(item) if route == "linkedin" else item["body"]
    url = f"/v1/{route}/{item['params']['vendor']}/{item['params']['token']}"
    return client.post(url, json=payload)


def test_webhooks_feed_one_intake_call_without_n8n_retries():
    webhooks = {n["parameters"]["path"]: n for n in of_type("webhook")}
    assert {"reply-intake/:vendor/:token", "linkedin-intake/:vendor/:token"} <= set(webhooks)
    for path in ("reply-intake/:vendor/:token", "linkedin-intake/:vendor/:token"):
        assert webhooks[path]["parameters"]["responseMode"] == "responseNode"
    assert targets("Email: pass body through") == ["Config"]
    assert targets("LinkedIn: map envelope") == ["Config"]
    assert assignments(NODES["Email: pass body through"])["route"] == "intake"
    assert assignments(NODES["LinkedIn: map envelope"])["route"] == "linkedin"
    (intake,) = targets("Config")
    node = NODES[intake]
    assert node["retryOnFail"] is False and node["onError"] == "continueErrorOutput"
    url = node["parameters"]["url"]
    assert "/v1/{{ $json.route }}/" in url and "encodeURIComponent($json.token)" in url
    response = node["parameters"]["options"]["response"]["response"]
    assert response["fullResponse"] is True and response["neverError"] is True


def test_vendor_gets_the_service_status_and_a_down_service_is_a_503():
    (intake,) = targets("Config")
    (respond,) = targets(intake, 0)
    assert NODES[respond]["parameters"]["options"]["responseCode"] == "={{ $json.statusCode }}"
    (down,) = targets(intake, 1)
    assert NODES[down]["type"] == "n8n-nodes-base.respondToWebhook"
    assert NODES[down]["parameters"]["options"]["responseCode"] == 503
    assert targets(down) == ["Alert (ops channel)"]
    # The answer goes out before any action runs.
    assert targets(respond) == ["Decision"] and targets("Decision") == ["Route by outcome"]


def test_every_outcome_and_action_kind_has_a_branch_and_unknowns_alert():
    for name in ("Route by outcome", "Route by action kind"):
        assert NODES[name]["parameters"]["options"]["fallbackOutput"] == "extra"
        assert switch_branch(name, "something_new") == ["Alert (ops channel)"]
    outcomes = {o.value for o in IntakeOutcome} | {"error"}
    assert set(switch_values("Route by outcome")) == outcomes
    assert set(switch_values("Route by action kind")) == {k.value for k in ActionKind}
    assert switch_branch("Route by outcome", "accepted") == ["One item per action"]
    assert switch_branch("Route by outcome", "stale") == ["Review queue: log event"]
    assert switch_branch("Route by outcome", "rejected") == ["Alert (ops channel)"]
    assert switch_branch("Route by outcome", "duplicate") == ["Done (no action)"]
    assert switch_branch("Route by action kind", "suppress") == ["Execute: sequencer actions"]
    assert switch_branch("Route by action kind", "notify_human") == ["Execute: notify a human"]


def test_failed_sequencer_or_crm_actions_reach_the_alert():
    for name in ("Execute: sequencer actions", "Execute: CRM + data actions"):
        node = NODES[name]
        assert node["parameters"]["options"]["waitForSubWorkflow"] is True
        assert node["onError"] == "continueErrorOutput"
        assert targets(name, 1) == ["Alert (ops channel)"]


def test_reply_text_fill_matches_the_api_and_rejoins_the_outcome_switch():
    (fill,) = targets("Webhook: reply text filled")
    node = NODES[fill]
    assert node["parameters"]["url"].endswith("/v1/replies")
    assert node["parameters"]["genericAuthType"] == "httpHeaderAuth"
    body = node["parameters"]["jsonBody"]
    assert set(re.findall(r"(\w+): \$json", body)) == set(FillBody.model_fields)
    assert targets(fill, 0) == ["Route by outcome"]
    assert targets(fill, 1) == ["Alert (ops channel)"]
    assert NODES["Webhook: reply text filled"]["parameters"]["authentication"] == "headerAuth"


def test_placeholders_are_explicit_not_live_ids():
    text = json.dumps(WF)
    for n in of_type("executeWorkflow"):
        assert n["parameters"]["workflowId"]["value"].startswith("REPLACE_")
    assert "REPLACE_ops_alert_webhook_url" in text
    assert "@" not in text.replace("@example", "")  # no real addresses in the workflow


def test_demo_deliveries_land_on_the_expected_branches(client):
    seen_outcomes, seen_branches = set(), set()
    for d in DEMO["deliveries"]:
        r = deliver(client, d)
        body = r.json()
        assert body["outcome"] == d["expect"], d["label"]
        assert r.status_code == (401 if d["expect"] == "rejected" else 200), d["label"]
        (branch,) = switch_branch("Route by outcome", body["outcome"])
        assert branch in NODES
        if d["expect"] not in {"rejected", "alert"}:
            assert branch != "Alert (ops channel)", d["label"]
        seen_outcomes.add(body["outcome"])
        seen_branches.add(branch)
        for action in body.get("actions") or []:
            (target,) = switch_branch("Route by action kind", action["kind"])
            assert target != "Alert (ops channel)", (d["label"], action["kind"])
            seen_branches.add(target)
    assert seen_outcomes == {o.value for o in IntakeOutcome}  # every outcome exercised
    assert {"Execute: sequencer actions", "Review queue: log event"} <= seen_branches


def test_linkedin_envelope_from_the_demo_mapping_is_usable(client):
    (reply,) = [d for d in DEMO["deliveries"] if d["label"].startswith("heyreach LinkedIn reply")]
    item = {"params": {}, "query": reply["query"], "body": reply["body"]}
    env = envelope(item)
    assert env["event_type"] == "MESSAGE_REPLY_RECEIVED"
    assert env["profile_url"].startswith("https://www.linkedin.com/in/")
    assert env["message_at"] == "2026-11-16T16:40:00.000Z" and env["campaign_id"] == "cmp-demo-1"
    body = deliver(client, reply).json()
    channels = {(a["kind"], a["channel"]) for a in body["actions"]}
    assert ("stop_sequences", "linkedin") in channels


def test_needs_body_event_is_filled_once_through_the_fill_route(client):
    (waiting,) = [d for d in DEMO["deliveries"] if d["expect"] == "needs_body"]
    key = deliver(client, waiting).json()["event"]["event_key"]
    auth = {"Authorization": f"Bearer {API_TOKEN}"}
    filled = client.post(
        "/v1/replies", json={"event_key": key, "body_text": "Unsubscribe me."}, headers=auth
    )
    assert filled.status_code == 200 and filled.json()["reply_class"] == "unsubscribe"
    (branch,) = switch_branch("Route by outcome", filled.json()["outcome"])
    assert branch == "One item per action"
    again = client.post(
        "/v1/replies", json={"event_key": key, "body_text": "Unsubscribe me."}, headers=auth
    )
    assert again.status_code == 404  # second fill is refused, not routed twice
