"""Error Trigger payloads -> masked alert text, repeats folded per fingerprint and window.

Payload shapes follow n8n's execute-error-workflow.ts (execution vs trigger failure) and
the Error Trigger's manual sample data; the error-alert workflow is replayed offline at the
end (no live n8n).
"""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from reliability.alerts import (
    MAX_MESSAGE,
    AlertLog,
    fingerprint,
    format_alert,
    mask_message,
    parse_error_event,
)
from reliability.api import create_app

ROOT = Path(__file__).resolve().parents[1]
T0 = 1_800_000_000.0


def execution_payload(message="Request failed with status 503", node="Push to sequencer", **kw):
    error = {"message": message, "node": {"name": node}, **kw.pop("error", {})}
    return {
        "execution": {
            "id": kw.pop("id", "231"),
            "url": "https://n8n.ops.example/workflow/7/executions/231",
            "error": error,
            "lastNodeExecuted": node,
            "mode": kw.pop("mode", "trigger"),
            "retryOf": kw.pop("retryOf", None),
        },
        "workflow": {"id": kw.pop("workflow_id", "7"), "name": "09 · retry loop"},
    }


class Clock:
    def __init__(self):
        self.t = T0

    def __call__(self):
        return self.t


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def log(clock):
    return AlertLog(window_seconds=900, clock=clock)


def test_execution_failure_names_workflow_node_and_execution():
    ev = parse_error_event(execution_payload(error={"httpCode": "503"}, retryOf="34"))
    assert ev.kind == "execution" and ev.workflow_name == "09 · retry loop"
    assert ev.node == "Push to sequencer" and ev.execution_id == "231" and ev.http_code == "503"
    text = format_alert(ev)
    assert "'09 · retry loop' failed at node 'Push to sequencer' (HTTP 503)" in text
    assert "execution 231, mode trigger, retry of 34: https://n8n.ops.example/" in text
    assert "GET /v1/dlq" in text  # the alert says where the leads are, since it names none


def test_trigger_failure_says_there_is_no_execution_to_open():
    ev = parse_error_event(
        {
            "trigger": {"error": {"message": "Invalid credentials"}, "mode": "trigger"},
            "workflow": {"id": "9", "name": "09 · DLQ replay"},
        }
    )
    assert ev.kind == "trigger" and ev.execution_id is None and ev.url is None
    assert "nothing to open" in format_alert(ev)


def test_node_falls_back_to_last_node_executed_and_description_is_kept():
    payload = execution_payload(message="Bad request", node="X")
    payload["execution"]["error"] = {"message": "Bad request", "description": "campaign missing"}
    ev = parse_error_event(payload)
    assert ev.node == "X" and ev.message == "Bad request — campaign missing"


def test_emails_are_masked_and_long_messages_cut():
    ev = parse_error_event(execution_payload(message="lead ana.ruiz@northwind.example rejected"))
    assert ev.message == "lead <email> rejected"
    assert "@" not in format_alert(ev)
    long = mask_message("x " * 400)
    assert len(long) == MAX_MESSAGE and long.endswith("…")


@pytest.mark.parametrize("payload", [None, [], "boom", {"workflow": {"name": "w"}}, {"x": 1}])
def test_odd_payloads_never_raise_and_are_always_sent(payload, log):
    ev = parse_error_event(payload)
    assert ev.kind == "unrecognised"
    assert log.record(ev).send and log.record(ev).send
    assert "does not recognise" in format_alert(ev)


def test_manual_sample_data_is_tagged_test_and_never_folds_into_live_alerts(log):
    live = parse_error_event(execution_payload())
    sample = parse_error_event(execution_payload(mode="manual"))
    assert sample.test and not live.test
    assert format_alert(sample).startswith("[test] ")
    assert fingerprint(sample) != fingerprint(live)
    assert log.record(sample).send and log.record(live).send


