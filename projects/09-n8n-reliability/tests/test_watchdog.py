"""Dead man's switch per schedule: period + grace, start without finish, one alert per episode.

Offline only: a fake clock, the FastAPI app through TestClient, and n8n/watchdog.json plus
the heartbeat at the end of n8n/dlq-replay.json replayed by walking their connections.
"""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from reliability.alerts import AlertLog
from reliability.api import create_app
from reliability.dedupe import KeyStore
from reliability.watchdog import UnknownWatch, Watchdog

ROOT = Path(__file__).resolve().parents[1]
T0 = 1_800_000_000.0
HOUR = 3600.0


class Clock:
    def __init__(self):
        self.t = T0

    def __call__(self):
        return self.t


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def dog(clock):
    w = Watchdog(realert_seconds=6 * HOUR, clock=clock)
    w.configure("nightly-sync", period_seconds=HOUR, grace_seconds=600)
    return w


def state(dog, name="nightly-sync"):
    return {s.name: s.state for s in dog.statuses()}[name]


def test_up_then_late_inside_grace_then_down(dog, clock):
    dog.ping("nightly-sync")
    assert state(dog) == "up"
    clock.t += HOUR + 1
    assert state(dog) == "late"
    clock.t += 600
    (st,) = dog.statuses()
    assert st.state == "down" and st.overdue_seconds == 1.0
    assert "last run finished 1h10m ago (period 1h00m)" in st.reason


def test_a_schedule_that_never_ran_goes_down_after_period_plus_grace(dog, clock):
    assert state(dog) == "new"
    clock.t += HOUR + 600
    assert state(dog) == "new"  # due exactly now, not yet past it
    clock.t += 1
    _, alerts = dog.check()
    assert [a.transition for a in alerts] == ["down"]
    assert "no finished run since it was configured" in alerts[0].text


def test_a_run_that_started_and_never_finished_is_down_after_grace(dog, clock):
    dog.ping("nightly-sync")
    clock.t += 1800
    dog.ping("nightly-sync", "start", execution="88")
    clock.t += 600
    assert state(dog) == "up"  # running for 10 min = grace; the last ok is still in period
    clock.t += 1
    (st,) = dog.statuses()
    assert st.state == "down" and st.reason.startswith("started 10m ago and never finished")
    assert st.last_execution == "88"
    dog.ping("nightly-sync", execution="88")
    assert state(dog) == "up"


def test_one_alert_per_down_episode_then_realert_then_recovered(dog, clock):
    dog.ping("nightly-sync")
    clock.t += 2 * HOUR
    assert [a.transition for a in dog.check()[1]] == ["down"]
    for _ in range(5):  # watchdog every 10 minutes: silent while it stays down
        clock.t += 600
        assert dog.check()[1] == []
    clock.t = T0 + 2 * HOUR + 6 * HOUR
    (again,) = dog.check()[1]
    assert again.transition == "still_down" and "still down for 6h00m" in again.text
    dog.ping("nightly-sync")
    (back,) = dog.check()[1]
    assert back.transition == "recovered" and "recovered after 6h00m down" in back.text
    assert dog.check()[1] == []


def test_pause_silences_and_unpause_starts_a_fresh_episode(dog, clock):
    clock.t += 2 * HOUR
    assert dog.check()[1]
    dog.pause("nightly-sync")
    clock.t += 7 * HOUR
    statuses, alerts = dog.check()
    assert alerts == [] and statuses[0].state == "paused"
    dog.pause("nightly-sync", False)
    assert [a.transition for a in dog.check()[1]] == ["down"]


def test_unknown_names_and_bad_timing_are_refused(dog):
    with pytest.raises(UnknownWatch):
        dog.ping("nightly-synk")
    with pytest.raises(UnknownWatch):
        dog.pause("nope")
    with pytest.raises(ValueError):
        dog.configure("x", period_seconds=0, grace_seconds=1)
    with pytest.raises(ValueError):
        dog.ping("nightly-sync", "finish")
    with pytest.raises(ValueError):
        Watchdog(realert_seconds=0)


def test_reconfigure_keeps_pings(dog, clock):
    dog.ping("nightly-sync")
    clock.t += 2 * HOUR
    dog.configure("nightly-sync", period_seconds=3 * HOUR, grace_seconds=0)
    assert state(dog) == "up"


def test_two_checkers_on_one_file_alert_once(tmp_path, clock):
    path = tmp_path / "watch.db"
    with Watchdog(path, clock=clock) as a, Watchdog(path, clock=clock) as b:
        a.configure("s", period_seconds=60, grace_seconds=0)
        clock.t += 61
        assert len(a.check()[1]) == 1 and b.check()[1] == []


# --- API ---------------------------------------------------------------------------------

TOKEN = "demo-reliability-token-0123456789"  # the token test_n8n's replays send
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def api(clock, keys=None):
    app = create_app(
        api_token=TOKEN,
        now=clock,
        store=keys or KeyStore(clock=clock),
        alerts=AlertLog(window_seconds=HOUR, clock=clock),
        watchdog=Watchdog(clock=clock),
    )
    return TestClient(app)


def test_routes_configure_ping_and_check(clock):
    c = api(clock)
    timing = {"period_seconds": 1800, "grace_seconds": 600}
    assert c.put("/v1/watchdog/checks/dlq-replay", json=timing).status_code == 401
    r = c.put("/v1/watchdog/checks/dlq-replay", json=timing, headers=AUTH)
    assert r.status_code == 200 and r.json()["state"] == "new"
    assert c.post("/v1/watchdog/check").status_code == 401
    ping = c.post("/v1/watchdog/checks/dlq-replay/ping", json={"execution": "5"}, headers=AUTH)
    assert ping.json()["state"] == "up" and ping.json()["last_execution"] == "5"
    quiet = c.post("/v1/watchdog/check", headers=AUTH).json()
    assert quiet["alert"] is False and quiet["text"] is None and quiet["counts"]["up"] == 1
    clock.t += 2401
    loud = c.post("/v1/watchdog/check", headers=AUTH).json()
    assert loud["alert"] and "schedule 'dlq-replay' is down" in loud["text"]
    assert c.get("/v1/watchdog/checks", headers=AUTH).json()["watches"][0]["state"] == "down"


