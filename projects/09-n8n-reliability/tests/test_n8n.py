"""n8n/retry-loop.json replayed offline against POST /v1/decide (no live n8n, no vendor).

The replay walks the workflow's own connections: Set assignments and node expressions are
resolved by a small evaluator below (only the expression forms the workflow uses), the
push goes to a scripted vendor from examples/demo-loop.json, the decide and DLQ calls go to
the FastAPI app and the Switch picks its branch from the response. A lead's path is therefore
decided by the JSON file, not by the test.
"""

import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from reliability.api import DecideIn, create_app

ROOT = Path(__file__).resolve().parents[1]
WF = json.loads((ROOT / "n8n" / "retry-loop.json").read_text())
NODES = {n["name"]: n for n in WF["nodes"]}
DEMO = json.loads((ROOT / "examples" / "demo-loop.json").read_text())
TOKEN = "demo-reliability-token-0123456789"
LOOP = "Loop over leads"
DLQ = "DLQ (POST /v1/dlq)"
WORKFLOW_META = {"$workflow.name": "push-demo", "$execution.id": "77"}  # n8n's built-ins


def targets(name, output=0):
    main = WF["connections"].get(name, {}).get("main", [])
    return [link["node"] for link in main[output]] if output < len(main) else []


def switch_values(name):
    rules = NODES[name]["parameters"]["rules"]["values"]
    return [r["conditions"]["conditions"][0]["rightValue"] for r in rules]


# --- a minimal evaluator for the expression forms used in the workflow -----------------

_REF = re.compile(r"^\$\('([^']+)'\)\.(item|first\(\))\.json((?:\??\.\w+)*)$")
_JSON = re.compile(r"^\$json((?:\??\.\w+)*)$")


def _walk(value, path):
    for part in filter(None, re.split(r"\??\.", path)):
        value = value.get(part) if isinstance(value, dict) else None
    return value


def _term(expr, item, ctx):
    expr = expr.strip()
    if expr == "null":
        return None
    if m := re.fullmatch(r"'([^']*)'", expr):
        return m.group(1)
    if expr in WORKFLOW_META:
        return WORKFLOW_META[expr]
    if m := _JSON.match(expr):
        return _walk(item, m.group(1))
    if m := _REF.match(expr):
        node, path = m.group(1), m.group(3)
        assert node in ctx, f"{node} has not run yet"
        return _walk(ctx[node], path)
    if expr.startswith("{") and expr.endswith("}"):
        fields = re.findall(r"(\w+): ([^,]+?)(?=, \w+: |\s*}$)", expr)
        assert fields, expr
        return {k: _expr(v, item, ctx) for k, v in fields}
    if m := re.match(r"^JSON\.stringify\((.+)\)$", expr):
        return _expr(m.group(1), item, ctx)
    if m := re.match(r"^(.+) / (\d+)$", expr):
        return _expr(m.group(1), item, ctx) / int(m.group(2))
    raise AssertionError(f"expression form not supported by the replay: {expr!r}")


def _expr(expr, item, ctx):
    if expr.strip().startswith(("{", "JSON.stringify(")):
        return _term(expr, item, ctx)
    for alternative in expr.split(" ?? "):
        value = _term(alternative, item, ctx)
        if value is not None:
            return value
    return None


def evaluate(value, item, ctx):
    if not (isinstance(value, str) and value.startswith("=")):
        return value
    body = value[1:]
    whole = re.fullmatch(r"\{\{ (.+) \}\}", body)
    if whole:
        return _expr(whole.group(1), item, ctx)
    return re.sub(r"\{\{ (.+?) \}\}", lambda m: str(_expr(m.group(1), item, ctx)), body)


def run_set(node, item, ctx):
    params = node["parameters"]
    out = dict(item) if params.get("includeOtherFields") else {}
    for a in params["assignments"]["assignments"]:
        value = evaluate(a["value"], item, ctx)
        if a["type"] == "object" and isinstance(value, str):
            value = json.loads(value)
        out[a["name"]] = value
    return out


# --- the replay -------------------------------------------------------------------------


SERVICE = "http://reliability:8009"
ALERTS = ("Alert: unknown action", "Alert: claim needs review")


