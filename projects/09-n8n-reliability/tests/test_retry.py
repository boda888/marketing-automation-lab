from __future__ import annotations

import email.utils

import pytest
from reliability import (
    HttpResult,
    RetryPolicy,
    TransportError,
    decide,
    is_idempotent,
    retry_after_seconds,
    run_items,
)

NOW = 1_800_000_000.0


def clock() -> float:
    return NOW


def http_date(epoch: float) -> str:
    return email.utils.formatdate(epoch, usegmt=True)


class FakeVendor:
    """Scripted answers per item; records every call so re-sends are visible."""

    def __init__(self, script: dict[str, list[object]]) -> None:
        self.script = {k: list(v) for k, v in script.items()}
        self.calls: list[str] = []

    def __call__(self, item: str) -> HttpResult:
        self.calls.append(item)
        answers = self.script.get(item)
        answer = answers.pop(0) if answers else HttpResult(200)
        if isinstance(answer, Exception):
            raise answer
        assert isinstance(answer, HttpResult)
        return answer


class Sleeps(list):
    def __call__(self, seconds: float) -> None:
        self.append(seconds)


# --- Retry-After ------------------------------------------------------------------------


def test_retry_after_delta_seconds_and_case_insensitive_header() -> None:
    assert retry_after_seconds({"retry-after": " 30 "}) == 30.0
    assert retry_after_seconds({"Retry-After": "0"}) == 0.0


def test_retry_after_http_date_future_past_and_junk() -> None:
    assert retry_after_seconds({"Retry-After": http_date(NOW + 90)}, now=clock) == 90.0
    assert retry_after_seconds({"Retry-After": http_date(NOW - 90)}, now=clock) == 0.0
    assert retry_after_seconds({"Retry-After": "soon"}) is None
    assert retry_after_seconds({"Retry-After": "-5"}) is None
    assert retry_after_seconds({}) is None


# --- idempotency ------------------------------------------------------------------------


@pytest.mark.parametrize("method", ["GET", "head", "PUT", "DELETE", "OPTIONS"])
def test_rfc9110_idempotent_methods(method: str) -> None:
    assert is_idempotent(method)


def test_post_is_idempotent_only_with_a_non_empty_idempotency_key() -> None:
    assert not is_idempotent("POST")
    assert not is_idempotent("PATCH", {"Content-Type": "application/json"})
    assert not is_idempotent("POST", {"Idempotency-Key": "  "})
    assert is_idempotent("POST", {"idempotency-key": "lead-42:push-1"})


# --- backoff ----------------------------------------------------------------------------


def test_backoff_is_exponential_and_capped() -> None:
    p = RetryPolicy(base_seconds=1, factor=2, max_backoff_seconds=5)
    assert [p.backoff(n) for n in range(1, 6)] == [1, 2, 4, 5, 5]


def test_jitter_only_shrinks_the_backoff_within_its_fraction() -> None:
    p = RetryPolicy(base_seconds=8, jitter=0.5)
    assert p.backoff(1, rand=lambda: 0.0) == 4.0
    assert p.backoff(1, rand=lambda: 1.0) == 8.0


@pytest.mark.parametrize(
    "kwargs",
    [{"max_attempts": 0}, {"factor": 0.5}, {"jitter": 1.5}, {"max_wait_seconds": -1}],
)
def test_policy_rejects_nonsense(kwargs: dict[str, float]) -> None:
    with pytest.raises(ValueError):
        RetryPolicy(**kwargs)


# --- decide() rule table -----------------------------------------------------------------


def test_2xx_is_done() -> None:
    d = decide(HttpResult(201), attempt=1, idempotent=False)
    assert (d.action, d.outcome) == ("done", "ok")


def test_429_retries_even_a_create_and_honours_retry_after() -> None:
    d = decide(HttpResult(429, {"Retry-After": "12"}), attempt=1, idempotent=False)
    assert (d.action, d.wait_seconds, d.wait_ms) == ("retry", 12.0, 12000)


def test_429_without_hint_uses_backoff() -> None:
    p = RetryPolicy(base_seconds=2)
    d = decide(HttpResult(429), attempt=2, idempotent=True, policy=p)
    assert (d.action, d.wait_seconds) == ("retry", 4.0)


def test_retry_after_longer_than_max_wait_defers_with_retry_at() -> None:
    p = RetryPolicy(max_wait_seconds=60)
    d = decide(
        HttpResult(429, {"Retry-After": "3600"}), attempt=1, idempotent=True, policy=p, now=clock
    )
    assert (d.action, d.outcome, d.retry_at) == ("stop", "deferred", NOW + 3600)


@pytest.mark.parametrize("status", [500, 502, 503, 504, 408])
def test_5xx_on_a_create_is_unknown_never_retried(status: int) -> None:
    d = decide(HttpResult(status), attempt=1, idempotent=False)
    assert (d.action, d.outcome) == ("stop", "unknown")


@pytest.mark.parametrize("status", [500, 502, 503, 504, 408, 425])
def test_5xx_on_an_idempotent_call_retries(status: int) -> None:
    assert decide(HttpResult(status), attempt=1, idempotent=True).action == "retry"


