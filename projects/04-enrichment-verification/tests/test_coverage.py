import pytest
from enrichment import (
    FakeProvider,
    LeadRecord,
    Ratio,
    Severity,
    Waterfall,
    check_mappings,
    coverage,
)

LI = (("linkedin_url",),)
NAME = (("first_name", "last_name", "company_domain"),)


def rec(rid, **fields):
    return LeadRecord(record_id=rid, fields=fields)


def li(n):
    return f"https://www.linkedin.com/in/person-{n}"


def codes(report):
    return {(f.code, f.provider, f.severity) for f in report.findings}


# --- Ratio -----------------------------------------------------------------


def test_ratio_zero_of_zero_is_unknown_not_zero():
    r = Ratio(numerator=0, denominator=0, denominator_name="input records")
    assert r.value is None
    assert str(r) == "0/0 input records (n/a)"


def test_ratio_names_its_denominator():
    assert str(Ratio(numerator=9, denominator=20, denominator_name="x")) == "9/20 x (45%)"


# --- check_mappings ----------------------------------------------------------


def test_empty_input_mapping_is_an_error_before_any_call():
    # PRACTICE: a provider step with an empty input mapping capped coverage near 45%.
    by_li = FakeProvider("by_li", "email", LI)
    by_name = FakeProvider("by_name", "email", NAME)
    records = [rec(str(i), linkedin_url=li(i)) for i in range(4)]
    report = check_mappings(Waterfall("email", [by_li, by_name]), records)
    assert not report.ok
    assert ("INPUT_FIELD_EMPTY_EVERYWHERE", "by_name", Severity.ERROR) in codes(report)
    assert ("NO_ELIGIBLE_RECORDS", "by_name", Severity.ERROR) in codes(report)
    assert by_li.calls == [] and by_name.calls == []
    name_row = report.providers[1]
    assert name_row.missing_fields == {"company_domain": 4, "first_name": 4, "last_name": 4}


def test_placeholder_column_counts_as_empty():
    p = FakeProvider("by_li", "email", LI)
    records = [rec("1", linkedin_url="Unknown"), rec("2", linkedin_url="N/A")]
    report = check_mappings(Waterfall("email", [p]), records)
    assert ("INPUT_FIELD_EMPTY_EVERYWHERE", "by_li", Severity.ERROR) in codes(report)


def test_dead_alternative_set_is_only_a_warning():
    p = FakeProvider("p", "email", LI + NAME)
    records = [rec(str(i), linkedin_url=li(i)) for i in range(3)]
    report = check_mappings(Waterfall("email", [p]), records)
    assert report.ok
    assert ("INPUT_FIELD_EMPTY_EVERYWHERE", "p", Severity.WARNING) in codes(report)
    assert report.providers[0].eligible.numerator == 3


def test_eligibility_uses_records_the_waterfall_runs_on():
    p = FakeProvider("p", "email", LI)
    records = [
        rec("1", linkedin_url=li(1)),
        rec("2"),
        rec("3", email="filled@northwind.example", linkedin_url=li(3)),  # condition false
    ]
    report = check_mappings(Waterfall("email", [p]), records)
    assert (report.records_total, report.records_to_run) == (3, 2)
    elig = report.providers[0].eligible
    assert (elig.numerator, elig.denominator, elig.denominator_name) == (
        1,
        2,
        "records the waterfall runs on",
    )
    assert ("LOW_ELIGIBILITY", "p", Severity.WARNING) not in codes(report)  # 50% is the floor
    assert report.unreachable.numerator == 1
    assert ("RECORDS_WITHOUT_PROVIDER", None, Severity.WARNING) in codes(report)


def test_low_eligibility_warning():
    p = FakeProvider("p", "email", LI)
    records = [rec("1", linkedin_url=li(1)), rec("2", x="y"), rec("3", x="y")]
    report = check_mappings(Waterfall("email", [p]), records, min_eligible=0.5)
    assert ("LOW_ELIGIBILITY", "p", Severity.WARNING) in codes(report)
    assert report.ok


