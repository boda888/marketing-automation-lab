"""DlqStore + /v1/dlq: one row per key, replay only what is safe to send again."""

import pytest
from fastapi.testclient import TestClient
from reliability import DlqStore, RowClosed, RowNotFound
from reliability.api import create_app

NOW = 1_800_000_000.0
SCOPE = "smartlead:campaign-42"


class Clock:
    def __init__(self, t: float = NOW) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def row(key, outcome, *, reason=None, attempts=1, last_status=None, retry_at=None, safe=None):
    out = {
        "key": key,
        "outcome": outcome,
        "reason": reason or outcome,
        "attempts": attempts,
        "last_status": last_status,
        "retry_at": retry_at,
    }
    if safe is not None:
        out["safe_to_resend"] = safe
    return out


def lead(email):
    return {"email": email, "company": "Acme", "first_line": "Saw the new office."}


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def store(clock):
    with DlqStore(clock=clock) as s:
        yield s


def test_intake_stores_row_and_recomputes_safe_to_resend(store):
    r = store.intake(
        row(
            "ana@acme.example", "unknown", reason="http_502_after_send", last_status=502, safe=True
        ),
        scope=SCOPE,
        payload=lead("ana@acme.example"),
        source={"workflow": "push", "node": "Push lead", "execution_id": "77"},
    )
    assert r.state == "open" and r.count == 1 and r.replays == 0
    assert r.safe_to_resend is False  # caller's safe_to_resend: true is ignored
    assert r.payload == lead("ana@acme.example")
    assert r.source["node"] == "Push lead"
    assert r.first_seen_at == NOW


@pytest.mark.parametrize(
    "bad, msg",
    [
        (row("a@acme.example", "ok"), "outcome"),
        (row("", "failed"), "blank"),
        (row("a@acme.example", "deferred"), "retry_at"),
    ],
)
def test_intake_rejects_rows_that_do_not_belong(store, bad, msg):
    with pytest.raises(ValueError, match=msg):
        store.intake(bad, scope=SCOPE)


def test_payload_size_is_capped(store):
    with pytest.raises(ValueError, match="payload"):
        store.intake(row("a@acme.example", "failed"), scope=SCOPE, payload="x" * 70_000)


def test_repeat_failure_of_one_key_updates_the_same_row(store, clock):
    first = store.intake(row("bo@acme.example", "exhausted", attempts=4), scope=SCOPE)
    clock.t += 60
    again = store.intake(row("bo@acme.example", "unknown", reason="timeout"), scope=SCOPE)
    assert again.id == first.id and again.count == 2
    assert again.outcome == "unknown" and again.reason == "timeout"
    assert again.first_seen_at == NOW and again.last_seen_at == NOW + 60
    assert again.payload is None
    # another scope is another row
    other = store.intake(row("bo@acme.example", "exhausted"), scope="hubspot:contacts")
    assert other.id != first.id
    assert len(store.rows(state=None)) == 2


def test_repeat_without_payload_keeps_the_stored_payload(store):
    store.intake(row("cy@acme.example", "exhausted"), scope=SCOPE, payload=lead("cy@acme.example"))
    r = store.intake(row("cy@acme.example", "exhausted"), scope=SCOPE)
    assert r.payload == lead("cy@acme.example")


def test_replay_hands_out_only_safe_and_due_rows(store, clock):
    ids = {
        key: store.intake(row(key, outcome, retry_at=retry_at), scope=SCOPE, payload=lead(key)).id
        for key, outcome, retry_at in [
            ("ex@acme.example", "exhausted", None),
            ("un@acme.example", "unknown", None),
            ("fa@acme.example", "failed", None),
            ("dl@acme.example", "deferred", NOW + 900),
        ]
    }
    items = {i.key: i for i in store.replay()}
    assert items["ex@acme.example"].status == "replay"
    assert items["ex@acme.example"].payload == lead("ex@acme.example")
    assert items["un@acme.example"].status == "unsafe_to_resend"
    assert "read the vendor back" in items["un@acme.example"].reason
    assert items["fa@acme.example"].status == "unsafe_to_resend"
    assert items["dl@acme.example"].status == "not_due"
    assert items["un@acme.example"].payload is None  # refused rows never carry a payload

    assert store.get(ids["ex@acme.example"]).state == "replayed"
    assert store.get(ids["un@acme.example"]).state == "open"
    # nothing is handed out twice
    assert [i.status for i in store.replay([ids["ex@acme.example"]])] == ["not_open"]

    clock.t = NOW + 900
    assert [i.key for i in store.replay() if i.go] == ["dl@acme.example"]


def test_dry_run_changes_nothing(store):
    rid = store.intake(row("dr@acme.example", "exhausted"), scope=SCOPE).id
    assert store.replay(dry_run=True)[0].go
    assert store.get(rid).state == "open" and store.get(rid).replays == 0


def test_replay_by_ids_reports_unknown_ids_and_respects_scope(store):
    rid = store.intake(row("sc@acme.example", "exhausted"), scope=SCOPE).id
    items = store.replay([rid, 999, rid], scope="hubspot:contacts")
    assert [(i.id, i.status) for i in items] == [(rid, "not_open"), (999, "not_found")]


def test_failed_replay_reopens_the_row_and_the_limit_stops_a_poison_item(clock):
    with DlqStore(clock=clock, max_replays=2) as store:
        key = "po@acme.example"
        rid = store.intake(row(key, "exhausted"), scope=SCOPE, payload=lead(key)).id
        for n in (1, 2):
            assert store.replay()[0].go
            again = store.intake(row(key, "exhausted"), scope=SCOPE)
            assert again.id == rid and again.state == "open" and again.replays == n
        item = store.replay()[0]
        assert item.status == "replay_limit" and not item.go


