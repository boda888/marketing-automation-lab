import pytest
from crmsync import CrmRow, Outcome, parse_upsert_response, plan_upserts
from crmsync.hubspot import UPSERT_PATH, trace_id


def rows(n, domain="northwind.io"):
    return [CrmRow(f"r{i}", f"p{i}@{domain}", {"firstname": f"P{i}"}) for i in range(n)]


def result(row, new=True, crm_id="101", echo_trace=True):
    item = {"id": crm_id, "properties": {"email": row.key}}
    if new is not None:
        item["_new"] = new
    if echo_trace:
        item["objectWriteTraceId"] = row.trace_id
    return item


# --- planning -----------------------------------------------------------------------------


def test_body_matches_sdk_upsert_input_shape():
    plan = plan_upserts([CrmRow("r1", " Ana@Northwind.io ", {"firstname": "Ana"})])
    (batch,) = plan.batches
    assert batch.method == "POST" and batch.path == UPSERT_PATH
    (item,) = batch.body()["inputs"]
    assert item == {
        "id": "ana@northwind.io",
        "idProperty": "email",
        "objectWriteTraceId": trace_id("ana@northwind.io"),
        "properties": {"firstname": "Ana", "email": "ana@northwind.io"},
    }


def test_batches_never_exceed_100_inputs():
    plan = plan_upserts(rows(250))
    assert [len(b.rows) for b in plan.batches] == [100, 100, 50]
    with pytest.raises(ValueError):
        plan_upserts(rows(1), batch_size=101)


def test_skips_have_one_reason_each_and_nothing_is_sent_twice():
    plan = plan_upserts(
        [
            CrmRow("r1", "Unknown"),
            CrmRow("r2", "ana@northwind.example"),
            CrmRow("r3", "bo@northwind.io"),
            CrmRow("r4", "BO@northwind.io", {"firstname": "Bo"}),
            CrmRow("r5", "cy@northwind.io", {"First Name": "Cy"}),
        ]
    )
    reasons = {s.row_id: s.reason for s in plan.skipped}
    assert reasons == {
        "r1": "missing_key:email",
        "r2": "fixture",
        "r4": "duplicate_key:first_row=r3",
        "r5": "invalid_property_name:First Name",
    }
    assert all(s.outcome is Outcome.SKIPPED for s in plan.skipped)
    assert [r.row_id for b in plan.batches for r in b.rows] == ["r3"]


def test_fixtures_only_with_explicit_flag():
    plan = plan_upserts([CrmRow("r1", "ana@northwind.example")], allow_fixtures=True)
    assert plan.batches and not plan.skipped


def test_blank_and_placeholder_values_are_not_sent():
    plan = plan_upserts(
        [
            CrmRow(
                "r1",
                "ana@northwind.io",
                {"jobtitle": "", "company": "Unknown", "phone": None, "score": 0},
            )
        ]
    )
    props = plan.batches[0].rows[0].properties
    assert props == {"score": "0", "email": "ana@northwind.io"}


def test_custom_unique_id_property():
    plan = plan_upserts(
        [CrmRow("r1", "ana@northwind.io", {"lab_person_key": "pk-1", "firstname": "Ana"})],
        id_property="lab_person_key",
    )
    (item,) = plan.batches[0].body()["inputs"]
    assert item["id"] == "pk-1" and item["idProperty"] == "lab_person_key"
    assert item["properties"]["email"] == "ana@northwind.io"
    missing = plan_upserts([CrmRow("r2", "bo@northwind.io")], id_property="lab_person_key")
    assert missing.skipped[0].reason == "missing_key:lab_person_key"


# --- parsing ------------------------------------------------------------------------------


