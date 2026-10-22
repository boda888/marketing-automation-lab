from datetime import UTC, datetime, timedelta, timezone

import pytest
from enrichment import (
    EMAILS_FOUND,
    CheckResult,
    EmailCheck,
    EmailStatus,
    FakeProvider,
    LeadRecord,
    Waterfall,
    assess_email,
    assess_outcome,
    ladder_summary,
)
from pydantic import ValidationError

NOW = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)
ADA = "ada@northwind.example"


def check(result, *, checker="verifier", days_ago=1, email=ADA, provider_status=None):
    return EmailCheck(
        email=email,
        checker=checker,
        checked_at=NOW - timedelta(days=days_ago),
        result=result,
        provider_status=provider_status,
    )


def assess(checks, **kw):
    return assess_email(ADA, "finder", checks, now=NOW, **kw)


# --- the ladder --------------------------------------------------------------


def test_found_without_any_check_stays_found():
    a = assess([])
    assert (a.status, a.reason, a.sendable) == (EmailStatus.FOUND, "no_check", False)
    assert a.deciding_check is None


def test_independent_valid_check_verifies():
    a = assess([check(CheckResult.VALID)])
    assert a.status is EmailStatus.VERIFIED
    assert a.sendable
    assert a.deciding_check.checker == "verifier"


def test_finders_own_valid_label_is_context_not_verification():
    a = assess([check(CheckResult.VALID, checker="Finder")])
    assert (a.status, a.reason) == (EmailStatus.CHECKED, "checked_by_finder_only")
    assert not a.sendable


def test_finder_self_check_can_be_allowed_explicitly():
    a = assess([check(CheckResult.VALID, checker="finder")], require_independent=False)
    assert a.status is EmailStatus.VERIFIED


@pytest.mark.parametrize(
    ("result", "status"),
    [
        (CheckResult.CATCH_ALL, EmailStatus.CHECKED),
        (CheckResult.UNKNOWN, EmailStatus.CHECKED),
        (CheckResult.INVALID, EmailStatus.INVALID),
        (CheckResult.DO_NOT_MAIL, EmailStatus.INVALID),
    ],
)
def test_non_valid_results(result, status):
    a = assess([check(result)])
    assert (a.status, a.reason) == (status, result.value)
    assert not a.sendable


def test_stale_valid_check_falls_back_to_found():
    a = assess([check(CheckResult.VALID, days_ago=120)])
    assert (a.status, a.reason) == (EmailStatus.FOUND, "only_stale_checks")
    assert a.checks_ignored == {"stale": 1}


def test_max_age_is_configurable():
    a = assess([check(CheckResult.VALID, days_ago=40)], max_age=timedelta(days=30))
    assert a.status is EmailStatus.FOUND


def test_check_of_another_address_is_ignored():
    # PRACTICE: QA found the new employer, later steps still read the old employer's email.
    old = check(CheckResult.VALID, email="ada@oldco.example")
    a = assess([old])
    assert a.status is EmailStatus.FOUND
    assert a.checks_ignored == {"other_address": 1}


def test_address_match_ignores_case_and_blanks():
    a = assess([check(CheckResult.VALID, email="  Ada@Northwind.EXAMPLE ")])
    assert a.status is EmailStatus.VERIFIED


def test_newer_invalid_overrides_older_valid():
    a = assess([check(CheckResult.VALID, days_ago=10), check(CheckResult.INVALID, days_ago=2)])
    assert a.status is EmailStatus.INVALID
    assert a.checks_used == 2


def test_newer_valid_overrides_older_catch_all():
    a = assess([check(CheckResult.CATCH_ALL, days_ago=10), check(CheckResult.VALID, days_ago=2)])
    assert a.status is EmailStatus.VERIFIED


def test_newer_unknown_does_not_erase_an_older_conclusive_result():
    a = assess([check(CheckResult.INVALID, days_ago=10), check(CheckResult.UNKNOWN, days_ago=1)])
    assert a.status is EmailStatus.INVALID


def test_same_timestamp_most_cautious_wins():
    a = assess(
        [
            check(CheckResult.VALID, checker="a", days_ago=3),
            check(CheckResult.DO_NOT_MAIL, checker="b", days_ago=3),
        ]
    )
    assert (a.status, a.deciding_check.checker) == (EmailStatus.INVALID, "b")


def test_timezones_are_compared_as_instants():
    plus2 = timezone(timedelta(hours=2))
    # 13:30+02:00 is 11:30 UTC: before NOW, so not a future-dated check.
    c = EmailCheck(
        email=ADA,
        checker="verifier",
        checked_at=datetime(2026, 10, 22, 13, 30, tzinfo=plus2),
        result=CheckResult.VALID,
    )
    assert assess([c]).status is EmailStatus.VERIFIED


