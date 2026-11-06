"""execute_upserts: only failed-and-retryable rows are re-sent, under one rate budget."""

from collections import deque

import pytest
from crmsync import (
    CrmRow,
    HttpResponse,
    Outcome,
    RateBudget,
    TransportError,
    execute_upserts,
    plan_upserts,
)
from crmsync.hubspot import trace_id


class FakeHubSpot:
    """Scripted answers; a callable item builds the body from the request it gets."""

    def __init__(self, *script):
        self.script = deque(script)
        self.calls = []

    def send(self, call):
        self.calls.append(call)
        item = self.script.popleft() if self.script else ok
        if isinstance(item, BaseException):
            raise item
        return item(call) if callable(item) else item


def ok(call, new=True):
    results = [
        {"id": f"9{i}", "_new": new, "objectWriteTraceId": x["objectWriteTraceId"]}
        for i, x in enumerate(call.json["inputs"])
    ]
    return HttpResponse(200, {"status": "COMPLETE", "results": results, "errors": []})


def emails(call):
    return [x["id"] for x in call.json["inputs"]]


def rows(n):
    return [CrmRow(f"r{i}", f"p{i}@northwind.io", {"firstname": f"P{i}"}) for i in range(n)]


class Clock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def run(plan, transport, **kw):
    clock = Clock()
    budget = kw.pop("budget", None) or RateBudget(100, 10, clock=clock, sleep=clock.sleep)
    report = execute_upserts(plan, transport, token="pat-x", budget=budget, sleep=clock.sleep, **kw)
    return report, clock


def test_request_shape_and_all_rows_landed():
    hub = FakeHubSpot()
    report, _ = run(plan_upserts(rows(2)), hub)
    (call,) = hub.calls
    assert call.method == "POST"
    assert call.url == "https://api.hubapi.com/crm/v3/objects/contacts/batch/upsert"
    assert call.headers["Authorization"] == "Bearer pat-x"
    assert "pat-x" not in repr(call)
    assert [o.outcome for o in report.outcomes] == [Outcome.CREATED, Outcome.CREATED]
    assert report.summary()["calls"] == 1 and report.retried_rows == 0


def test_partial_207_resends_only_missing_row_never_rejected_one():
    plan = plan_upserts(rows(3))
    first = plan.batches[0].rows
    partial = HttpResponse(
        207,
        {
            "status": "COMPLETE",
            "results": [{"id": "90", "_new": False, "objectWriteTraceId": first[0].trace_id}],
            "errors": [
                {
                    "category": "VALIDATION_ERROR",
                    "message": "bad",
                    "context": {"ids": ["p1@northwind.io"]},
                }
            ],
        },
    )  # fmt: skip
    hub = FakeHubSpot(partial, ok)
    report, clock = run(plan, hub)
    assert emails(hub.calls[1]) == ["p2@northwind.io"]  # p1 rejected: not re-sent
    assert [o.outcome for o in report.outcomes] == [
        Outcome.UPDATED,
        Outcome.FAILED,
        Outcome.CREATED,
    ]
    assert report.outcomes[1].reason.startswith("VALIDATION_ERROR")
    assert report.retried_rows == 1 and clock.sleeps == [1.0]


def test_429_honours_retry_after_capped():
    hub = FakeHubSpot(
        HttpResponse(429, {}, {"Retry-After": "7"}),
        HttpResponse(429, {}, {"retry-after": "90"}),
        ok,
    )
    report, clock = run(plan_upserts(rows(1)), hub, max_wait_seconds=30)
    assert clock.sleeps == [7.0, 30.0]
    assert report.outcomes[0].outcome is Outcome.CREATED and report.calls == 3


@pytest.mark.parametrize("error", [TimeoutError("read timed out"), TransportError("reset")])
def test_timeout_or_reset_is_retried_because_upsert_is_idempotent(error):
    hub = FakeHubSpot(error, lambda call: ok(call, new=False))
    report, _ = run(plan_upserts(rows(2)), hub)
    assert len(hub.calls) == 2 and hub.calls[0].json == hub.calls[1].json
    assert [o.outcome for o in report.outcomes] == [Outcome.UPDATED, Outcome.UPDATED]


def test_gives_up_after_max_attempts_and_says_still_retryable():
    hub = FakeHubSpot(HttpResponse(503), HttpResponse(503), HttpResponse(503))
    report, clock = run(plan_upserts(rows(2)), hub, max_attempts=3)
    assert report.calls == 3 and clock.sleeps == [1.0, 2.0]
    assert all(o.outcome is Outcome.FAILED and o.retryable for o in report.outcomes)
    assert report.summary()["still_retryable"] == 2 and report.summary()["failed"] == 2


def test_400_is_never_retried():
    hub = FakeHubSpot(HttpResponse(400, {"category": "VALIDATION_ERROR", "message": "nope"}))
    report, _ = run(plan_upserts(rows(2)), hub)
    assert report.calls == 1 and not any(o.retryable for o in report.outcomes)


def test_outcomes_in_input_order_with_skipped_rows():
    data = [
        CrmRow("a", "ana@northwind.io"),
        CrmRow("b", "bea@fabrikam.example"),  # fixture
        CrmRow("c", "ANA@northwind.io"),  # duplicate key
        CrmRow("d", "dan@northwind.io"),
    ]
    report, _ = run(plan_upserts(data, batch_size=1), FakeHubSpot())
    assert [o.row_id for o in report.outcomes] == ["a", "b", "c", "d"]
    assert [o.outcome for o in report.outcomes] == [
        Outcome.CREATED, Outcome.SKIPPED, Outcome.SKIPPED, Outcome.CREATED,
    ]  # fmt: skip
    assert report.calls == 2


def test_duplicate_row_ids_refused():
    with pytest.raises(ValueError, match="not unique"):
        plan_upserts([CrmRow("r1", "a@northwind.io"), CrmRow("r1", "b@northwind.io")])


def test_shared_budget_throttles_two_writers_on_one_portal():
    clock = Clock()
    budget = RateBudget(2, 10, clock=clock, sleep=clock.sleep)
    for _ in range(2):  # two plans, same portal budget
        execute_upserts(
            plan_upserts(rows(2), batch_size=1), FakeHubSpot(), token="t", budget=budget,
            sleep=clock.sleep,
        )  # fmt: skip
    assert clock.sleeps == [10.0] and budget.waited == 10.0


def test_rate_budget_and_arguments_validated():
    with pytest.raises(ValueError):
        RateBudget(0, 10)
    with pytest.raises(ValueError, match="token"):
        execute_upserts(plan_upserts(rows(1)), FakeHubSpot(), token="", budget=RateBudget())
    with pytest.raises(ValueError, match="max_attempts"):
        execute_upserts(
            plan_upserts(rows(1)), FakeHubSpot(), token="t", budget=RateBudget(), max_attempts=0
        )


def test_trace_ids_stable_across_retries():
    hub = FakeHubSpot(HttpResponse(502), ok)
    run(plan_upserts(rows(1)), hub)
    ids = [c.json["inputs"][0]["objectWriteTraceId"] for c in hub.calls]
    assert ids == [trace_id("p0@northwind.io")] * 2