def test_ids_numbers_and_quoted_values_fold_into_one_fingerprint():
    a = parse_error_event(execution_payload(message='lead 123 "acme" 3f9a1c2b4d5e6f708192a3b4'))
    b = parse_error_event(execution_payload(message="lead 456 'globex' 0011223344556677889900aa"))
    assert fingerprint(a) == fingerprint(b)


@pytest.mark.parametrize(
    "change",
    [{"node": "Other node"}, {"error": {"httpCode": "429"}}, {"workflow_id": "8"}],
)
def test_another_node_status_or_workflow_is_another_alert(change):
    assert fingerprint(parse_error_event(execution_payload(**change))) != fingerprint(
        parse_error_event(execution_payload())
    )


def test_a_failing_schedule_alerts_once_per_window_and_reports_the_repeats(log, clock):
    ev = parse_error_event(execution_payload())
    first = log.record(ev)
    assert first.send and first.suppressed_since_last == 0 and first.previous_sent_at is None
    for _ in range(5):  # every 2 minutes, inside the 15-minute window
        clock.t += 120
        d = log.record(ev)
        assert not d.send
    assert d.suppressed_since_last == 5 and d.last_sent_at == T0
    clock.t = T0 + 900
    sent = log.record(ev)
    assert sent.send and sent.suppressed_since_last == 5 and sent.total == 7
    assert sent.previous_sent_at == T0
    text = format_alert(ev, sent)
    assert "+5 similar not sent since the alert at 2027-01-15 08:00 UTC (7 in total" in text
    clock.t += 60
    assert log.record(ev).suppressed_since_last == 1  # counter restarts after a send


def test_window_zero_sends_every_alert(log):
    ev = parse_error_event(execution_payload())
    assert all(log.record(ev, window_seconds=0).send for _ in range(3))


def test_counters_are_shared_through_one_file(tmp_path, clock):
    path = tmp_path / "alerts.db"
    ev = parse_error_event(execution_payload())
    with AlertLog(path, clock=clock) as a, AlertLog(path, clock=clock) as b:
        assert a.record(ev).send
        assert not b.record(ev).send  # a second worker sees the first one's alert


def test_negative_window_is_refused():
    with pytest.raises(ValueError):
        AlertLog(window_seconds=-1)


# --- API ---------------------------------------------------------------------------------


def api(clock, **kw):
    return TestClient(create_app(now=clock, alerts=AlertLog(clock=clock), **kw))


def test_route_answers_send_then_folds_repeats(clock):
    c = api(clock)
    first = c.post("/v1/alerts/n8n-error", json=execution_payload()).json()
    assert first["send"] and first["event"]["node"] == "Push to sequencer"
    assert "Push to sequencer" in first["text"]
    again = c.post("/v1/alerts/n8n-error", json=execution_payload(id="232")).json()
    assert not again["send"] and again["suppressed_since_last"] == 1
    assert again["fingerprint"] == first["fingerprint"]


def test_route_takes_a_window_and_accepts_any_json(clock):
    c = api(clock)
    body = execution_payload()
    assert c.post("/v1/alerts/n8n-error", json=body).json()["send"]
    assert c.post("/v1/alerts/n8n-error?window_seconds=0", json=body).json()["send"]
    assert c.post("/v1/alerts/n8n-error?window_seconds=-1", json=body).status_code == 422
    odd = c.post("/v1/alerts/n8n-error", json=["not", "an", "object"])
    assert odd.status_code == 200 and odd.json()["send"]


def test_route_needs_the_token_when_one_is_set(clock):
    c = api(clock, api_token="t0ken-for-tests")
    assert c.post("/v1/alerts/n8n-error", json=execution_payload()).status_code == 401
    ok = c.post(
        "/v1/alerts/n8n-error",
        json=execution_payload(),
        headers={"Authorization": "Bearer t0ken-for-tests"},
    )
    assert ok.status_code == 200


# --- n8n/error-alert.json replayed offline ----------------------------------------------