class Replay:
    def __init__(self, client, entries):
        self.client = client
        self.script = {d["lead"]["email"]: list(d["answers"]) for d in entries}
        self.leads = [d["lead"] for d in entries]
        self.pushes = {}
        self.waits = {}
        self.ends = {}
        self.dlq = []
        self.alerts = []
        self.calls = {}  # service path -> bodies, in order

    @property
    def decide_calls(self):
        return len(self.calls.get("/v1/decide", []))

    def step(self, name, item, ctx):
        """Run one node on one item -> (output index, item out)."""
        node = NODES[name]
        kind = node["type"].removeprefix("n8n-nodes-base.")
        if kind == "set":
            return 0, run_set(node, item, ctx)
        if kind == "noOp":
            return 0, item
        if kind == "switch":
            left = node["parameters"]["rules"]["values"][0]["conditions"]["conditions"][0]
            value = evaluate(left["leftValue"], item, ctx)
            values = switch_values(name)
            return (values.index(value) if value in values else len(values)), item
        if kind == "wait":
            params = node["parameters"]
            assert params["resume"] == "timeInterval" and params["unit"] == "seconds"
            email = item["lead"]["email"]
            self.waits.setdefault(email, []).append(evaluate(params["amount"], item, ctx))
            return 0, item
        if name == "Push lead (one item)":
            return self.push(node, item, ctx)
        if name in ALERTS:
            self.alerts.append((name, item))
            return 0, item
        if kind == "httpRequest":
            return self.service(node, item, ctx)
        raise AssertionError(f"replay does not know node {name!r}")

    def push(self, node, item, ctx):
        params = node["parameters"]
        assert evaluate(params["method"], item, ctx) == "POST"
        assert evaluate(params["url"], item, ctx).startswith("https://api.sequencer.example/")
        lead = evaluate(params["jsonBody"], item, ctx)
        email = lead["email"]
        self.pushes[email] = self.pushes.get(email, 0) + 1
        answer = self.script[email].pop(0)
        if "error" in answer:
            return 1, {"error": answer["error"]}
        return 0, {"statusCode": answer["status"], "headers": answer.get("headers", {}), "body": {}}

    def service(self, node, item, ctx):
        """Any call to the reliability service goes to the FastAPI app; non-200 fails the run."""
        params = node["parameters"]
        assert params["method"] == "POST" and params["genericAuthType"] == "httpHeaderAuth"
        url = evaluate(params["url"], item, ctx)
        assert url.startswith(SERVICE + "/v1/"), url
        path = url.removeprefix(SERVICE)
        body = evaluate(params["jsonBody"], item, ctx)
        self.calls.setdefault(path, []).append(body)
        r = self.client.post(path, json=body, headers={"Authorization": f"Bearer {TOKEN}"})
        assert r.status_code == 200, (path, r.text)
        if path == "/v1/dlq":
            self.dlq.append(r.json())
        return 0, r.json()

    def drive(self, item, ctx):
        """One lead from the loop output until it is handed back to the loop."""
        ctx = dict(ctx)  # node outputs of this pass only; Config stays visible
        name = targets(LOOP, 1)[0]
        for _ in range(200):
            output, item = self.step(name, item, ctx)
            ctx[name] = item
            (name,) = targets(name, output)
            if name == LOOP:
                return item
            if name == "Next lead":  # DLQ / alert paths carry no lead fields: read the pass
                end = ctx.get("Decision + lead") or ctx["Claim + lead"]
                self.ends[end["lead"]["email"]] = end
        raise AssertionError("lead never returned to the loop")

    def run(self):
        ctx = {}
        (collect,) = targets("Leads in (sub-workflow)")
        agg = NODES[collect]["parameters"]
        assert agg["aggregate"] == "aggregateAllItemData"
        one = {agg["destinationFieldName"]: self.leads}
        (config,) = targets(collect)
        ctx[config] = run_set(NODES[config], one, ctx)
        (split,) = targets(config)
        leads = ctx[config][NODES[split]["parameters"]["fieldToSplitOut"]]
        assert targets(split) == [LOOP]
        for lead in leads:  # batch size 1: one lead per pass
            self.drive(lead, ctx)
        return targets(LOOP, 0)


class Clock:
    def __init__(self, t):
        self.t = float(t)

    def __call__(self):
        return self.t


@pytest.fixture()
def clock():
    return Clock(DEMO["now"])


@pytest.fixture()
def client(clock):
    return TestClient(create_app(api_token=TOKEN, now=clock, rand=lambda: 0.5))


@pytest.fixture()
def replay(client):
    r = Replay(client, DEMO["leads"])
    r.finished = r.run()
    return r


# --- structure ------------------------------------------------------------------------


