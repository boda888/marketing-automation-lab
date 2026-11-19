"""KeyStore: a key is marked done only after the guarded call succeeded."""

import itertools
import threading

import pytest
from fastapi.testclient import TestClient
from reliability import (
    AlreadyCommitted,
    KeyStore,
    NotReserved,
    StillInFlight,
    TokenMismatch,
)
from reliability.api import create_app

SCOPE = "smartlead:campaign-42"


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture()
def clock():
    return Clock()


@pytest.fixture()
def store(clock):
    counter = itertools.count(1)
    with KeyStore(stale_after=900, clock=clock, new_token=lambda: f"tok-{next(counter)}") as s:
        yield s


def test_first_claim_reserves_and_a_second_is_in_flight(store, clock):
    first = store.claim(SCOPE, "ana@acme.example")
    assert first.status == "reserved" and first.go and first.token == "tok-1"
    clock.t += 30
    second = store.claim(SCOPE, "ana@acme.example")
    assert second.status == "in_flight" and not second.go and second.token is None
    assert second.age_seconds == 30


def test_commit_after_success_makes_later_claims_skip(store, clock):
    c = store.claim(SCOPE, "ana@acme.example")
    clock.t += 5
    store.commit(SCOPE, "ana@acme.example", c.token)
    store.commit(SCOPE, "ana@acme.example", c.token)  # repeat is a no-op
    clock.t += 3600
    again = store.claim(SCOPE, "ana@acme.example")
    assert again.status == "committed" and again.since == clock.t - 3600
    assert store.counts() == {"reserved": 0, "committed": 1}


def test_failed_push_is_released_and_claimable_next_run(store):
    """The failure n8n's Remove Duplicates has: a key seen on read is lost if the push fails."""
    c = store.claim(SCOPE, "ben@birch.example")
    store.release(SCOPE, "ben@birch.example", c.token)
    retry = store.claim(SCOPE, "ben@birch.example")
    assert retry.status == "reserved" and retry.token != c.token


def test_scopes_keep_the_same_key_apart(store):
    assert store.claim(SCOPE, "ana@acme.example").go
    assert store.claim("smartlead:campaign-77", "ana@acme.example").go
    assert store.counts(scope=SCOPE) == {"reserved": 1, "committed": 0}


def test_another_executions_token_cannot_commit_or_release(store):
    c = store.claim(SCOPE, "ana@acme.example")
    with pytest.raises(TokenMismatch):
        store.commit(SCOPE, "ana@acme.example", "tok-other")
    with pytest.raises(TokenMismatch):
        store.release(SCOPE, "ana@acme.example", "tok-other")
    store.commit(SCOPE, "ana@acme.example", c.token)
    with pytest.raises(AlreadyCommitted):
        store.release(SCOPE, "ana@acme.example", c.token)
    with pytest.raises(NotReserved):
        store.commit(SCOPE, "nobody@none.example", "tok-1")


def test_old_reservation_is_stale_and_never_handed_to_a_new_caller(store, clock):
    store.claim(SCOPE, "cara@cobalt.example")  # holder ended "unknown" and never came back
    clock.t += 900
    stale = store.claim(SCOPE, "cara@cobalt.example")
    assert stale.status == "stale" and not stale.go and stale.token is None
    assert store.claim(SCOPE, "cara@cobalt.example", stale_after=3600).status == "in_flight"
    (row,) = store.pending(older_than=600)
    assert row.key == "cara@cobalt.example" and row.stale and row.age_seconds == 900


def test_resolve_records_the_read_back(store, clock):
    store.claim(SCOPE, "cara@cobalt.example")
    store.claim(SCOPE, "dev@dune.example")
    with pytest.raises(StillInFlight):
        store.resolve(SCOPE, "cara@cobalt.example", landed=True)
    clock.t += 1000
    assert store.resolve(SCOPE, "cara@cobalt.example", landed=True) == "committed"
    assert store.claim(SCOPE, "cara@cobalt.example").status == "committed"
    assert store.resolve(SCOPE, "dev@dune.example", landed=False) is None
    assert store.claim(SCOPE, "dev@dune.example").go
    # force: an operator who knows the holder is gone
    store.claim(SCOPE, "eli@elm.example")
    assert store.resolve(SCOPE, "eli@elm.example", landed=False, force=True) is None


def test_pending_filters_by_scope_and_age_oldest_first(store, clock):
    store.claim(SCOPE, "b@birch.example")
    clock.t += 10
    store.claim("hubspot:contacts", "a@acme.example")
    clock.t += 10
    store.claim(SCOPE, "c@cobalt.example")
    assert [r.key for r in store.pending()] == [
        "b@birch.example",
        "a@acme.example",
        "c@cobalt.example",
    ]
    assert [r.key for r in store.pending(scope=SCOPE, older_than=15)] == ["b@birch.example"]