# --- inputs are evidence ------------------------------------------------------


def test_future_dated_check_raises():
    with pytest.raises(ValueError, match="after now"):
        assess([check(CheckResult.VALID, days_ago=-1)])


def test_naive_check_date_is_rejected():
    with pytest.raises(ValidationError, match="timezone"):
        EmailCheck(email=ADA, checker="v", checked_at=datetime(2026, 10, 1), result="valid")


@pytest.mark.parametrize("checker", ["", "Unknown", "n/a"])
def test_checker_must_be_named(checker):
    with pytest.raises(ValidationError):
        check(CheckResult.VALID, checker=checker)


@pytest.mark.parametrize(("email", "finder"), [("Unknown", "finder"), (ADA, ""), (ADA, "N/A")])
def test_found_email_needs_address_and_finder(email, finder):
    with pytest.raises(ValueError, match="address and the provider"):
        assess_email(email, finder, [], now=NOW)


def test_naive_now_is_rejected():
    with pytest.raises(ValueError, match="timezone"):
        assess_email(ADA, "finder", [], now=datetime(2026, 10, 22))


# --- ZeroBounce mapping --------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "result"),
    [
        ("valid", CheckResult.VALID),
        ("invalid", CheckResult.INVALID),
        ("catch-all", CheckResult.CATCH_ALL),
        ("unknown", CheckResult.UNKNOWN),
        ("", CheckResult.UNKNOWN),
        ("spamtrap", CheckResult.DO_NOT_MAIL),
        ("abuse", CheckResult.DO_NOT_MAIL),
        ("do_not_mail", CheckResult.DO_NOT_MAIL),
    ],
)
def test_zerobounce_statuses(status, result):
    c = EmailCheck.from_zerobounce(ADA, status, checked_at=NOW)
    assert (c.result, c.checker) == (result, "zerobounce")


def test_zerobounce_keeps_sub_status_as_context():
    c = EmailCheck.from_zerobounce(ADA, "do_not_mail", "role_based", checked_at=NOW)
    assert c.provider_status == "do_not_mail/role_based"


def test_zerobounce_unknown_status_raises_instead_of_guessing():
    with pytest.raises(ValueError, match="unknown ZeroBounce status"):
        EmailCheck.from_zerobounce(ADA, "probably_ok", checked_at=NOW)


# --- waterfall outcome -> ladder ------------------------------------------------


def test_assess_outcome_uses_the_provider_that_answered():
    li = (("linkedin_url",),)
    url = "https://www.linkedin.com/in/ada"
    finder = FakeProvider("finder", "email", li, answers={url: ADA}, provider_status="verified")
    out = Waterfall("email", [finder]).run(
        LeadRecord(record_id="lead-1", fields={"linkedin_url": url})
    )
    # The finder's own "verified" label does not make the address verified.
    a = assess_outcome(out, [], now=NOW)
    assert (a.found_by, a.status) == ("finder", EmailStatus.FOUND)
    assert assess_outcome(out, [check(CheckResult.VALID)], now=NOW).sendable


def test_assess_outcome_none_when_nothing_found():
    finder = FakeProvider("finder", "email", (("linkedin_url",),))
    out = Waterfall("email", [finder]).run(
        LeadRecord(record_id="lead-1", fields={"linkedin_url": "https://www.linkedin.com/in/x"})
    )
    assert assess_outcome(out, [], now=NOW) is None


# --- summary: no promotion by arithmetic -------------------------------------


def test_removing_invalid_does_not_make_the_rest_verified():
    # PRACTICE: "rows left after removing invalid" were reported as verified.
    emails = [f"p{i}@northwind.example" for i in range(4)]
    checks = [
        EmailCheck(email=emails[0], checker="v", checked_at=NOW, result="invalid"),
        EmailCheck(email=emails[1], checker="v", checked_at=NOW, result="valid"),
    ]
    found = [assess_email(e, "finder", checks, now=NOW) for e in emails]
    s = ladder_summary(found)
    assert s.sendable.numerator == 1
    assert s.by_status[EmailStatus.FOUND].numerator == 2
    assert s.by_status[EmailStatus.INVALID].numerator == 1
    assert str(s.sendable) == f"1/4 {EMAILS_FOUND} (25%)"
    assert s.by_reason == {"invalid": 1, "valid": 1, "no_check": 2}


def test_summary_of_nothing_is_unknown_not_zero():
    s = ladder_summary([])
    assert s.sendable.value is None


def test_summary_refuses_the_same_address_twice():
    a = assess([])
    with pytest.raises(ValueError, match="assessed twice"):
        ladder_summary([a, a])