def test_push_node_never_retries_itself_and_splits_answer_from_no_answer():
    push = NODES["Push lead (one item)"]
    assert push["retryOnFail"] is False and push["onError"] == "continueErrorOutput"
    response = push["parameters"]["options"]["response"]["response"]
    assert response["fullResponse"] is True and response["neverError"] is True
    assert targets("Push lead (one item)", 0) == ["Decide body: HTTP answer"]
    assert targets("Push lead (one item)", 1) == ["Decide body: no HTTP answer"]


def test_loop_takes_one_lead_per_pass_and_retries_re_enter_the_same_call():
    assert NODES[LOOP]["parameters"]["batchSize"] == 1
    assert NODES[LOOP]["typeVersion"] == 3  # outputs: 0 done, 1 loop
    assert targets(LOOP, 0) == ["All leads handled"]
    assert targets(LOOP, 1) == ["Claim body"]
    assert targets("Wait (wait_ms)") == ["Next attempt"]
    assert targets("Next attempt") == ["Attempt"]
    assert targets("Attempt") == ["Push lead (one item)"]
    # only "Next lead" feeds the loop after the start: a retry never pulls in the next lead
    sources = [
        src
        for src, out in WF["connections"].items()
        for branch in out["main"]
        for link in branch
        if link["node"] == LOOP
    ]
    assert sources == ["One item per lead", "Next lead"]


def test_switch_covers_every_action_and_unknown_actions_alert():
    assert switch_values("Route by action") == ["done", "retry", "stop"]
    assert NODES["Route by action"]["parameters"]["options"]["fallbackOutput"] == "extra"
    assert targets("Route by action", 0) == ["Commit key (POST /v1/keys/commit)"]
    assert targets("Route by action", 1) == ["Wait (wait_ms)"]
    assert targets("Route by action", 2) == ["DLQ intake body"]
    assert targets("Route by action", 3) == ["Alert: unknown action"]
    assert targets("DLQ intake body") == [DLQ]
    assert targets(DLQ) == ["Key after stop"]


def test_dlq_write_is_authenticated_and_fails_the_run_when_it_cannot_store():
    from reliability.api import DlqIn

    dlq = NODES[DLQ]
    assert dlq["type"] == "n8n-nodes-base.httpRequest"
    assert dlq["parameters"]["genericAuthType"] == "httpHeaderAuth"
    assert "onError" not in dlq and dlq["retryOnFail"] is False
    assert assigned("DLQ intake body") == set(DlqIn.model_fields)


def assigned(name):
    return {a["name"] for a in NODES[name]["parameters"]["assignments"]["assignments"]}


def test_decide_bodies_only_send_fields_the_service_reads():
    accepted = set(DecideIn.model_fields) | {"statusCode"}
    for name in ("Decide body: HTTP answer", "Decide body: no HTTP answer"):
        sent = assigned(name)
        assert sent <= accepted, sent - accepted
    answer = assigned("Decide body: HTTP answer")
    assert {"statusCode", "headers"} <= answer and "error" not in answer
    decide = NODES["Decide (POST /v1/decide)"]
    assert decide["parameters"]["genericAuthType"] == "httpHeaderAuth"
    assert "onError" not in decide  # a down decide service fails the run loudly


# --- replay ---------------------------------------------------------------------------


def test_every_lead_ends_exactly_once_as_scripted(replay):
    assert replay.finished == ["All leads handled"]
    emails = [d["lead"]["email"] for d in DEMO["leads"]]
    assert sorted(replay.ends) == sorted(emails)
    for d in DEMO["leads"]:
        email, expect = d["lead"]["email"], d["expect"]
        end = replay.ends[email]
        assert replay.pushes[email] == expect["pushes"], email
        assert replay.waits.get(email, []) == expect["waits_s"], email
        if expect["end"] == "done":
            assert end["decision"]["action"] == "done" and end["decision"]["outcome"] == "ok"
        else:
            (row,) = [r for r in replay.dlq if r["key"] == email]
            assert row["outcome"] == expect["outcome"], email
            assert row["safe_to_resend"] is expect["safe_to_resend"], email
    assert all(not answers for answers in replay.script.values())  # every answer consumed
    assert replay.alerts == []


def test_a_possible_landing_is_never_pushed_again(replay):
    for email in ("cara@cobalt.example", "eli@elm.example"):
        assert replay.pushes[email] == 1
        (row,) = [r for r in replay.dlq if r["key"] == email]
        assert row["outcome"] == "unknown" and row["safe_to_resend"] is False


