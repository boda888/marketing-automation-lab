import json
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from replyrouting.api import MAX_BODY_BYTES, ReplyService, app_from_env, create_app
from replyrouting.intake import IntakeConfig, MemorySeenEvents, SqliteSeenEvents
from replyrouting.llm import FakeLLMClient, LLMResponse

NOW = datetime(2026, 11, 16, 17, 0, tzinfo=UTC)
SL_TOKEN = "t0k3n-smartlead-0123456789abcdef"
HR_TOKEN = "hr-path-token-0123456789abcdef"
SECRET = "lemlist-secret-0123456789abcdef"
API_TOKEN = "api-token-0123456789"
AUTH = {"Authorization": f"Bearer {API_TOKEN}"}
EMAIL = "mara.olsen@northwind-freight.example"
CONFIG = IntakeConfig(
    lemlist_secret=SECRET, path_tokens={"smartlead": SL_TOKEN, "heyreach": HR_TOKEN}
)


def smartlead(text="Please remove me from your list.", **extra):
    body = {
        "event_type": "EMAIL_REPLY",
        "campaign_id": 123456,
        "sl_email_lead_id": 987654,
        "to_email": EMAIL,
        "subject": "Re: lanes into Rotterdam",
        "event_timestamp": "2026-11-16T16:30:00.000Z",
    }
    if text is not None:
        body["reply_message"] = {"text": text}
    body.update(extra)
    return body


def lemlist(**extra):
    body = {
        "_id": "act_api_example",
        "type": "emailsReplied",
        "createdAt": "2026-11-16T16:45:00.000Z",
        "campaignId": "cam_example",
        "leadEmail": EMAIL,
        "subject": "Re: lanes",
        "text": "I'm out of the office until 24 November.",
        "secret": SECRET,
    }
    body.update(extra)
    return body


def make(service=None, **kw):
    service = service or ReplyService(CONFIG, MemorySeenEvents(), clock=lambda: NOW, **kw)
    return service, TestClient(create_app(service, api_token=API_TOKEN))


def kinds(body):
    return [(a["kind"], a["channel"]) for a in body["actions"]]


def test_smartlead_unsubscribe_reply_is_routed_with_actions():
    _, client = make()
    r = client.post(f"/v1/intake/smartlead/{SL_TOKEN}", json=smartlead())
    assert r.status_code == 200
    body = r.json()
    assert body["outcome"] == "accepted"
    assert body["reply_class"] == "unsubscribe"
    assert ("suppress", "email") in kinds(body)
    assert body["event"]["event_key"].startswith("smartlead:123456:987654:reply:")
    assert "reply_message" not in json.dumps(body)  # payload / body text never echoed back


def test_wrong_or_missing_token_is_401_so_the_vendor_retries_later():
    _, client = make()
    r = client.post("/v1/intake/smartlead/not-the-token", json=smartlead())
    assert r.status_code == 401
    assert r.json()["outcome"] == "rejected"
    assert client.post("/v1/intake/smartlead", json=smartlead()).status_code == 401
    # A rejected delivery records no key: the retry after the fix is processed.
    ok = client.post(f"/v1/intake/smartlead/{SL_TOKEN}", json=smartlead())
    assert ok.json()["outcome"] == "accepted"


def test_unconfigured_vendor_fails_closed():
    _, client = make()
    r = client.post("/v1/intake/instantly/anything", json={"event_type": "reply_received"})
    assert r.status_code == 401
    assert "not configured" in r.json()["reason"]


def test_lemlist_secret_in_body_and_ooo_pause_date():
    _, client = make()
    body = client.post("/v1/intake/lemlist", json=lemlist()).json()
    assert body["outcome"] == "accepted"
    assert body["reply_class"] == "out_of_office"
    assert body["return_date"] == "2026-11-24"
    assert {a["until"] for a in body["actions"] if a["kind"] == "pause_sequences"} == {"2026-11-25"}
    bad = client.post("/v1/intake/lemlist", json=lemlist(secret="wrong"))
    assert bad.status_code == 401


