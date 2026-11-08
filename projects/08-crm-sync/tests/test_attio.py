"""Attio assert: no multiselect wipe, one outcome per row, retries only where idempotent."""

from collections import deque

import pytest
from crmsync import (
    CrmRow,
    HttpResponse,
    Outcome,
    RateBudget,
    TransportError,
    execute_asserts,
    parse_assert_response,
    plan_asserts,
    retry_after_seconds,
)


def record(record_id="rec-1"):
    return {
        "data": {
            "id": {"workspace_id": "ws", "object_id": "obj", "record_id": record_id},
            "created_at": "2026-11-08T17:00:00.000000000Z",
            "web_url": "https://app.attio.com/x",
            "values": {},
        }
    }


class FakeAttio:
    def __init__(self, *script):
        self.script = deque(script)
        self.calls = []

    def send(self, call):
        self.calls.append(call)
        item = self.script.popleft() if self.script else HttpResponse(200, record())
        if isinstance(item, BaseException):
            raise item
        return item


def one(rows, **kw):
    return plan_asserts(rows, **kw)


# --- planning ----------------------------------------------------------------------------


def test_assert_call_shape_on_email():
    plan = one([CrmRow("r1", " Ana@Northwind.IO ", {"job_title": "COO", "score": 0})])
    call = plan.calls[0]
    assert call.method == "PUT"
    assert call.path == "/v2/objects/people/records?matching_attribute=email_addresses"
    assert call.body() == {
        "data": {
            "values": {
                "job_title": ["COO"],
                "score": [0],  # 0 is a value, sent as a JSON number
                "email_addresses": [{"email_address": "ana@northwind.io"}],
            }
        }
    }


def test_blank_and_placeholder_values_are_not_sent():
    plan = one([CrmRow("r1", "ana@northwind.io", {"job_title": "Unknown", "city": " "})])
    assert plan.calls[0].values == {"email_addresses": [{"email_address": "ana@northwind.io"}]}


def test_declared_multiselect_is_refused_unless_owned():
    rows = [CrmRow("r1", "ana@northwind.io", {"tags": ["clay-import"]})]
    plan = one(rows, multiselect=["tags"])
    assert plan.calls == []
    assert plan.skipped[0].reason == "multiselect_overwrite:tags"

    owned = one(rows, multiselect=["tags"], overwrite=["tags"])
    assert owned.calls[0].values["tags"] == ["clay-import"]


def test_scalar_on_multiselect_attribute_also_counts_as_overwrite():
    plan = one([CrmRow("r1", "ana@northwind.io", {"phone_numbers": "+15550100"})])
    assert plan.skipped[0].reason == "multiselect_overwrite:phone_numbers"


def test_list_on_undeclared_attribute_is_skipped():
    plan = one([CrmRow("r1", "ana@northwind.io", {"segments": ["a", "b"]})])
    assert plan.skipped[0].reason == "undeclared_list:segments"


def test_matching_multiselect_is_additive_so_allowed():
    plan = one(
        [CrmRow("r1", properties={"lab_person_key": "pk-7", "email_addresses": "ana@n.io"})],
        matching_attribute="lab_person_key",
        multiselect=["tags"],
        overwrite=["email_addresses"],
    )
    call = plan.calls[0]
    assert call.path.endswith("matching_attribute=lab_person_key")
    assert call.values["lab_person_key"] == ["pk-7"]


def test_custom_matching_attribute_missing_key():
    plan = one([CrmRow("r1", "ana@northwind.io", {})], matching_attribute="lab_person_key")
    assert plan.skipped[0].reason == "missing_key:lab_person_key"


def test_fixture_duplicate_and_bad_names_are_skipped():
    plan = one(
        [
            CrmRow("r1", "ana@northwind.example"),
            CrmRow("r2", "bo@northwind.io"),
            CrmRow("r3", "BO@northwind.io"),
            CrmRow("r4", "cy@northwind.io", {"Job Title": "x"}),
            CrmRow("r5", None),
        ]
    )
    reasons = {o.row_id: o.reason for o in plan.skipped}
    assert reasons == {
        "r1": "fixture",
        "r3": "duplicate_key:first_row=r2",
        "r4": "invalid_property_name:Job Title",
        "r5": "missing_key:email_addresses",
    }
    assert [c.row_id for c in plan.calls] == ["r2"]


def test_duplicate_row_id_raises():
    with pytest.raises(ValueError):
        one([CrmRow("r1", "a@n.io"), CrmRow("r1", "b@n.io")])


def test_object_slug_is_url_quoted():
    call = one([CrmRow("r1", "a@n.io")], object="my obj/x").calls[0]
    assert call.path.startswith("/v2/objects/my%20obj%2Fx/records?")


# --- parsing -----------------------------------------------------------------------------


def test_200_is_upserted_never_guessed_created():
    call = one([CrmRow("r1", "a@n.io")]).calls[0]
    out = parse_assert_response(call, 200, record("rec-9"))
    assert (out.outcome, out.crm_id) == (Outcome.UPSERTED, "rec-9")