def test_route_errors(clock):
    c = api(clock)
    missing = c.post("/v1/watchdog/checks/typo/ping", json={}, headers=AUTH)
    assert missing.status_code == 404
    zero = {"period_seconds": 0, "grace_seconds": 0}
    bad = c.put("/v1/watchdog/checks/x", json=zero, headers=AUTH)
    assert bad.status_code == 422
    sig = c.post("/v1/watchdog/checks/x/ping", json={"signal": "done"}, headers=AUTH)
    assert sig.status_code == 422
    assert c.post("/v1/watchdog/checks/x/pause", json={}, headers=AUTH).status_code == 404


def test_stale_reservations_alert_once_per_scope_and_window(clock):
    keys = KeyStore(stale_after=900, clock=clock)
    c = api(clock, keys)
    keys.claim("sequencer:campaign-demo", "ana@northwind.example")
    keys.claim("sequencer:campaign-demo", "bo@bluefin.example")
    assert c.post("/v1/watchdog/check", headers=AUTH).json()["stale_keys"]["count"] == 0
    clock.t += 900
    first = c.post("/v1/watchdog/check", headers=AUTH).json()
    assert first["stale_keys"] == {"count": 2, "by_scope": {"sequencer:campaign-demo": 2}}
    (alert,) = first["alerts"]
    assert alert["source"] == "stale_keys" and "2 key reservation(s)" in alert["text"]
    assert "@" not in first["text"]  # scopes and counts only, never the lead keys
    clock.t += 600
    again = c.post("/v1/watchdog/check", headers=AUTH).json()
    assert again["alert"] is False and again["stale_keys"]["count"] == 2
    clock.t += HOUR
    assert c.post("/v1/watchdog/check", headers=AUTH).json()["alert"] is True


# --- n8n: watchdog.json + the heartbeat at the end of dlq-replay.json --------------------

WWF = json.loads((ROOT / "n8n" / "watchdog.json").read_text())
WNODES = {n["name"]: n for n in WWF["nodes"]}


def wtargets(name, output=0):
    main = WWF["connections"].get(name, {}).get("main", [])
    return [link["node"] for link in main[output]] if output < len(main) else []


def watchdog_tick(client):
    """One run of watchdog.json; returns the bodies posted to the alert URL."""
    from test_n8n import SERVICE, evaluate, run_set

    (trigger,) = [n for n in WWF["nodes"] if n["type"].endswith("scheduleTrigger")]
    (config,) = wtargets(trigger["name"])
    ctx = {config: run_set(WNODES[config], {}, {})}
    (check,) = wtargets(config)
    params = WNODES[check]["parameters"]
    path = evaluate(params["url"], {}, ctx).removeprefix(SERVICE)
    assert "sendBody" not in params  # the check takes no body
    r = client.post(path, headers=AUTH)
    assert r.status_code == 200, r.text  # no continue-on-error: a down service fails the run
    answer = r.json()
    (gate,) = wtargets(check)
    cond = WNODES[gate]["parameters"]["conditions"]["conditions"][0]
    assert cond["operator"]["operation"] == "true"
    posted = []
    for name in wtargets(gate, 0 if evaluate(cond["leftValue"], answer, ctx) is True else 1):
        node = WNODES[name]["parameters"]
        posted.append((evaluate(node["url"], {}, ctx), evaluate(node["jsonBody"], answer, ctx)))
    return posted


def test_watchdog_workflow_is_wired_like_the_rest_of_the_kit():
    check = WNODES["Check schedules (POST /v1/watchdog/check)"]
    assert "onError" not in check and check["retryOnFail"] is False
    assert check["parameters"]["genericAuthType"] == "httpHeaderAuth"
    assert WWF["settings"]["errorWorkflow"] == "REPLACE_error_alert_workflow_id"
    assert wtargets("Anything to say?", 1) == []


def test_dlq_replay_heartbeat_keeps_the_watchdog_quiet_and_its_absence_alerts(clock):
    from test_n8n import tick

    c = api(clock)
    r = c.put(
        "/v1/watchdog/checks/dlq-replay",
        json={"period_seconds": 1800, "grace_seconds": 600},
        headers=AUTH,
    )
    assert r.status_code == 200
    for _ in range(3):  # dlq-replay runs every 30 min and pings at the end of each run
        assert tick(c, clock)["heartbeat"] == 200
        assert watchdog_tick(c) == []
        clock.t += 1800
    clock.t += 601  # the schedule stopped: no run, no error, no error workflow
    ((url, body),) = watchdog_tick(c)
    assert url == "https://alerts.ops.example/hooks/n8n"
    assert "schedule 'dlq-replay' is down" in body["text"] and body["counts"]["down"] == 1
    clock.t += 600
    assert watchdog_tick(c) == []  # still down: no new alert until the re-alert interval
    assert tick(c, clock)["heartbeat"] == 200
    ((_, back),) = watchdog_tick(c)
    assert "recovered" in back["text"]


def test_a_failed_ping_does_not_fail_the_replay_run(clock):
    from test_n8n import tick

    c = api(clock)  # dlq-replay never configured: the ping gets 404
    assert tick(c, clock)["heartbeat"] == 404
