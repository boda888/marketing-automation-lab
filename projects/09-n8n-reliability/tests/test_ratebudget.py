"""Shared rate budget: several windows per account scope, cost per call, Retry-After block."""

import multiprocessing
import sqlite3

import pytest
from fastapi.testclient import TestClient
from reliability import (
    PRESETS,
    CostTooHigh,
    RateBudget,
    UnknownScope,
    Window,
)
from reliability.api import create_app


class Clock:
    def __init__(self, t: float = 1_800_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def tick(self, s: float) -> None:
        self.t += s


def budget(clock, scope="apollo:team-1", windows=((60, 3), (3600, 5))):
    b = RateBudget(clock=clock)
    b.configure(scope, windows)
    return b


def test_grants_until_the_shortest_window_is_full_then_answers_the_wait():
    clock = Clock()
    b = budget(clock)
    assert [b.acquire("apollo:team-1").granted for _ in range(3)] == [True] * 3
    clock.tick(10)
    g = b.acquire("apollo:team-1")
    assert not g.granted and g.blocked_by == "60s"
    assert g.wait_seconds == pytest.approx(50) and g.wait_ms == 50_000
    clock.tick(50)
    assert b.acquire("apollo:team-1").granted


def test_refused_calls_are_not_recorded():
    clock = Clock()
    b = budget(clock, windows=((60, 1),))
    assert b.acquire("apollo:team-1").granted
    for _ in range(5):
        assert not b.acquire("apollo:team-1").granted
    clock.tick(60)
    g = b.acquire("apollo:team-1")
    assert g.granted and g.usage[0].used == 1


def test_longer_window_blocks_after_the_minute_window_has_room_again():
    # Apollo-style: the minute window frees up, the hour window does not.
    clock = Clock()
    b = budget(clock)
    for _ in range(3):
        assert b.acquire("apollo:team-1").granted
    clock.tick(60)
    for _ in range(2):
        assert b.acquire("apollo:team-1").granted
    clock.tick(60)
    g = b.acquire("apollo:team-1")
    assert not g.granted and g.blocked_by == "3600s"
    # the oldest of the 5 calls (t=0) leaves the hour window at t=3600; now is t=120
    assert g.wait_seconds == pytest.approx(3480)
    assert [(u.seconds, u.used, u.remaining) for u in g.usage] == [(60, 0, 3), (3600, 5, 0)]


def test_cost_charges_more_than_one_slot_and_waits_for_enough_to_expire():
    clock = Clock()
    b = RateBudget(clock=clock)
    b.configure("pipedrive:company", [(86400, 100)])
    assert b.acquire("pipedrive:company", cost=40).granted  # a search, t=0
    clock.tick(100)
    assert b.acquire("pipedrive:company", cost=20).granted  # a list, t=100
    clock.tick(100)
    assert b.acquire("pipedrive:company", cost=40).granted  # t=200, 100 used
    clock.tick(100)
    g = b.acquire("pipedrive:company", cost=20)
    assert not g.granted
    # 20 more fits once the first 40 has expired: t=86400, now t=300
    assert g.wait_seconds == pytest.approx(86100)


def test_cost_above_a_limit_raises_instead_of_waiting_forever():
    b = budget(Clock())
    with pytest.raises(CostTooHigh):
        b.acquire("apollo:team-1", cost=4)
    with pytest.raises(ValueError):
        b.acquire("apollo:team-1", cost=0)


def test_unknown_scope_fails_closed():
    b = RateBudget(clock=Clock())
    with pytest.raises(UnknownScope):
        b.acquire("instantly:ws-north")
    with pytest.raises(UnknownScope):
        b.penalize("instantly:ws-north", 5)


def test_scopes_are_independent():
    clock = Clock()
    b = budget(clock, windows=((60, 1),))
    b.configure("apollo:team-2", [(60, 1)])
    assert b.acquire("apollo:team-1").granted
    assert b.acquire("apollo:team-2").granted
    assert not b.acquire("apollo:team-1").granted


def test_retry_after_blocks_the_whole_scope_even_with_free_windows():
    clock = Clock()
    b = budget(clock)
    until = b.penalize("apollo:team-1", 30)
    assert until == clock.t + 30
    g = b.acquire("apollo:team-1")
    assert not g.granted and g.blocked_by == "retry_after" and g.wait_ms == 30_000
    assert b.penalize("apollo:team-1", 5) == until  # a shorter hint never shortens it
    clock.tick(30)
    assert b.acquire("apollo:team-1").granted
    assert b.usage("apollo:team-1")["blocked_until"] is None


def test_penalty_without_retry_after_uses_the_shortest_window():
    clock = Clock()
    b = budget(clock)
    assert b.penalize("apollo:team-1") == clock.t + 60


def test_reconfigure_keeps_the_call_log():
    clock = Clock()
    b = budget(clock, windows=((60, 5),))
    for _ in range(3):
        b.acquire("apollo:team-1")
    b.configure("apollo:team-1", [(60, 2)])
    assert not b.acquire("apollo:team-1").granted


def test_windows_are_validated():
    b = RateBudget(clock=Clock())
    for bad in ([], [(60, 0)], [(0, 5)], [(60, 5), (60, 10)]):
        with pytest.raises(ValueError):
            b.configure("x", bad)
    with pytest.raises(ValueError):
        b.configure("x", "no-such-preset")
    assert b.configure("x", [(3600, 10), (1, 2)]) == (Window(1, 2), Window(3600, 10))


def test_presets_have_ascending_windows_and_a_source():
    b = RateBudget(clock=Clock())
    for name, p in PRESETS.items():
        assert b.configure(f"t:{name}", name) == p.windows
        assert p.source and p.confidence
    assert [(w.seconds, w.limit) for w in PRESETS["instantly"].windows] == [(1, 100), (60, 6000)]


def test_old_events_are_pruned():
    clock = Clock()
    b = budget(clock, windows=((60, 10),))
    for _ in range(5):
        b.acquire("apollo:team-1")
    clock.tick(61)
    b.acquire("apollo:team-1")
    rows = b._db.execute("SELECT COUNT(*) FROM rate_events").fetchone()[0]
    assert rows == 1


def _worker(path: str, n: int, q) -> None:
    b = RateBudget(path)
    q.put(sum(b.acquire("instantly:ws-north").granted for _ in range(n)))
    b.close()


def test_two_processes_on_one_file_share_the_limit(tmp_path):
    # Two queue-mode workers: each tries 40 calls, the scope allows 50 per hour in total.
    path = str(tmp_path / "rate.db")
    with RateBudget(path) as b:
        b.configure("instantly:ws-north", [(3600, 50)])
    ctx = multiprocessing.get_context("spawn")
    q = ctx.Queue()
    procs = [ctx.Process(target=_worker, args=(path, 40, q)) for _ in range(2)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(60)
    assert all(p.exitcode == 0 for p in procs)
    assert q.get(timeout=5) + q.get(timeout=5) == 50
    db = sqlite3.connect(path)
    assert db.execute("SELECT SUM(cost) FROM rate_events").fetchone()[0] == 50
    db.close()


# --- HTTP ---------------------------------------------------------------------------


def client(clock, **kw):
    return TestClient(create_app(now=clock, rand=lambda: 0.5, rates=RateBudget(clock=clock), **kw))


def test_api_configure_acquire_wait_and_usage():
    clock = Clock()
    c = client(clock)
    r = c.post(
        "/v1/rate/scopes",
        json={"scope": "smartlead:pool", "windows": [{"seconds": 60, "limit": 2}]},
    )
    assert r.status_code == 200 and r.json()["windows"][0]["remaining"] == 2
    assert c.post("/v1/rate/acquire", json={"scope": "smartlead:pool"}).json()["granted"]
    assert c.post("/v1/rate/acquire", json={"scope": "smartlead:pool"}).json()["granted"]
    clock.tick(15)
    body = c.post("/v1/rate/acquire", json={"scope": "smartlead:pool"}).json()
    assert body["granted"] is False and body["wait_ms"] == 45_000
    assert body["retry_at"] == clock.t + 45 and body["blocked_by"] == "60s"
    usage = c.get("/v1/rate/usage", params={"scope": "smartlead:pool"}).json()
    assert usage["windows"] == [{"seconds": 60, "limit": 2, "used": 2, "remaining": 0}]


def test_api_preset_and_presets_listing():
    c = client(Clock())
    r = c.post("/v1/rate/scopes", json={"scope": "apollo:team-1", "preset": "apollo_basic"})
    assert [w["limit"] for w in r.json()["windows"]] == [200, 400, 2000]
    listing = c.get("/v1/rate/presets").json()
    assert "search summary" in listing["apollo_basic"]["confidence"]


def test_api_rejects_bad_bodies_and_unknown_scope():
    c = client(Clock())
    assert c.post("/v1/rate/scopes", json={"scope": "x"}).status_code == 422
    both = {"scope": "x", "preset": "instantly", "windows": [{"seconds": 1, "limit": 1}]}
    assert c.post("/v1/rate/scopes", json=both).status_code == 422
    assert c.post("/v1/rate/scopes", json={"scope": "x", "preset": "nope"}).status_code == 422
    assert c.post("/v1/rate/acquire", json={"scope": "missing"}).status_code == 404
    assert c.get("/v1/rate/usage", params={"scope": "missing"}).status_code == 404
    c.post("/v1/rate/scopes", json={"scope": "x", "windows": [{"seconds": 60, "limit": 2}]})
    assert c.post("/v1/rate/acquire", json={"scope": "x", "cost": 3}).status_code == 422


def test_api_penalty_reads_retry_after_from_n8n_headers():
    clock = Clock()
    c = client(clock)
    c.post("/v1/rate/scopes", json={"scope": "apollo:team-1", "preset": "apollo_basic"})
    r = c.post(
        "/v1/rate/penalty",
        json={"scope": "apollo:team-1", "headers": {"retry-after": ["120"]}},
    ).json()
    assert r["wait_ms"] == 120_000 and r["retry_after_seconds"] == 120
    body = c.post("/v1/rate/acquire", json={"scope": "apollo:team-1"}).json()
    assert body["blocked_by"] == "retry_after" and body["wait_ms"] == 120_000
    # no header at all -> shortest window (60 s); the longer block stays
    r2 = c.post("/v1/rate/penalty", json={"scope": "apollo:team-1"}).json()
    assert r2["wait_ms"] == 120_000


def test_api_rate_routes_need_the_token():
    c = client(Clock(), api_token="s3cret")
    assert c.post("/v1/rate/acquire", json={"scope": "x"}).status_code == 401
    assert c.get("/v1/rate/presets").status_code == 401
    ok = c.get("/v1/rate/presets", headers={"Authorization": "Bearer s3cret"})
    assert ok.status_code == 200


def test_api_acquire_answers_go_wait_or_a_deferred_stop_past_max_wait():
    clock = Clock()
    c = client(clock)
    c.post("/v1/rate/scopes", json={"scope": "s", "windows": [{"seconds": 60, "limit": 1}]})
    body = {"scope": "s", "max_wait_ms": 30_000, "key": "ana@acme.example", "attempt": 2}
    go = c.post("/v1/rate/acquire", json=body).json()
    assert go["action"] == "go" and go["granted"] and go["dlq_row"] is None
    # 60 s left in the window: past max_wait -> stop, shaped like a /v1/decide stop
    stop = c.post("/v1/rate/acquire", json=body).json()
    assert stop["action"] == "stop" and stop["outcome"] == "deferred"
    assert stop["safe_to_resend"] is True and stop["retry_at_iso"].endswith("+00:00")
    assert stop["dlq_row"] == {
        "key": "ana@acme.example",
        "outcome": "deferred",
        "reason": "rate_budget_60s",
        "attempts": 1,  # calls made: attempt 2 never happened
        "last_status": None,
        "retry_at": clock.t + 60,  # still t0 here
        "safe_to_resend": True,
    }
    clock.tick(40)  # 20 s left: within max_wait
    wait = c.post("/v1/rate/acquire", json=body).json()
    assert wait["action"] == "wait" and wait["wait_ms"] == 20_000 and wait["dlq_row"] is None
    # without max_wait_ms nothing is ever stopped (the old contract)
    assert c.post("/v1/rate/acquire", json={"scope": "s"}).json()["action"] == "wait"
    # the stop row goes into the DLQ as is, attempts 0 included
    row = dict(stop["dlq_row"], attempts=0)
    stored = c.post("/v1/dlq", json={"scope": "seq:camp", "row": row}).json()
    assert stored["attempts"] == 0 and stored["outcome"] == "deferred"


def test_api_decide_with_rate_scope_blocks_the_scope_on_429_only():
    clock = Clock()
    c = client(clock)
    c.post("/v1/rate/scopes", json={"scope": "inst:ws", "windows": [{"seconds": 1, "limit": 9}]})
    ok = {"attempt": 1, "method": "POST", "statusCode": 201, "rate_scope": "inst:ws"}
    assert c.post("/v1/decide", json=ok).json()["rate_blocked_until"] is None
    limited = {**ok, "statusCode": 429, "headers": {"retry-after": "12"}}
    d = c.post("/v1/decide", json=limited).json()
    assert d["action"] == "retry" and d["rate_blocked_until"] == clock.t + 12
    other = c.post("/v1/rate/acquire", json={"scope": "inst:ws"}).json()
    assert other["blocked_by"] == "retry_after" and other["wait_ms"] == 12_000
    # no rate_scope: decide stays stateless; an unknown scope fails loudly
    plain = {k: v for k, v in limited.items() if k != "rate_scope"}
    assert c.post("/v1/decide", json=plain).json()["rate_blocked_until"] is None
    missing = {**limited, "rate_scope": "never:configured"}
    assert c.post("/v1/decide", json=missing).status_code == 404