def test_200_without_record_id_is_retryable_failure():
    call = one([CrmRow("r1", "a@n.io")]).calls[0]
    out = parse_assert_response(call, 200, {"data": {}})
    assert (out.outcome, out.reason, out.retryable) == (Outcome.FAILED, "unparseable_body", True)


def test_documented_400_is_not_retryable_and_keeps_code():
    call = one([CrmRow("r1", "a@n.io")]).calls[0]
    body = {
        "status_code": 400,
        "type": "invalid_request_error",
        "code": "value_not_found",
        "message": "No attribute 'x'",
    }
    out = parse_assert_response(call, 400, body)
    assert out.reason == "http_400 value_not_found: No attribute 'x'"
    assert out.retryable is False


@pytest.mark.parametrize("status", [408, 429, 500, 503])
def test_transient_statuses_are_retryable(status):
    call = one([CrmRow("r1", "a@n.io")]).calls[0]
    assert parse_assert_response(call, status, None).retryable is True


def test_redirect_is_not_retryable():
    call = one([CrmRow("r1", "a@n.io")]).calls[0]
    assert parse_assert_response(call, 302, None).retryable is False


# --- executing ---------------------------------------------------------------------------


def run(plan, fake, **kw):
    sleeps = []
    report = execute_asserts(
        plan,
        fake,
        token="t",
        budget=RateBudget(25, 1.0, clock=lambda: 0.0, sleep=lambda s: None),
        sleep=sleeps.append,
        **kw,
    )
    return report, sleeps


def test_one_outcome_per_row_in_input_order_with_skips():
    plan = one([CrmRow("r1", "a@n.io"), CrmRow("r2", None), CrmRow("r3", "c@n.io")])
    fake = FakeAttio(HttpResponse(200, record("rec-a")), HttpResponse(200, record("rec-c")))
    report, _ = run(plan, fake)
    assert [(o.row_id, o.outcome) for o in report.outcomes] == [
        ("r1", Outcome.UPSERTED),
        ("r2", Outcome.SKIPPED),
        ("r3", Outcome.UPSERTED),
    ]
    assert fake.calls[0].url == (
        "https://api.attio.com/v2/objects/people/records?matching_attribute=email_addresses"
    )
    assert fake.calls[0].headers["Authorization"] == "Bearer t"


def test_429_waits_http_date_retry_after_then_lands():
    plan = one([CrmRow("r1", "a@n.io")])
    fake = FakeAttio(
        HttpResponse(
            429, {"code": "rate_limit_exceeded"}, {"Retry-After": "Thu, 01 Jan 2099 00:00:00 GMT"}
        ),
        HttpResponse(200, record()),
    )
    report, sleeps = run(plan, fake, max_wait_seconds=5)
    assert report.outcomes[0].outcome is Outcome.UPSERTED
    assert sleeps == [5]  # far-future date, capped
    assert report.summary()["retried_rows"] == 1


def test_timeout_and_reset_are_resent_because_assert_is_idempotent():
    plan = one([CrmRow("r1", "a@n.io")])
    fake = FakeAttio(TimeoutError(), TransportError("reset"), HttpResponse(200, record()))
    report, sleeps = run(plan, fake)
    assert report.outcomes[0].outcome is Outcome.UPSERTED
    assert len(fake.calls) == 3 and sleeps == [1.0, 2.0]


def test_rejected_row_is_sent_once():
    plan = one([CrmRow("r1", "a@n.io")])
    fake = FakeAttio(HttpResponse(400, {"code": "value_not_found", "message": "m"}))
    report, _ = run(plan, fake)
    assert len(fake.calls) == 1
    assert report.summary()["still_retryable"] == 0


def test_not_sent_after_last_attempt_stays_retryable():
    plan = one([CrmRow("r1", "a@n.io")])
    fake = FakeAttio(*[TransportError("dns", reached_vendor=False)] * 2)
    report, _ = run(plan, fake, max_attempts=2)
    out = report.outcomes[0]
    assert (out.outcome, out.reason, out.retryable) == (Outcome.FAILED, "not_sent", True)


def test_token_required():
    with pytest.raises(ValueError):
        execute_asserts(one([]), FakeAttio(), token="", budget=RateBudget())


# --- Retry-After -------------------------------------------------------------------------


def test_retry_after_forms():
    now = 1_700_000_000.0  # 2023-11-14T22:13:20Z
    assert retry_after_seconds({"retry-after": " 3 "}) == 3.0
    later = {"Retry-After": "Tue, 14 Nov 2023 22:13:50 GMT"}
    assert retry_after_seconds(later, now=lambda: now) == 30.0
    past = {"Retry-After": "Tue, 14 Nov 2023 22:00:00 GMT"}
    assert retry_after_seconds(past, now=lambda: now) == 0.0
    assert retry_after_seconds({"Retry-After": "soon"}) is None
    assert retry_after_seconds({}) is None
