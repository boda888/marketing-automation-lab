import tomllib
from pathlib import Path

import enrichment
import pytest
from enrichment import (
    AttemptStatus,
    FakeProvider,
    LeadRecord,
    Provider,
    Waterfall,
    WaterfallStatus,
    select_inputs,
)

LI = "https://www.linkedin.com/in/ada-fictional"
BY_LINKEDIN = (("linkedin_url",),)
BY_NAME = (("first_name", "last_name", "company_domain"),)


def rec(rid="lead-1", **fields):
    return LeadRecord(record_id=rid, fields=fields)


def email_provider(name, answers=None, input_sets=BY_LINKEDIN, **kw):
    return FakeProvider(name, "email", input_sets, answers=answers or {}, **kw)


def test_fake_satisfies_protocol():
    assert isinstance(email_provider("a"), Provider)


def test_stops_at_first_acceptable_answer_and_records_provider():
    cheap = email_provider("cheap", {LI: "ada@northwind.example"}, cost_per_call=0.5)
    pricey = email_provider("pricey", {LI: "other@northwind.example"}, cost_per_call=3)
    out = Waterfall("email", [cheap, pricey]).run(rec(linkedin_url=LI))
    assert out.status is WaterfallStatus.FOUND
    assert (out.value, out.answered_by) == ("ada@northwind.example", "cheap")
    assert pricey.calls == []
    assert out.cost == 0.5 and out.calls == 1


def test_falls_through_misses_and_sums_cost():
    a = email_provider("a", cost_per_call=1)
    b = email_provider("b", cost_per_call=2, charge_on_miss=False)
    c = email_provider("c", {LI: "ada@northwind.example"}, cost_per_call=4)
    out = Waterfall("email", [a, b, c]).run(rec(linkedin_url=LI))
    assert [x.status for x in out.attempts] == [
        AttemptStatus.NOT_FOUND,
        AttemptStatus.NOT_FOUND,
        AttemptStatus.FOUND,
    ]
    assert out.answered_by == "c" and out.cost == 5


def test_default_condition_skips_filled_field_without_calls():
    p = email_provider("a", {LI: "new@northwind.example"})
    out = Waterfall("email", [p]).run(rec(linkedin_url=LI, email="kept@northwind.example"))
    assert out.status is WaterfallStatus.NOT_RUN
    assert out.attempts == [] and p.calls == [] and out.cost == 0


def test_explicit_condition_controls_the_run():
    p = email_provider("a", {LI: "ada@northwind.example"})
    only_tier1 = Waterfall("email", [p], run_if=lambda r: r.get("tier") == "Tier 1")
    assert only_tier1.run(rec(linkedin_url=LI, tier="Tier 3")).status is WaterfallStatus.NOT_RUN
    assert only_tier1.run(rec(linkedin_url=LI, tier="Tier 1")).status is WaterfallStatus.FOUND


def test_placeholder_input_never_reaches_a_provider():
    # PRACTICE: the string "Unknown" sent as a LinkedIn URL wasted paid credits.
    p = email_provider("a", {"Unknown": "junk@northwind.example"})
    out = Waterfall("email", [p]).run(rec(linkedin_url="Unknown"))
    assert p.calls == []
    assert out.status is WaterfallStatus.NO_ELIGIBLE_PROVIDER
    assert out.attempts[0].status is AttemptStatus.SKIPPED_MISSING_INPUTS
    assert out.attempts[0].detail == "needs linkedin_url"


def test_alternative_input_sets_in_preference_order():
    p = email_provider(
        "a", {"northwind.example": "ada@northwind.example"}, input_sets=BY_LINKEDIN + BY_NAME
    )
    r = rec(first_name="Ada", last_name="Fictional", company_domain="northwind.example")
    assert select_inputs(p, r) == {
        "first_name": "Ada",
        "last_name": "Fictional",
        "company_domain": "northwind.example",
    }
    out = Waterfall("email", [p]).run(r)
    assert out.attempts[0].inputs_used == ["first_name", "last_name", "company_domain"]
    assert p.calls == [
        {"first_name": "Ada", "last_name": "Fictional", "company_domain": "northwind.example"}
    ]
    both = rec(linkedin_url=LI, first_name="Ada", last_name="F", company_domain="northwind.example")
    assert select_inputs(p, both) == {"linkedin_url": LI}