def test_unknown_vendor_404_and_non_json_400_and_oversized_413():
    _, client = make()
    assert client.post("/v1/intake/mailchimp/x", json={}).status_code == 404
    assert client.post("/v1/linkedin/smartlead/x", json={}).status_code == 404
    r = client.post(f"/v1/intake/smartlead/{SL_TOKEN}", content=b"not json")
    assert r.status_code == 400
    big = b'{"x": "' + b"a" * MAX_BODY_BYTES + b'"}'
    assert client.post(f"/v1/intake/smartlead/{SL_TOKEN}", content=big).status_code == 413


def test_replay_is_200_duplicate_and_other_outcomes_are_200():
    _, client = make()
    url = f"/v1/intake/smartlead/{SL_TOKEN}"
    assert client.post(url, json=smartlead()).json()["outcome"] == "accepted"
    again = client.post(url, json=smartlead())
    assert again.status_code == 200 and again.json()["outcome"] == "duplicate"
    alert = client.post(url, json=smartlead(event_type="EMAIL_REPLY_V2"))
    assert alert.status_code == 200 and alert.json()["outcome"] == "alert"
    stale = client.post(url, json=smartlead(event_timestamp="2026-11-14T10:00:00Z"))
    assert stale.json()["outcome"] == "stale"
    invalid = client.post(url, json=smartlead(sl_email_lead_id=None, event_timestamp=None))
    assert invalid.status_code == 200 and invalid.json()["outcome"] == "invalid"


def test_needs_body_waits_then_is_filled_once():
    _, client = make()
    url = f"/v1/intake/smartlead/{SL_TOKEN}"
    r = client.post(url, json=smartlead(text=None)).json()
    assert r["outcome"] == "needs_body"
    key = r["event"]["event_key"]
    pending = client.get("/v1/replies/pending", headers=AUTH).json()["pending"]
    assert [p["event_key"] for p in pending] == [key]

    assert client.post("/v1/replies", json={"event_key": key, "body_text": "x"}).status_code == 401
    filled = client.post(
        "/v1/replies", json={"event_key": key, "body_text": "Please remove me."}, headers=AUTH
    )
    assert filled.status_code == 200
    assert filled.json()["reply_class"] == "unsubscribe"
    second = client.post(
        "/v1/replies", json={"event_key": key, "body_text": "Please remove me."}, headers=AUTH
    )
    assert second.status_code == 404
    assert client.get("/v1/replies/pending", headers=AUTH).json()["pending"] == []


def test_pending_is_bounded_oldest_evicted():
    service = ReplyService(CONFIG, MemorySeenEvents(), clock=lambda: NOW, max_pending=2)
    _, client = make(service)
    url = f"/v1/intake/smartlead/{SL_TOKEN}"
    keys = [
        client.post(url, json=smartlead(text=None, sl_email_lead_id=i)).json()["event"]["event_key"]
        for i in (1, 2, 3)
    ]
    assert [p["event_key"] for p in service.pending()] == keys[1:]


def test_open_reply_without_llm_plans_classify_with_llm():
    _, client = make()
    body = client.post(
        f"/v1/intake/smartlead/{SL_TOKEN}", json=smartlead(text="What does this cost per lane?")
    ).json()
    assert body["reply_class"] == "unclassified"
    assert ("classify_with_llm", None) in kinds(body)
    assert body["llm"] is None


