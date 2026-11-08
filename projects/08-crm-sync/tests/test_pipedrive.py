"""Pipedrive: cache before search, one writer, a create is never blindly re-sent."""

import threading
from collections import deque

import pytest
from crmsync import (
    CrmRow,
    HttpResponse,
    MemoryPersonCache,
    Outcome,
    RateBudget,
    TransportError,
    execute_persons,
    plan_persons,
)

CF = "a" * 40  # a Pipedrive custom-field key is a 40-hex hash


def hits(*people):
    items = [{"result_score": 1.0, "item": {"id": pid, "emails": emails}} for pid, emails in people]
    return HttpResponse(200, {"success": True, "data": {"items": items}})


def person(pid):
    return HttpResponse(200, {"success": True, "data": {"id": pid}})


class FakePipedrive:
    def __init__(self, *script):
        self.script = deque(script)
        self.calls = []

    def send(self, call):
        self.calls.append(call)
        item = self.script.popleft()
        if isinstance(item, BaseException):
            raise item
        return item

    def methods(self):
        return [c.method for c in self.calls]


def run(rows, fake, cache=None, **kw):
    sleeps = []
    report = execute_persons(
        plan_persons(rows),
        fake,
        token="t",
        budget=RateBudget(100, 2.0, clock=lambda: 0.0, sleep=lambda s: None),
        cache=cache,
        sleep=sleeps.append,
        **kw,
    )
    return report, sleeps


ANA = CrmRow("r1", "Ana@Northwind.io", {"name": "Ana Ruiz", "job_title": "COO", CF: "Tier 1"})


# --- planning ----------------------------------------------------------------------------


def test_plan_splits_standard_and_custom_fields_and_drops_placeholders():
    plan = plan_persons([CrmRow("r1", "ana@n.io", {"name": "Ana", "org_id": 0, CF: "Unknown"})])
    w = plan.writes[0]
    assert w.create_body() == {
        "name": "Ana",
        "org_id": 0,
        "emails": [{"value": "ana@n.io", "primary": True, "label": "work"}],
    }
    assert "emails" not in w.update_body()


def test_plan_skips():
    plan = plan_persons(
        [
            CrmRow("r1", None),
            CrmRow("r2", "a@n.example"),
            CrmRow("r3", "b@n.io", {"Lead score": 3}),
            CrmRow("r4", "c@n.io"),
            CrmRow("r5", "C@n.io"),
        ]
    )
    assert {o.row_id: o.reason for o in plan.skipped} == {
        "r1": "missing_key:email",
        "r2": "fixture",
        "r3": "invalid_property_name:Lead score",
        "r5": "duplicate_key:first_row=r4",
    }


# --- search, update, create --------------------------------------------------------------


def test_miss_searches_exactly_then_creates_and_caches():
    cache = MemoryPersonCache()
    fake = FakePipedrive(hits(), person(41))
    report, _ = run([ANA], fake, cache)
    out = report.outcomes[0]
    assert (out.outcome, out.crm_id) == (Outcome.CREATED, "41")
    search = fake.calls[0]
    assert search.method == "GET"
    assert search.url == (
        "https://api.pipedrive.com/api/v2/persons/search"
        "?term=ana%40northwind.io&fields=email&exact_match=true"
    )
    assert search.headers["x-api-token"] == "t"
    assert fake.calls[1].json["custom_fields"] == {CF: "Tier 1"}
    assert cache.get("ana@northwind.io") == "41"


def test_cache_hit_updates_without_search():
    cache = MemoryPersonCache({"ana@northwind.io": "41"})
    fake = FakePipedrive(person(41))
    report, _ = run([ANA], fake, cache)
    assert fake.methods() == ["PATCH"]
    assert fake.calls[0].url.endswith("/persons/41")
    assert report.outcomes[0].outcome is Outcome.UPDATED
    assert report.summary()["cache_hits"] == 1 and report.summary()["search_calls"] == 0


def test_found_person_is_updated_not_created():
    fake = FakePipedrive(hits((7, ["ANA@northwind.io"])), person(7))
    report, _ = run([ANA], fake)
    assert fake.methods() == ["GET", "PATCH"]
    assert report.outcomes[0].crm_id == "7"


def test_search_hit_without_the_exact_email_is_ignored():
    fake = FakePipedrive(hits((7, ["ana@northwind.io.old"])), person(8))
    report, _ = run([ANA], fake)
    assert report.outcomes[0].outcome is Outcome.CREATED


def test_two_matching_persons_is_ambiguous_never_picked():
    fake = FakePipedrive(hits((7, ["ana@northwind.io"]), (9, ["ana@northwind.io"])))
    report, _ = run([ANA], fake)
    out = report.outcomes[0]
    assert (out.outcome, out.reason, out.retryable) == (
        Outcome.FAILED,
        "ambiguous_match:7,9",
        False,
    )
    assert fake.methods() == ["GET"]