def test_resolve_after_read_back(store):
    landed = store.intake(row("la@acme.example", "unknown"), scope=SCOPE).id
    lost = store.intake(row("lo@acme.example", "unknown", reason="timeout"), scope=SCOPE).id
    r = store.resolve(landed, landed=True, note="found in campaign")
    assert r.state == "resolved" and r.closed_at == NOW and r.note == "found in campaign"
    r = store.resolve(lost, landed=False)
    assert r.state == "open" and r.outcome == "exhausted" and r.safe_to_resend
    assert r.reason == "timeout; read back: not landed"
    assert [i.key for i in store.replay() if i.go] == ["lo@acme.example"]
    # a resolved key that fails again is a new incident, not the closed row
    again = store.intake(row("la@acme.example", "failed"), scope=SCOPE)
    assert again.id != landed


def test_resolve_and_discard_errors(store):
    failed = store.intake(row("fx@acme.example", "failed", last_status=400), scope=SCOPE).id
    with pytest.raises(ValueError, match="rejected"):
        store.resolve(failed, landed=False)
    assert store.discard(failed, note="bad email").state == "discarded"
    with pytest.raises(RowClosed):
        store.discard(failed)
    with pytest.raises(RowNotFound):
        store.resolve(12345, landed=True)


def test_summary_for_alerts(store, clock):
    store.intake(row("s1@acme.example", "unknown"), scope=SCOPE)
    clock.t += 30
    store.intake(row("s2@acme.example", "failed"), scope=SCOPE)
    rid = store.intake(row("s3@acme.example", "exhausted"), scope="hubspot:contacts").id
    store.discard(rid)
    clock.t += 30
    s = store.summary()
    assert s["states"] == {"open": 2, "replayed": 0, "resolved": 0, "discarded": 1}
    assert s["open_by_outcome"] == {"deferred": 0, "exhausted": 0, "failed": 1, "unknown": 1}
    assert s["open_unsafe"] == 2 and s["oldest_open_age_seconds"] == 60.0
    assert store.summary(scope="hubspot:contacts")["oldest_open_age_seconds"] is None


def test_rows_survive_a_restart(tmp_path, clock):
    path = tmp_path / "dlq.sqlite"
    with DlqStore(path, clock=clock) as s:
        s.intake(row("re@acme.example", "exhausted"), scope=SCOPE, payload=lead("re@acme.example"))
    with DlqStore(path, clock=clock) as s:
        assert [r.key for r in s.rows()] == ["re@acme.example"]
        assert s.replay()[0].payload == lead("re@acme.example")


# --- HTTP ---------------------------------------------------------------------------------


def api(clock, **kw):
    return TestClient(create_app(now=clock, rand=lambda: 0.5, dlq=DlqStore(clock=clock), **kw))


def test_decide_stop_flows_into_dlq_and_replay(clock):
    c = api(clock)
    decision = c.post(
        "/v1/decide",
        json={"attempt": 4, "method": "POST", "status": 429, "key": "hx@acme.example"},
    ).json()
    unknown = c.post(
        "/v1/decide",
        json={"attempt": 1, "method": "POST", "status": 502, "key": "hu@acme.example"},
    ).json()
    assert decision["outcome"] == "exhausted" and unknown["outcome"] == "unknown"
    for d in (decision, unknown):
        r = c.post(
            "/v1/dlq",
            json={"scope": SCOPE, "row": d["dlq_row"], "payload": lead(d["dlq_row"]["key"])},
        )
        assert r.status_code == 200
    listed = c.get("/v1/dlq").json()
    assert listed["count"] == 2

    replay = c.post("/v1/dlq/replay", json={}).json()
    assert [i["key"] for i in replay["replay"]] == ["hx@acme.example"]
    assert replay["replay"][0]["payload"] == lead("hx@acme.example")
    assert [(i["key"], i["status"]) for i in replay["refused"]] == [
        ("hu@acme.example", "unsafe_to_resend")
    ]
    assert c.get("/v1/dlq", params={"state": "all"}).json()["count"] == 2
    assert c.get("/v1/dlq/summary").json()["open_unsafe"] == 1


def test_http_resolve_discard_and_errors(clock):
    c = api(clock)
    rid = c.post("/v1/dlq", json={"scope": SCOPE, "row": row("hr@acme.example", "unknown")}).json()[
        "id"
    ]
    r = c.post(f"/v1/dlq/{rid}/resolve", json={"landed": False})
    assert r.status_code == 200 and r.json()["safe_to_resend"] is True
    assert c.post(f"/v1/dlq/{rid}/discard", json={"note": "unsubscribed"}).json()["state"] == (
        "discarded"
    )
    assert c.post(f"/v1/dlq/{rid}/discard", json={}).status_code == 409
    assert c.post("/v1/dlq/999/resolve", json={"landed": True}).status_code == 404
    bad = c.post("/v1/dlq", json={"scope": SCOPE, "row": row("ok@acme.example", "ok")})
    assert bad.status_code == 422
    assert c.post("/v1/dlq", json={"scope": SCOPE, "row": {}, "extra": 1}).status_code == 422


def test_dlq_routes_need_the_token(clock):
    c = api(clock, api_token="s3cret")
    assert c.get("/v1/dlq").status_code == 401
    assert c.post("/v1/dlq/replay", json={}).status_code == 401
    ok = c.get("/v1/dlq/summary", headers={"Authorization": "Bearer s3cret"})
    assert ok.status_code == 200