def test_open_reply_with_llm_client_is_decided_and_usage_reported():
    text = "What does this cost per lane?"
    answer = {
        "intent": "question",
        "confidence": 0.92,
        "evidence_quote": "What does this cost per lane?",
        "suggested_next_action": "answer_question",
        "reason": "asks for pricing",
    }
    llm = FakeLLMClient([LLMResponse(json.dumps(answer), "end_turn", 300, 50)])
    _, client = make(llm=llm)
    body = client.post(f"/v1/intake/smartlead/{SL_TOKEN}", json=smartlead(text=text)).json()
    assert body["reply_class"] == "question"
    assert {"kind": "notify_human", "priority": "high"}.items() <= next(
        a for a in body["actions"] if a["kind"] == "notify_human"
    ).items()
    assert body["llm"] == {"calls": 1, "input_tokens": 300, "output_tokens": 50}
    assert text in llm.calls[0]["user"]


class Boom:
    def complete(self, *a, **kw):
        raise RuntimeError("transport bug")  # not an LLMError: a real crash


def test_crash_after_key_recorded_releases_key_and_returns_503():
    seen = MemorySeenEvents()
    service = ReplyService(CONFIG, seen, clock=lambda: NOW, llm=Boom())
    _, client = make(service)
    url = f"/v1/intake/smartlead/{SL_TOKEN}"
    body = smartlead(text="What does this cost per lane?")
    r = client.post(url, json=body)
    assert r.status_code == 503
    service.llm = None  # fixed; the vendor's retry must be processed, not a duplicate
    assert client.post(url, json=body).json()["outcome"] == "accepted"


def test_failed_fill_keeps_the_event_pending():
    service = ReplyService(CONFIG, MemorySeenEvents(), clock=lambda: NOW, llm=Boom())
    _, client = make(service)
    key = client.post(f"/v1/intake/smartlead/{SL_TOKEN}", json=smartlead(text=None)).json()[
        "event"
    ]["event_key"]
    r = client.post("/v1/replies", json={"event_key": key, "body_text": "Pricing?"}, headers=AUTH)
    assert r.status_code == 503
    assert [p["event_key"] for p in service.pending()] == [key]


def test_linkedin_envelope_stops_email_too():
    _, client = make()
    env = {
        "event_type": "MESSAGE_REPLY_RECEIVED",
        "profile_url": "https://www.linkedin.com/in/mara-olsen-example",
        "message_text": "Please stop messaging me.",
        "message_at": "2026-11-16T16:40:00Z",
    }
    r = client.post(f"/v1/linkedin/heyreach/{HR_TOKEN}", json=env)
    body = r.json()
    assert body["reply_class"] == "unsubscribe"
    assert ("suppress", "linkedin") in kinds(body)
    assert any("no email key" in w for w in body["warnings"])
    every = dict(env, event_type="EVERY_MESSAGE_REPLY_RECEIVED")
    assert client.post(f"/v1/linkedin/heyreach/{HR_TOKEN}", json=every).json()["outcome"] == (
        "duplicate"
    )
    assert client.post("/v1/linkedin/heyreach/wrong", json=env).status_code == 401


def test_app_from_env_refuses_without_api_token_and_fails_closed(tmp_path):
    with pytest.raises(RuntimeError):
        app_from_env({})
    db = str(tmp_path / "seen.db")
    app = app_from_env(
        {
            "REPLYROUTING_API_TOKEN": API_TOKEN,
            "REPLYROUTING_TOKEN_SMARTLEAD": SL_TOKEN,
            "REPLYROUTING_SEEN_DB": db,
        }
    )
    client = TestClient(app)
    assert client.get("/healthz").json()["ok"] is True
    body = smartlead(event_timestamp=datetime.now(UTC).isoformat())
    assert client.post(f"/v1/intake/smartlead/{SL_TOKEN}", json=body).json()["outcome"] == (
        "accepted"
    )
    assert client.post("/v1/intake/lemlist", json=lemlist()).status_code == 401  # no secret set
    # The key went to the shared SQLite file: a second worker sees the replay as a duplicate.
    assert (
        SqliteSeenEvents(db).add(
            client.post(f"/v1/intake/smartlead/{SL_TOKEN}", json=body).json()["event"]["event_key"]
        )
        is False
    )