def test_retry_after_beyond_the_policy_is_a_deferred_row_not_a_long_wait(replay):
    (row,) = [r for r in replay.dlq if r["key"] == "fay@fjord.example"]
    assert row["reason"] == "http_429" and row["last_status"] == 429
    assert row["retry_at"] == DEMO["now"] + 900
    assert "fay@fjord.example" not in replay.waits


def test_only_failed_leads_reach_the_dlq_and_waits_follow_the_service(replay):
    assert len(replay.dlq) == 5
    assert {r["key"] for r in replay.dlq}.isdisjoint({"ana@acme.example", "ben@birch.example"})
    assert replay.decide_calls == sum(d["expect"]["pushes"] for d in DEMO["leads"])


def test_dlq_rows_carry_the_lead_scope_and_the_execution_that_failed(replay):
    listed = replay.client.get(
        "/v1/dlq",
        params={"scope": "sequencer:campaign-demo"},
        headers={"Authorization": f"Bearer {TOKEN}"},
    ).json()
    assert listed["count"] == 5
    for row in listed["rows"]:
        lead = next(d["lead"] for d in DEMO["leads"] if d["lead"]["email"] == row["key"])
        assert row["payload"] == lead  # replay can hand back the exact lead
        assert row["source"] == {
            "workflow": "push-demo",
            "node": "Push lead (one item)",
            "execution_id": "77",
        }
        assert row["state"] == "open" and row["count"] == 1


def test_replay_hands_back_only_rows_that_cannot_have_landed(replay):
    auth = {"Authorization": f"Bearer {TOKEN}"}
    out = replay.client.post("/v1/dlq/replay", json={"dry_run": True}, headers=auth).json()
    # exhausted is replayable now; deferred only after retry_at (900 s after the run)
    assert [i["key"] for i in out["replay"]] == ["hal@heath.example"]
    refused = {i["key"]: i["status"] for i in out["refused"]}
    assert refused["cara@cobalt.example"] == refused["eli@elm.example"] == "unsafe_to_resend"
    assert refused["fay@fjord.example"] == "not_due"


def test_unexpected_action_takes_the_alert_branch():
    client = TestClient(create_app(api_token=TOKEN))
    r = Replay(client, DEMO["leads"])
    ctx = {"Attempt": {"attempt": 1, "lead": {"email": "x@y.example"}}}
    item = {"decision": {"action": "pause"}, "lead": {"email": "x@y.example"}}
    output, _ = r.step("Route by action", item, ctx)
    assert targets("Route by action", output) == ["Alert: unknown action"]


# --- dedupe keys: claim before the push, commit on done, release what never landed --------

AUTH = {"Authorization": f"Bearer {TOKEN}"}
SCOPE = "sequencer:campaign-demo"
LANDED = {"ana@acme.example", "ben@birch.example", "dev@dune.example"}
MAY_HAVE_LANDED = {"cara@cobalt.example", "eli@elm.example"}
NEVER_LANDED = {"fay@fjord.example", "gus@gale.example", "hal@heath.example"}


def second_run(client, answer=None):
    """Same leads again; only the leads in `answer` have a scripted vendor reply."""
    answer = answer or {}
    entries = [
        {"lead": d["lead"], "answers": answer.get(d["lead"]["email"], [])} for d in DEMO["leads"]
    ]
    r = Replay(client, entries)
    r.finished = r.run()
    return r


def test_every_reliability_call_is_authenticated_and_fails_the_run_loudly():
    service = [
        n
        for n in WF["nodes"]
        if n["type"] == "n8n-nodes-base.httpRequest" and "reliability_url" in n["parameters"]["url"]
    ]
    paths = sorted(n["parameters"]["url"].split("}}")[-1] for n in service)
    assert paths == [
        "/v1/decide",
        "/v1/dlq",
        "/v1/keys/claim",
        "/v1/keys/commit",
        "/v1/keys/release",
    ]
    for n in service:
        assert n["parameters"]["genericAuthType"] == "httpHeaderAuth", n["name"]
        assert "onError" not in n and n["retryOnFail"] is False, n["name"]


def test_only_a_fresh_reservation_is_pushed_and_unknown_keeps_its_key():
    assert switch_values("Route by claim") == ["reserved", "committed", "in_flight", "stale"]
    assert targets("Route by claim", 0) == ["First attempt"]
    assert (
        targets("Route by claim", 1)
        == targets("Route by claim", 2)
        == ["Skip: already pushed or in flight"]
    )
    assert (
        targets("Route by claim", 3)
        == targets("Route by claim", 4)
        == ["Alert: claim needs review"]
    )
    assert switch_values("Key after stop") == ["unknown", "failed", "exhausted", "deferred"]
    assert targets("Key after stop", 0) == ["Keep reservation (may have landed)"]
    for out in (1, 2, 3):
        assert targets("Key after stop", out) == ["Release key (POST /v1/keys/release)"]
    assert targets("Key after stop", 4) == ["Alert: unknown action"]