def test_stale_cached_id_is_evicted_and_searched_again():
    cache = MemoryPersonCache({"ana@northwind.io": "41"})
    fake = FakePipedrive(HttpResponse(404, {"success": False}), hits((50, [ANA.email])), person(50))
    report, _ = run([ANA], fake, cache)
    assert fake.methods() == ["PATCH", "GET", "PATCH"]
    assert cache.get("ana@northwind.io") == "50"
    assert report.outcomes[0].crm_id == "50"


def test_create_needs_a_name():
    fake = FakePipedrive(hits())
    report, _ = run([CrmRow("r1", "bo@n.io", {"job_title": "CTO"})], fake)
    assert report.outcomes[0].reason == "missing_name_for_create"


def test_no_fields_on_existing_person_spends_no_write():
    fake = FakePipedrive(hits((7, ["bo@n.io"])))
    report, _ = run([CrmRow("r1", "bo@n.io")], fake)
    assert fake.methods() == ["GET"]
    assert (report.outcomes[0].outcome, report.outcomes[0].reason) == (Outcome.UPDATED, "no_fields")


# --- retries: only where a re-send cannot duplicate ---------------------------------------


@pytest.mark.parametrize(
    "failure",
    [TimeoutError(), TransportError("reset"), HttpResponse(502), HttpResponse(200, {"data": {}})],
)
def test_create_with_unknown_result_is_never_resent_nor_cached(failure):
    cache = MemoryPersonCache()
    fake = FakePipedrive(hits(), failure)
    report, _ = run([ANA], fake, cache)
    out = report.outcomes[0]
    assert (out.outcome, out.reason, out.retryable) == (Outcome.FAILED, "outcome_unknown", False)
    assert fake.methods() == ["GET", "POST"]
    assert cache.get("ana@northwind.io") is None  # next run searches before any create


def test_create_rejected_by_429_or_never_sent_is_retried():
    fake = FakePipedrive(
        hits(),
        HttpResponse(429, None, {"Retry-After": "3"}),
        TransportError("refused", reached_vendor=False),
        person(41),
    )
    report, sleeps = run([ANA], fake)
    assert report.outcomes[0].outcome is Outcome.CREATED
    assert fake.methods() == ["GET", "POST", "POST", "POST"]
    assert sleeps == [3.0, 2.0]


def test_search_and_update_are_retried_on_transient_errors():
    fake = FakePipedrive(TimeoutError(), hits((7, [ANA.email])), HttpResponse(503), person(7))
    report, _ = run([ANA], fake)
    assert report.outcomes[0].outcome is Outcome.UPDATED
    assert report.summary()["retried_rows"] == 2


def test_search_exhausted_stays_retryable():
    fake = FakePipedrive(HttpResponse(500), HttpResponse(500))
    report, _ = run([ANA], fake, max_attempts=2)
    out = report.outcomes[0]
    assert (out.outcome, out.reason, out.retryable) == (Outcome.FAILED, "http_500", True)


def test_403_contact_sync_field_is_not_retried():
    fake = FakePipedrive(hits(), HttpResponse(403, {"success": False, "error": "contact sync off"}))
    report, _ = run([ANA], fake)
    assert report.outcomes[0].reason == "http_403: contact sync off"
    assert fake.methods() == ["GET", "POST"]


def test_search_and_create_run_under_the_writer_lock():
    class SpyLock:
        def __init__(self):
            self.inner, self.held = threading.Lock(), False

        def __enter__(self):
            self.inner.acquire()
            self.held = True

        def __exit__(self, *exc):
            self.held = False
            self.inner.release()

    lock = SpyLock()
    held = []

    class Spy(FakePipedrive):
        def send(self, call):
            held.append((call.method, lock.held))
            return super().send(call)

    run([ANA], Spy(hits(), person(41)), lock=lock)
    assert held == [("GET", True), ("POST", True)]


def test_outcomes_in_input_order_with_skips():
    rows = [ANA, CrmRow("r2", None), CrmRow("r3", "bo@n.io", {"name": "Bo"})]
    fake = FakePipedrive(hits(), person(1), hits(), person(2))
    report, _ = run(rows, fake)
    assert [o.row_id for o in report.outcomes] == ["r1", "r2", "r3"]
    assert report.summary()["created"] == 2 and report.summary()["skipped"] == 1


def test_token_required():
    with pytest.raises(ValueError):
        execute_persons(plan_persons([]), FakePipedrive(), token="", budget=RateBudget())
