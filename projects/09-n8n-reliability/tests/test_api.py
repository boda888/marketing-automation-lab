"""POST /v1/decide: the retry rule table over HTTP, one item and one attempt at a time."""

import pytest
from fastapi.testclient import TestClient
from reliability.api import DecideIn, create_app, decide_request, reached_vendor_from_code

NOW = 1_800_000_000.0


def client(**kw):
    return TestClient(create_app(now=lambda: NOW, rand=lambda: 0.5, **kw))


def post(c, body, **kw):
    return c.post("/v1/decide", json=body, **kw)


def test_2xx_is_done_ok():
    r = post(client(), {"attempt": 1, "method": "POST", "statusCode": 201})
    assert r.status_code == 200
    body = r.json()
    assert body["action"] == "done" and body["outcome"] == "ok"
    assert body["dlq_row"] is None and body["wait_ms"] == 0 and body["next_attempt"] is None


def test_429_honours_retry_after_and_counts_attempts():
    body = post(
        client(),
        {"attempt": 1, "method": "POST", "status": 429, "headers": {"Retry-After": "7"}},
    ).json()
    assert body["action"] == "retry"
    assert body["wait_ms"] == 7000 and body["next_attempt"] == 2
    assert body["reason"] == "http_429"


def test_backoff_without_retry_after_grows_with_attempt():
    c = client()
    waits = [
        post(c, {"attempt": a, "method": "GET", "status": 503}).json()["wait_ms"] for a in (1, 2, 3)
    ]
    assert waits == [1000, 2000, 4000]


def test_plain_create_5xx_is_unknown_never_retried():
    body = post(
        client(), {"attempt": 1, "method": "POST", "status": 502, "key": "ana@acme.example"}
    ).json()
    assert body["action"] == "stop" and body["outcome"] == "unknown"
    assert body["safe_to_resend"] is False
    assert body["dlq_row"] == {
        "key": "ana@acme.example",
        "outcome": "unknown",
        "reason": "http_502_after_send",
        "attempts": 1,
        "last_status": 502,
        "retry_at": None,
        "safe_to_resend": False,
    }


def test_explicit_idempotent_upsert_retries_5xx():
    body = post(
        client(), {"attempt": 1, "method": "POST", "status": 502, "idempotent": True}
    ).json()
    assert body["action"] == "retry" and body["idempotent"] is True


def test_idempotency_key_in_request_headers_makes_409_retry_and_422_fail():
    c = client()
    keyed = {"request_headers": {"Idempotency-Key": "lead-42-push-1"}}
    r409 = post(c, {"attempt": 1, "method": "POST", "status": 409, **keyed}).json()
    assert r409["action"] == "retry" and r409["reason"] == "idempotency_key_in_flight"
    r422 = post(c, {"attempt": 1, "method": "POST", "status": 422, **keyed}).json()
    assert r422["outcome"] == "failed" and r422["reason"] == "idempotency_key_reused"
    # without the key a 409 is an ordinary rejection
    plain = post(c, {"attempt": 1, "method": "POST", "status": 409}).json()
    assert plain["outcome"] == "failed"


def test_blank_idempotency_key_does_not_count():
    body = post(
        client(),
        {
            "attempt": 1,
            "method": "POST",
            "status": 503,
            "request_headers": {"idempotency-key": "  "},
        },
    ).json()
    assert body["outcome"] == "unknown"


@pytest.mark.parametrize("code", ["ENOTFOUND", "ECONNREFUSED", "eai_again", "CERT_HAS_EXPIRED"])
def test_error_codes_that_never_left_are_retried_even_for_creates(code):
    body = post(client(), {"attempt": 1, "method": "POST", "error": {"code": code}}).json()
    assert body["action"] == "retry" and body["reason"] == "not_sent"


@pytest.mark.parametrize("error", [{"code": "ETIMEDOUT"}, {"code": "ECONNRESET"}, {}])
def test_timeout_reset_or_unknown_error_after_send_is_unknown_for_creates(error):
    body = post(client(), {"attempt": 1, "method": "POST", "error": error}).json()
    assert body["outcome"] == "unknown" and body["reason"] == "transport_error_after_send"


def test_explicit_reached_vendor_overrides_code():
    body = post(
        client(),
        {"attempt": 1, "method": "POST", "error": {"code": "ENOTFOUND", "reached_vendor": True}},
    ).json()
    assert body["outcome"] == "unknown"


def test_long_retry_after_is_deferred_with_iso_time():
    body = post(
        client(),
        {"attempt": 1, "method": "GET", "status": 429, "headers": {"retry-after": ["3600"]}},
    ).json()
    assert body["action"] == "stop" and body["outcome"] == "deferred"
    assert body["retry_at"] == NOW + 3600
    assert body["retry_at_iso"].startswith("2027-01-15T")
    assert body["safe_to_resend"] is True and body["dlq_row"]["retry_at"] == NOW + 3600


def test_policy_from_body_limits_attempts():
    body = post(
        client(),
        {"attempt": 2, "method": "GET", "status": 503, "policy": {"max_attempts": 2}},
    ).json()
    assert body["outcome"] == "exhausted" and body["safe_to_resend"] is True


@pytest.mark.parametrize(
    "body",
    [
        {"attempt": 1, "method": "GET"},  # neither status nor error
        {"attempt": 1, "method": "GET", "status": 200, "error": {"code": "ETIMEDOUT"}},
        {"attempt": 0, "method": "GET", "status": 200},
        {"attempt": 1, "method": "GET", "status": 200, "policy": {"max_attempts": 0}},
        {"attempt": 1, "method": "GET", "status": 200, "policy": {"retries": 3}},
        {"attempt": 1, "method": "GET", "status": 99},
    ],
)
def test_undecidable_bodies_are_422(body):
    assert post(client(), body).status_code == 422


def test_bearer_token_when_configured():
    c = client(api_token="demo-token-0123456789")
    body = {"attempt": 1, "method": "GET", "status": 200}
    assert post(c, body).status_code == 401
    assert post(c, body, headers={"Authorization": "Bearer wrong"}).status_code == 401
    ok = post(c, body, headers={"Authorization": "Bearer demo-token-0123456789"})
    assert ok.status_code == 200
    assert c.get("/healthz").json() == {"status": "ok"}


def test_reached_vendor_from_code_defaults_to_reached():
    assert reached_vendor_from_code(None) is True
    assert reached_vendor_from_code("") is True
    assert reached_vendor_from_code("ESOMETHINGNEW") is True
    assert reached_vendor_from_code(" econnrefused ") is False


def test_decide_request_matches_route_without_http():
    body = DecideIn.model_validate({"attempt": 3, "method": "PUT", "status": 500})
    out = decide_request(body, now=lambda: NOW, rand=lambda: 0.5)
    assert out["action"] == "retry" and out["wait_ms"] == 4000 and out["next_attempt"] == 4


def test_n8n_loop_never_resends_a_landed_item():
    """Replay an n8n loop: item A lands first try, item B gets 429 then 201; only B repeats."""
    c = client()
    script = {"A": [201], "B": [429, 201]}
    calls: list[str] = []
    for key, answers in script.items():
        attempt = 1
        while True:
            calls.append(key)
            status = answers[attempt - 1]
            d = post(c, {"attempt": attempt, "method": "POST", "status": status, "key": key}).json()
            if d["action"] != "retry":
                assert d["outcome"] == "ok"
                break
            attempt = d["next_attempt"]
    assert calls == ["A", "B", "B"]