def test_200_maps_created_and_updated_by_trace_id_in_batch_order():
    (batch,) = plan_upserts(rows(3)).batches
    a, b, c = batch.rows
    body = {
        "status": "COMPLETE",
        "results": [result(c, new=False, crm_id="3"), result(a, crm_id="1"), result(b, None)],
    }
    out = parse_upsert_response(batch, 200, body)
    assert [(o.row_id, o.outcome, o.crm_id) for o in out] == [
        ("r0", Outcome.CREATED, "1"),
        ("r1", Outcome.UPSERTED, "101"),
        ("r2", Outcome.UPDATED, "3"),
    ]


def test_207_is_split_per_row_and_never_reported_as_success():
    (batch,) = plan_upserts(rows(3)).batches
    a, b, c = batch.rows
    body = {
        "status": "COMPLETE",
        "numErrors": 1,
        "results": [result(a)],
        "errors": [
            {
                "status": "error",
                "category": "VALIDATION_ERROR",
                "message": "Property values were not valid",
                "context": {"ids": [b.key.upper()]},
                "errors": [],
            }
        ],
    }
    out = parse_upsert_response(batch, 207, body)
    assert out[0].outcome is Outcome.CREATED
    assert out[1].outcome is Outcome.FAILED and not out[1].retryable
    assert out[1].reason == "VALIDATION_ERROR: Property values were not valid"
    # c is in neither results nor errors: failed, safe to resend because upsert is idempotent.
    assert out[2].outcome is Outcome.FAILED and out[2].retryable
    assert out[2].reason == "not_in_response"


def test_unattributable_errors_are_carried_on_missing_rows():
    (batch,) = plan_upserts(rows(2)).batches
    body = {
        "status": "COMPLETE",
        "results": [result(batch.rows[0])],
        "errors": [{"category": "OBJECT_ALREADY_EXISTS", "message": "dup", "context": {}}],
    }
    out = parse_upsert_response(batch, 207, body)
    assert out[1].reason == "not_in_response; unattributed errors: OBJECT_ALREADY_EXISTS: dup"


def test_result_without_trace_falls_back_to_echoed_key():
    (batch,) = plan_upserts(rows(1)).batches
    body = {"status": "COMPLETE", "results": [result(batch.rows[0], echo_trace=False)]}
    assert parse_upsert_response(batch, 200, body)[0].outcome is Outcome.CREATED


def test_unknown_results_are_ignored_not_attributed():
    (batch,) = plan_upserts(rows(1)).batches
    stranger = {"id": "9", "_new": True, "properties": {"email": "x@other.io"}}
    out = parse_upsert_response(batch, 200, {"status": "COMPLETE", "results": [stranger]})
    assert out[0].outcome is Outcome.FAILED and out[0].reason == "not_in_response"


def test_incomplete_batch_status_marks_missing_rows_retryable():
    (batch,) = plan_upserts(rows(2)).batches
    body = {"status": "PROCESSING", "results": [result(batch.rows[0])]}
    out = parse_upsert_response(batch, 200, body)
    assert out[1].reason == "batch_PROCESSING" and out[1].retryable


@pytest.mark.parametrize("status", [429, 500, 502, 503])
def test_rate_limit_and_server_errors_fail_every_row_retryably(status):
    (batch,) = plan_upserts(rows(2)).batches
    out = parse_upsert_response(batch, status, None)
    assert {(o.outcome, o.reason, o.retryable) for o in out} == {
        (Outcome.FAILED, f"http_{status}", True)
    }


def test_client_errors_fail_every_row_without_retry():
    (batch,) = plan_upserts(rows(2)).batches
    body = {"status": "error", "category": "INVALID_AUTHENTICATION", "message": "expired"}
    out = parse_upsert_response(batch, 401, body)
    assert all(o.outcome is Outcome.FAILED and not o.retryable for o in out)
    assert out[0].reason == "http_401 INVALID_AUTHENTICATION: expired"


def test_unparseable_success_body_is_not_success():
    (batch,) = plan_upserts(rows(1)).batches
    out = parse_upsert_response(batch, 200, "<html>proxy</html>")
    assert out[0].outcome is Outcome.FAILED and out[0].reason == "unparseable_body"