def test_first_run_commits_what_landed_and_releases_what_never_did(replay):
    commits = {b["key"] for b in replay.calls["/v1/keys/commit"]}
    releases = {b["key"] for b in replay.calls["/v1/keys/release"]}
    assert commits == LANDED and releases == NEVER_LANDED
    tokens = {
        b["token"] for b in replay.calls["/v1/keys/commit"] + replay.calls["/v1/keys/release"]
    }
    assert len(tokens) == 6 and None not in tokens  # each lead's own claim token
    pending = replay.client.get("/v1/keys/pending", params={"scope": SCOPE}, headers=AUTH).json()
    assert {r["key"] for r in pending["reservations"]} == MAY_HAVE_LANDED


def test_second_run_skips_pushed_and_in_flight_leads_and_retries_only_released_ones(replay, clock):
    clock.t += 60
    again = second_run(replay.client, {e: [{"status": 201}] for e in NEVER_LANDED})
    assert again.finished == ["All leads handled"]
    assert again.pushes == dict.fromkeys(NEVER_LANDED, 1)  # no other lead reaches the vendor
    for email in LANDED:
        assert again.ends[email]["claim"]["status"] == "committed"
    for email in MAY_HAVE_LANDED:
        assert again.ends[email]["claim"]["status"] == "in_flight"
    for email in NEVER_LANDED:
        assert again.ends[email]["decision"]["outcome"] == "ok"
    assert again.alerts == [] and again.dlq == []


def test_an_old_reservation_that_may_have_landed_alerts_instead_of_re_sending(replay, clock):
    clock.t += 901  # RELIABILITY_STALE_AFTER default: 900 s
    again = second_run(replay.client, {e: [{"status": 201}] for e in NEVER_LANDED})
    assert set(again.pushes) == NEVER_LANDED
    reviewed = {item["lead"]["email"] for name, item in again.alerts}
    assert reviewed == MAY_HAVE_LANDED
    assert {name for name, _ in again.alerts} == {"Alert: claim needs review"}
    for email in MAY_HAVE_LANDED:
        assert email not in again.pushes


def test_a_dlq_replay_of_a_lead_that_landed_since_is_skipped_by_its_claim(replay, clock):
    clock.t += 60
    second_run(replay.client, {e: [{"status": 201}] for e in NEVER_LANDED})
    handed = replay.client.post("/v1/dlq/replay", json={}, headers=AUTH).json()["replay"]
    assert [i["key"] for i in handed] == ["hal@heath.example"]  # the DLQ row is still open
    third = Replay(replay.client, [{"lead": i["payload"], "answers": []} for i in handed])
    third.run()
    assert third.pushes == {}  # committed in run 2: the replayed payload is not pushed again
    assert third.ends["hal@heath.example"]["claim"]["status"] == "committed"


# --- n8n/dlq-replay.json: scheduled replay of safe rows + alert on rows that need a person --

RWF = json.loads((ROOT / "n8n" / "dlq-replay.json").read_text())
RNODES = {n["name"]: n for n in RWF["nodes"]}


def rtargets(name, output=0):
    main = RWF["connections"].get(name, {}).get("main", [])
    return [link["node"] for link in main[output]] if output < len(main) else []