def test_timeout_after_send_on_create_is_unknown() -> None:
    d = decide(TransportError("read timeout"), attempt=1, idempotent=False)
    assert (d.outcome, d.reason) == ("unknown", "transport_error_after_send")


def test_not_sent_retries_even_a_create() -> None:
    d = decide(TransportError("dns", reached_vendor=False), attempt=1, idempotent=False)
    assert (d.action, d.reason) == ("retry", "not_sent")


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 422])
def test_other_4xx_fail_permanently(status: int) -> None:
    d = decide(HttpResult(status), attempt=1, idempotent=True)
    assert (d.action, d.outcome) == ("stop", "failed")


def test_keyed_409_means_first_request_still_in_flight() -> None:
    d = decide(HttpResult(409), attempt=1, idempotent=False, keyed=True)
    assert (d.action, d.reason) == ("retry", "idempotency_key_in_flight")


def test_keyed_422_means_key_reused_with_other_payload() -> None:
    d = decide(HttpResult(422), attempt=1, idempotent=False, keyed=True)
    assert (d.outcome, d.reason) == ("failed", "idempotency_key_reused")


def test_keyed_post_5xx_is_retried_because_the_key_makes_it_replay_safe() -> None:
    assert decide(HttpResult(503), attempt=1, idempotent=False, keyed=True).action == "retry"


def test_last_attempt_is_exhausted() -> None:
    p = RetryPolicy(max_attempts=3)
    d = decide(HttpResult(503), attempt=3, idempotent=True, policy=p)
    assert (d.action, d.outcome) == ("stop", "exhausted")


def test_attempt_is_one_based() -> None:
    with pytest.raises(ValueError):
        decide(HttpResult(200), attempt=0, idempotent=True)


# --- run_items() --------------------------------------------------------------------------


def test_a_429_mid_batch_resends_only_that_item() -> None:
    """The n8n Retry On Fail failure mode: lead 3 hits 429, leads 1-2 must not be re-sent."""
    leads = ["a@acme.example", "b@acme.example", "c@acme.example", "d@acme.example"]
    vendor = FakeVendor({"c@acme.example": [HttpResult(429, {"Retry-After": "7"})]})
    sleeps = Sleeps()
    report = run_items(leads, vendor, idempotent=False, sleep=sleeps)
    assert vendor.calls == [*leads[:3], "c@acme.example", "d@acme.example"]
    assert sleeps == [7.0]
    assert report.counts()["ok"] == 4
    assert report.calls == 5
    assert report.dlq_rows() == []


def test_every_item_gets_one_result_in_input_order_with_dlq_rows() -> None:
    vendor = FakeVendor(
        {
            "ok": [HttpResult(200)],
            "bad": [HttpResult(400)],
            "flaky": [HttpResult(503)] * 5,
            "slow": [HttpResult(429, {"Retry-After": "7200"})],
            "lost": [TransportError("reset")],
        }
    )
    report = run_items(
        ["ok", "bad", "flaky", "slow", "lost"],
        vendor,
        idempotent=False,
        policy=RetryPolicy(max_attempts=3),
        sleep=Sleeps(),
        now=clock,
    )
    assert [r.outcome for r in report.results] == ["ok", "failed", "unknown", "deferred", "unknown"]
    rows = {r["key"]: r for r in report.dlq_rows()}
    assert set(rows) == {"bad", "flaky", "slow", "lost"}
    assert rows["slow"]["retry_at"] == NOW + 7200
    assert rows["slow"]["safe_to_resend"] is True
    assert rows["lost"]["safe_to_resend"] is False  # may have landed: read back first
    assert rows["lost"]["last_status"] is None
    assert vendor.calls.count("flaky") == 1  # create + 503 is never retried


def test_idempotent_upsert_rides_out_503s_then_exhausts() -> None:
    vendor = FakeVendor(
        {"x": [HttpResult(503), HttpResult(503), HttpResult(200)], "y": [HttpResult(502)] * 9}
    )
    sleeps = Sleeps()
    report = run_items(
        ["x", "y"],
        vendor,
        idempotent=True,
        policy=RetryPolicy(max_attempts=3, base_seconds=1),
        sleep=sleeps,
        key=lambda item: f"lead:{item}",
    )
    x, y = report.results
    assert (x.key, x.outcome, x.attempts, x.waited_seconds) == ("lead:x", "ok", 3, 3.0)
    assert (y.outcome, y.attempts, y.last_status, y.safe_to_resend) == ("exhausted", 3, 502, True)
    assert sleeps == [1.0, 2.0, 1.0, 2.0]


def test_not_sent_create_is_retried_and_lands() -> None:
    vendor = FakeVendor({"a": [TransportError("refused", reached_vendor=False), HttpResult(201)]})
    report = run_items(["a"], vendor, idempotent=False, sleep=Sleeps())
    assert (report.results[0].outcome, report.results[0].attempts) == ("ok", 2)


def test_programming_errors_propagate() -> None:
    def broken(item: str) -> HttpResult:
        raise KeyError(item)

    with pytest.raises(KeyError):
        run_items(["a"], broken, idempotent=True, sleep=Sleeps())
