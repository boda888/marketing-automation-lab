import json
import re
from pathlib import Path

from signal_engine import Tier
from signal_engine.api import PrioritiseRequest, PrioritiseResponse, create_app

WF = json.loads(
    (Path(__file__).resolve().parents[1] / "n8n" / "prioritise-before-research.json").read_text()
)
NODES = {n["name"]: n for n in WF["nodes"]}


def by_type(node_type):
    return next(n for n in WF["nodes"] if n["type"] == node_type)


def targets(name, output):
    return [link["node"] for link in WF["connections"][name]["main"][output]]


def test_structure_is_valid():
    assert len(NODES) == len(WF["nodes"])
    assert len({n["id"] for n in WF["nodes"]}) == len(NODES)
    for src, outputs in WF["connections"].items():
        assert src in NODES
        assert all(link["node"] in NODES for branch in outputs["main"] for link in branch)
    assert by_type("n8n-nodes-base.webhook")["parameters"]["responseMode"] == "responseNode"


def test_http_call_matches_the_api():
    http = by_type("n8n-nodes-base.httpRequest")
    path = http["parameters"]["url"].split("}}", 1)[1]
    assert path in {route.path for route in create_app().routes}
    assert http["retryOnFail"] is True and http["maxTries"] >= 2
    sent = set(re.findall(r"(\w+): \$json\.body\.", http["parameters"]["jsonBody"]))
    assert sent == set(PrioritiseRequest.model_fields)
    # The summary response and the split read fields the API really returns.
    respond = by_type("n8n-nodes-base.respondToWebhook")["parameters"]["responseBody"]
    for field in re.findall(r"\$json\.(\w+)", respond):
        assert field in PrioritiseResponse.model_fields
    split = by_type("n8n-nodes-base.splitOut")["parameters"]
    assert split["fieldToSplitOut"] == "accounts" in PrioritiseResponse.model_fields
    assert set(targets(http["name"], 0)) == {
        by_type("n8n-nodes-base.respondToWebhook")["name"],
        by_type("n8n-nodes-base.splitOut")["name"],
    }


def test_switch_has_one_output_per_tier_plus_fallback():
    switch = by_type("n8n-nodes-base.switch")
    rules = switch["parameters"]["rules"]["values"]
    values = [r["conditions"]["conditions"][0]["rightValue"] for r in rules]
    assert sorted(values) == sorted(t.value for t in Tier)
    for r in rules:
        cond = r["conditions"]["conditions"][0]
        assert cond["leftValue"] == "={{ $json.tier }}"
        assert cond["operator"] == {"type": "string", "operation": "equals"}
        assert r["conditions"]["options"]["caseSensitive"] is True
    assert switch["parameters"]["options"]["fallbackOutput"] == "extra"
    # rules + the extra fallback output, every one wired (unwired outputs drop items)
    assert len(WF["connections"][switch["name"]]["main"]) == len(rules) + 1


def test_review_and_unexpected_never_reach_research():
    switch = by_type("n8n-nodes-base.switch")
    rules = switch["parameters"]["rules"]["values"]
    out = {r["outputKey"]: i for i, r in enumerate(rules)}
    fallback = len(rules)
    review = targets(switch["name"], out["Review"])
    assert targets(switch["name"], fallback) == review
    assert all("review" in name.lower() for name in review)
    research = set(targets(switch["name"], out["Tier 1"])) | set(
        targets(switch["name"], out["Tier 2"])
    )
    for key in ("Review", "Hold", "Tier 3"):
        assert not set(targets(switch["name"], out[key])) & research