def tick(client, answers=None):
    """One schedule tick of dlq-replay.json; the sub-workflow call runs retry-loop.json."""
    answers = answers or {}
    out = {"pushed": None, "alerts": [], "replay": None}
    (config,) = rtargets("Every 30 minutes")
    ctx = {config: run_set(RNODES[config], {}, {})}
    replay_node, summary_node = rtargets(config)
    for name in (replay_node, summary_node):
        params = RNODES[name]["parameters"]
        assert params["genericAuthType"] == "httpHeaderAuth"
        path = evaluate(params["url"], ctx[config], ctx).removeprefix(SERVICE)
        if params["method"] == "POST":
            body = evaluate(params["jsonBody"], ctx[config], ctx)
            r = client.post(path, json=body, headers=AUTH)
        else:
            query = {
                p["name"]: evaluate(p["value"], ctx[config], ctx)
                for p in params["queryParameters"]["parameters"]
            }
            r = client.get(path, params=query, headers=AUTH)
        assert r.status_code == 200, r.text
        ctx[name] = r.json()
    out["replay"] = ctx[replay_node]
    # replay branch: Split Out (noOtherFields spreads each row) -> raw Set -> sub-workflow
    (split,) = rtargets(replay_node)
    rows = ctx[replay_node][RNODES[split]["parameters"]["fieldToSplitOut"]]
    (to_lead,) = rtargets(split)
    assert RNODES[to_lead]["parameters"]["mode"] == "raw"
    leads = [evaluate(RNODES[to_lead]["parameters"]["jsonOutput"], row, ctx) for row in rows]
    (sub,) = rtargets(to_lead)
    if leads:  # n8n runs no node below an empty Split Out
        entries = [{"lead": lead, "answers": answers.get(lead["email"], [])} for lead in leads]
        out["pushed"] = Replay(client, entries)
        assert out["pushed"].run() == ["All leads handled"]
    # summary branch: IF open_unsafe > 0 -> alert
    (check,) = rtargets(summary_node)
    cond = RNODES[check]["parameters"]["conditions"]["conditions"][0]
    assert cond["operator"] == {"type": "number", "operation": "gt"}
    hit = evaluate(cond["leftValue"], ctx[summary_node], ctx) > cond["rightValue"]
    for name in rtargets(check, 0 if hit else 1):
        out["alerts"].append((name, ctx[summary_node]))
    return out


def test_replay_workflow_targets_the_same_scope_and_the_retry_loop():
    loop_scope = {
        a["name"]: a["value"] for a in NODES["Config"]["parameters"]["assignments"]["assignments"]
    }
    replay_cfg = {
        a["name"]: a["value"] for a in RNODES["Config"]["parameters"]["assignments"]["assignments"]
    }
    assert replay_cfg["scope"] == loop_scope["scope"]
    assert replay_cfg["reliability_url"] == loop_scope["reliability_url"]
    sub = RNODES["Push again (retry-loop)"]["parameters"]
    assert sub["mode"] == "once" and sub["options"]["waitForSubWorkflow"] is True
    assert sub["workflowId"]["value"] == "REPLACE_retry_loop_workflow_id"
    for n in RWF["nodes"]:
        if "reliability_url" in n["parameters"].get("url", ""):
            assert "onError" not in n and n["retryOnFail"] is False, n["name"]
    assert rtargets("Needs a person?", 1) == []  # nothing to do on a clean store


def test_tick_pushes_only_safe_rows_once_and_alerts_on_rows_replay_never_touches(replay, clock):
    clock.t += 901  # fay's Retry-After (900 s) has passed
    first = tick(
        replay.client,
        {"fay@fjord.example": [{"status": 201}], "hal@heath.example": [{"status": 201}]},
    )
    assert first["pushed"].pushes == {"fay@fjord.example": 1, "hal@heath.example": 1}
    assert {e: end["decision"]["outcome"] for e, end in first["pushed"].ends.items()} == {
        "fay@fjord.example": "ok",
        "hal@heath.example": "ok",
    }
    refused = {i["key"] for i in first["replay"]["refused"]}
    assert refused == {"cara@cobalt.example", "eli@elm.example", "gus@gale.example"}
    ((alert, summary),) = first["alerts"]
    assert alert == "Alert: DLQ needs a read-back" and summary["open_unsafe"] == 3

    second = tick(replay.client)  # same rows are 'replayed' now: nothing is handed out twice
    assert second["pushed"] is None and second["replay"]["replay"] == []


def test_tick_is_quiet_once_a_person_resolved_the_unsafe_rows(replay):
    rows = replay.client.get("/v1/dlq", headers=AUTH).json()["rows"]
    for row in rows:
        if row["outcome"] == "unknown":  # read back: they did land
            r = replay.client.post(
                f"/v1/dlq/{row['id']}/resolve", json={"landed": True}, headers=AUTH
            )
            assert r.status_code == 200 and r.json()["state"] == "resolved"
        if row["outcome"] == "failed":
            r = replay.client.post(f"/v1/dlq/{row['id']}/discard", json={}, headers=AUTH)
            assert r.status_code == 200 and r.json()["state"] == "discarded"
    quiet = tick(replay.client, {"hal@heath.example": [{"status": 201}]})
    assert quiet["alerts"] == []
    assert quiet["pushed"].pushes == {"hal@heath.example": 1}  # fay is not due yet