EWF = json.loads((ROOT / "n8n" / "error-alert.json").read_text())
ENODES = {n["name"]: n for n in EWF["nodes"]}
DECIDE = "Decide alert (POST /v1/alerts/n8n-error)"


def etargets(name, output=0):
    main = EWF["connections"].get(name, {}).get("main", [])
    return [link["node"] for link in main[output]] if output < len(main) else []


def run_error_workflow(client, payload):
    """Walk the workflow from the Error Trigger; returns the bodies posted to the alert URL."""
    from test_n8n import evaluate, run_set

    ctx = {"Error Trigger": payload}
    ctx["Config"] = run_set(ENODES["Config"], payload, ctx)
    decide = ENODES[DECIDE]["parameters"]
    assert evaluate(decide["url"], {}, ctx) == "http://reliability:8009/v1/alerts/n8n-error"
    body = evaluate(decide["jsonBody"], {}, ctx)
    posted = []
    if client is None:  # service unreachable -> error output
        (fallback,) = etargets(DECIDE, 1)
        node = ENODES[fallback]["parameters"]
        posted.append((evaluate(node["url"], {}, ctx), evaluate(node["jsonBody"], {}, ctx)))
        return posted
    answer = client.post("/v1/alerts/n8n-error", json=body).json()
    (gate,) = etargets(DECIDE, 0)
    cond = ENODES[gate]["parameters"]["conditions"]["conditions"][0]
    assert cond["operator"] == {"type": "boolean", "operation": "true", "singleValue": True}
    if evaluate(cond["leftValue"], answer, ctx) is True:
        for name in etargets(gate, 0):
            node = ENODES[name]["parameters"]
            posted.append((evaluate(node["url"], {}, ctx), evaluate(node["jsonBody"], answer, ctx)))
    assert etargets(gate, 1) == []  # repeats end quietly
    return posted


def test_error_workflow_posts_the_masked_text_once_per_window(clock):
    c = api(clock)
    payload = execution_payload(message="lead ana.ruiz@northwind.example: 503")
    (sent,) = run_error_workflow(c, payload)
    url, body = sent
    assert url == "https://alerts.ops.example/hooks/n8n"
    assert "<email>" in body["text"] and "@northwind" not in json.dumps(body)
    assert run_error_workflow(c, payload) == []
    clock.t += 900
    (later,) = run_error_workflow(c, payload)
    assert "+1 similar" in later[1]["text"]


def test_error_workflow_still_alerts_when_the_service_is_down():
    payload = execution_payload(message="lead ana.ruiz@northwind.example: 503")
    ((url, body),) = run_error_workflow(None, payload)
    assert url == "https://alerts.ops.example/hooks/n8n"
    assert body["workflow"] == "09 · retry loop"
    assert body["execution_url"].endswith("/executions/231")
    assert "northwind" not in json.dumps(body)  # no unmasked error message in the fallback


def test_decide_call_never_retries_and_routes_failures_to_the_fallback():
    node = ENODES[DECIDE]
    assert node["onError"] == "continueErrorOutput" and node["retryOnFail"] is False
    assert node["parameters"]["authentication"] == "genericCredentialType"
    assert EWF["connections"]["Error Trigger"]["main"][0][0]["node"] == "Config"
    assert ENODES["Error Trigger"]["type"] == "n8n-nodes-base.errorTrigger"
    assert "errorWorkflow" not in EWF["settings"]  # never its own error workflow


@pytest.mark.parametrize("name", ["retry-loop.json", "dlq-replay.json"])
def test_kit_workflows_name_the_error_workflow_and_save_failed_executions(name):
    settings = json.loads((ROOT / "n8n" / name).read_text())["settings"]
    assert settings["errorWorkflow"] == "REPLACE_error_alert_workflow_id"
    assert settings["saveDataErrorExecution"] == "all"  # else the alert's link opens nothing