def test_only_matched_input_fields_are_sent():
    p = email_provider("a")
    Waterfall("email", [p]).run(rec(linkedin_url=LI, phone="+1 555 0100", notes="vip"))
    assert p.calls == [{"linkedin_url": LI}]


def test_paid_provider_never_sees_a_fixture():
    paid = email_provider("paid", {LI: "ada@northwind.example"}, paid=True)
    free = email_provider("free")
    out = Waterfall("email", [paid, free]).run(
        rec(linkedin_url=LI, company_domain="northwind.example")
    )
    assert paid.calls == []
    assert out.attempts[0].status is AttemptStatus.SKIPPED_FIXTURE
    assert out.status is WaterfallStatus.NOT_FOUND  # the free fake was still called


def test_paid_provider_runs_on_real_looking_record():
    paid = email_provider("paid", {LI: "ada@northwind.io"}, paid=True)
    out = Waterfall("email", [paid]).run(rec(linkedin_url=LI, company_domain="northwind.io"))
    assert out.status is WaterfallStatus.FOUND and len(paid.calls) == 1


def test_only_fixture_skips_means_no_eligible_provider():
    paid = email_provider("paid", paid=True)
    out = Waterfall("email", [paid]).run(rec(linkedin_url=LI, email_domain="northwind.example"))
    assert out.status is WaterfallStatus.NO_ELIGIBLE_PROVIDER and out.calls == 0


def test_rejected_value_moves_on():
    a = email_provider("a", {LI: "info@northwind.example"}, provider_status="catch_all")
    b = email_provider("b", {LI: "ada@northwind.example"})
    not_role = Waterfall("email", [a, b], accept=lambda v: not v.startswith("info@"))
    out = not_role.run(rec(linkedin_url=LI))
    assert out.attempts[0].status is AttemptStatus.REJECTED
    assert out.attempts[0].detail == "catch_all"
    assert out.answered_by == "b"


def test_provider_error_is_recorded_and_waterfall_continues():
    a = email_provider("a", errors=frozenset({LI}))
    b = email_provider("b", {LI: "ada@northwind.example"})
    out = Waterfall("email", [a, b]).run(rec(linkedin_url=LI))
    assert out.attempts[0].status is AttemptStatus.ERROR
    assert "503" in out.attempts[0].detail
    assert out.status is WaterfallStatus.FOUND


def test_placeholder_answer_counts_as_not_found():
    a = email_provider("a", {LI: "Unknown"})
    out = Waterfall("email", [a]).run(rec(linkedin_url=LI))
    assert out.status is WaterfallStatus.NOT_FOUND and out.value is None


def test_attempts_never_contain_input_values():
    a = email_provider("a")
    out = Waterfall("email", [a]).run(rec(linkedin_url=LI))
    assert LI not in out.model_dump_json()


@pytest.mark.parametrize(
    ("providers", "message"),
    [
        ([], "no providers"),
        ([email_provider("a"), email_provider("a")], "duplicate provider"),
        ([FakeProvider("p", "phone", BY_LINKEDIN)], "returns 'phone'"),
        ([email_provider("a", input_sets=())], "input set"),
        ([email_provider("a", input_sets=((),))], "input set"),
    ],
)
def test_misconfigured_waterfall_fails_loudly(providers, message):
    with pytest.raises(ValueError, match=message):
        Waterfall("email", providers)


def test_run_many_needs_stable_unique_ids():
    w = Waterfall("email", [email_provider("a", {LI: "ada@northwind.example"})])
    outs = w.run_many([rec("1", linkedin_url=LI), rec("2")])
    assert [o.status for o in outs] == [WaterfallStatus.FOUND, WaterfallStatus.NO_ELIGIBLE_PROVIDER]
    with pytest.raises(ValueError, match="duplicate record_id"):
        w.run_many([rec("1"), rec("1")])


def test_version_agrees_with_pyproject():
    meta = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())
    assert enrichment.__version__ == meta["project"]["version"]