def test_blank_scope_or_key_is_refused(store):
    with pytest.raises(ValueError):
        store.claim(" ", "ana@acme.example")
    with pytest.raises(ValueError):
        store.claim(SCOPE, "")
    with pytest.raises(ValueError):
        KeyStore(stale_after=0)


def test_concurrent_claims_reserve_exactly_once(tmp_path):
    path = tmp_path / "keys.sqlite"
    stores = [KeyStore(path) for _ in range(4)]
    results = []
    barrier = threading.Barrier(8)

    def worker(s):
        barrier.wait()
        results.append(s.claim(SCOPE, "ana@acme.example").status)

    threads = [threading.Thread(target=worker, args=(stores[i % 4],)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    for s in stores:
        s.close()
    assert results.count("reserved") == 1 and results.count("in_flight") == 7


def test_state_survives_a_restart(tmp_path, clock):
    path = tmp_path / "keys.sqlite"
    with KeyStore(path, clock=clock) as s:
        c = s.claim(SCOPE, "ana@acme.example")
        s.commit(SCOPE, "ana@acme.example", c.token)
        s.claim(SCOPE, "ben@birch.example")
    with KeyStore(path, clock=clock) as s:
        assert s.claim(SCOPE, "ana@acme.example").status == "committed"
        assert [r.key for r in s.pending()] == ["ben@birch.example"]


# --- HTTP ------------------------------------------------------------------------------

TOKEN = "demo-reliability-token-0123456789"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture()
def api(store):
    return TestClient(create_app(api_token=TOKEN, store=store))


def test_api_claim_commit_flow(api, clock):
    body = {"scope": SCOPE, "key": "ana@acme.example"}
    claim = api.post("/v1/keys/claim", json=body, headers=AUTH).json()
    assert claim["status"] == "reserved" and claim["go"] is True and claim["token"] == "tok-1"
    busy = api.post("/v1/keys/claim", json=body, headers=AUTH).json()
    assert busy["status"] == "in_flight" and busy["go"] is False and busy["token"] is None
    r = api.post("/v1/keys/commit", json={**body, "token": "tok-1"}, headers=AUTH)
    assert r.status_code == 200 and r.json() == {"state": "committed"}
    done = api.post("/v1/keys/claim", json=body, headers=AUTH).json()
    assert done["status"] == "committed"


def test_api_errors_are_409_404_and_422(api, clock):
    body = {"scope": SCOPE, "key": "ana@acme.example"}
    api.post("/v1/keys/claim", json=body, headers=AUTH)
    wrong = api.post("/v1/keys/commit", json={**body, "token": "tok-x"}, headers=AUTH)
    assert wrong.status_code == 409
    missing = api.post(
        "/v1/keys/release",
        json={"scope": SCOPE, "key": "zed@zinc.example", "token": "t"},
        headers=AUTH,
    )
    assert missing.status_code == 404
    early = api.post("/v1/keys/resolve", json={**body, "landed": True}, headers=AUTH)
    assert early.status_code == 409
    extra = api.post("/v1/keys/claim", json={**body, "ttl": 5}, headers=AUTH)
    assert extra.status_code == 422
    blank = api.post("/v1/keys/claim", json={"scope": " ", "key": "k"}, headers=AUTH)
    assert blank.status_code == 422
    api.post("/v1/keys/commit", json={**body, "token": "tok-1"}, headers=AUTH)
    committed = api.post("/v1/keys/release", json={**body, "token": "tok-1"}, headers=AUTH)
    assert committed.status_code == 409


def test_api_pending_and_resolve_for_a_watchdog(api, clock):
    body = {"scope": SCOPE, "key": "cara@cobalt.example"}
    api.post("/v1/keys/claim", json=body, headers=AUTH)
    clock.t += 1200
    pending = api.get("/v1/keys/pending", params={"older_than": 900}, headers=AUTH).json()
    assert pending["count"] == 1 and pending["stale"] == 1
    assert pending["reservations"][0]["key"] == "cara@cobalt.example"
    r = api.post("/v1/keys/resolve", json={**body, "landed": False}, headers=AUTH)
    assert r.json() == {"state": "released"}
    assert api.get("/v1/keys/pending", headers=AUTH).json()["count"] == 0


def test_api_key_routes_need_the_token(api):
    r = api.post("/v1/keys/claim", json={"scope": SCOPE, "key": "a@acme.example"})
    assert r.status_code == 401
    assert api.get("/v1/keys/pending").status_code == 401