def test_fixtures_are_counted_as_blocked_for_paid_providers():
    paid = FakeProvider("paid", "email", LI, paid=True)
    free = FakeProvider("free", "email", LI)
    records = [
        rec(str(i), linkedin_url=li(i), company_domain="northwind.example") for i in range(2)
    ]
    report = check_mappings(Waterfall("email", [paid, free]), records)
    paid_row, free_row = report.providers
    assert (paid_row.fixture_blocked, paid_row.eligible.numerator) == (2, 0)
    assert free_row.eligible.numerator == 2
    assert report.unreachable.numerator == 0


def test_no_records_reports_nothing_to_do():
    p = FakeProvider("p", "email", LI)
    report = check_mappings(Waterfall("email", [p]), [])
    assert report.ok and report.findings == []
    assert report.providers[0].eligible.value is None


# --- coverage ----------------------------------------------------------------


def run_batch():
    cheap = FakeProvider(
        "cheap", "email", LI, answers={li(1): "p1@northwind.example"}, cost_per_call=1
    )
    deep = FakeProvider(
        "deep",
        "email",
        LI + NAME,
        answers={li(2): "p2@northwind.example", "info-co.example": "info@info-co.example"},
        errors=frozenset({li(4)}),
        cost_per_call=3,
        charge_on_miss=False,
    )
    w = Waterfall("email", [cheap, deep], accept=lambda v: not v.startswith("info@"))
    records = [
        rec("1", linkedin_url=li(1)),
        rec("2", linkedin_url=li(2)),
        rec("3", first_name="Bo", last_name="Fict", company_domain="info-co.example"),
        rec("4", linkedin_url=li(4)),
        rec("5", email="kept@northwind.example"),
        rec("6"),
    ]
    return w.run_many(records)


def test_coverage_names_both_denominators():
    report = coverage(run_batch())
    assert str(report.found_of_run) == "2/5 records the waterfall runs on (40%)"
    assert str(report.found_of_input) == "2/6 input records (33%)"
    assert report.status_counts == {
        "found": 2,
        "not_found": 2,
        "not_run": 1,
        "no_eligible_provider": 1,
        "budget_spent": 0,
        "outcome_unknown": 0,
    }


def test_coverage_per_provider_counts_calls_not_skips():
    by_name = {p.provider: p for p in coverage(run_batch()).providers}
    cheap, deep = by_name["cheap"], by_name["deep"]
    # cheap: called for 1, 2, 4 (LI); skipped for 3 (no LI) and 6.
    assert (cheap.calls, cheap.answered.numerator, cheap.skipped) == (3, 1, 2)
    assert cheap.cost == 3
    # deep: called for 2 (hit), 3 (rejected role address, charged), 4 (error); skipped for 6.
    assert (deep.calls, deep.answered.numerator, deep.rejected, deep.errors) == (3, 1, 1, 1)
    assert deep.answered.denominator_name == "calls to this provider"
    assert deep.cost == 6


def test_cost_per_found():
    report = coverage(run_batch())
    assert report.cost == 9
    assert report.cost_per_found == 4.5


def test_nothing_found_has_no_cost_per_found():
    p = FakeProvider("p", "email", LI)
    report = coverage(Waterfall("email", [p]).run_many([rec("1", linkedin_url=li(1))]))
    assert report.cost == 1 and report.cost_per_found is None
    assert report.found_of_run.value == 0


def test_coverage_refuses_mixed_fields():
    email = Waterfall("email", [FakeProvider("a", "email", LI)]).run(rec("1"))
    phone = Waterfall("phone", [FakeProvider("b", "phone", LI)]).run(rec("1"))
    with pytest.raises(ValueError, match="mix fields"):
        coverage([email, phone])
