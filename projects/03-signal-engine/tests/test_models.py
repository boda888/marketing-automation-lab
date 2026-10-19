from datetime import UTC, date, datetime

import pytest
import signal_engine
from pydantic import ValidationError
from signal_engine import DatePrecision, EventDate, SignalEvent, SignalStatus, SignalType

SEEN = datetime(2026, 10, 19, 9, 0, tzinfo=UTC)


def found(**kw):
    base = {
        "company_domain": "northwind.example",
        "type": "funding",
        "status": "found",
        "observed_at": SEEN,
        "source_url": "https://news.northwind.example/2026/03/series-a",
        "evidence": "Northwind raised a $12M Series A led by Fictive Ventures.",
        "occurred_on": "2026-03",
    }
    base.update(kw)
    return SignalEvent(**base)


# --- EventDate -------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "precision", "earliest", "latest"),
    [
        ("2026-03-14", DatePrecision.DAY, date(2026, 3, 14), date(2026, 3, 14)),
        ("2026-02", DatePrecision.MONTH, date(2026, 2, 1), date(2026, 2, 28)),
        ("2024-02", DatePrecision.MONTH, date(2024, 2, 1), date(2024, 2, 29)),
        ("2025", DatePrecision.YEAR, date(2025, 1, 1), date(2025, 12, 31)),
    ],
)
def test_event_date_precision_is_an_interval(text, precision, earliest, latest):
    d = EventDate.parse(text)
    assert d.precision is precision
    assert (d.earliest, d.latest) == (earliest, latest)
    assert str(d) == text


@pytest.mark.parametrize("bad", ["", "Q1 2026", "2026/03", "03-2026", "2026-13", "2026-02-30"])
def test_event_date_rejects_non_iso_or_impossible(bad):
    with pytest.raises(ValueError):
        EventDate.parse(bad)


def test_event_date_value_must_start_its_interval():
    with pytest.raises(ValidationError):
        EventDate(value=date(2026, 3, 14), precision=DatePrecision.MONTH)
    with pytest.raises(ValidationError):
        EventDate(value=date(2026, 3, 1), precision=DatePrecision.YEAR)


def test_age_days_covers_whole_interval_and_never_negative():
    as_of = date(2026, 10, 19)
    assert EventDate.parse("2026-10-19").age_days(as_of) == (0, 0)
    # "March 2026" is between 202 and 232 days old: a 12-month window must hold for both.
    assert EventDate.parse("2026-03").age_days(as_of) == (202, 232)
    # Year precision for the current year: youngest possible age is 0, not negative.
    assert EventDate.parse("2026").age_days(as_of) == (0, 291)


# --- SignalEvent: found -----------------------------------------------------


def test_found_event_round_trips_with_parsed_date():
    e = found()
    assert e.is_found and e.type is SignalType.FUNDING
    assert e.occurred_on == EventDate.parse("2026-03")
    again = SignalEvent.model_validate_json(e.model_dump_json())
    assert again == e
    assert again.occurred_on.precision is DatePrecision.MONTH


def test_found_requires_source_url_and_evidence():
    with pytest.raises(ValidationError, match="source_url is required"):
        found(source_url=None)
    with pytest.raises(ValidationError, match="evidence is required"):
        found(evidence="   ")


@pytest.mark.parametrize(
    "url", ["https://northwind.example", "https://northwind.example/", "http://www.x.example/"]
)
def test_found_rejects_bare_homepage_as_evidence(url):
    with pytest.raises(ValidationError, match="not a homepage"):
        found(source_url=url)


@pytest.mark.parametrize("url", ["northwind.example/jobs", "ftp://x.example/a", "mailto:a@b.c"])
def test_source_url_must_be_absolute_http(url):
    with pytest.raises(ValidationError, match="absolute http"):
        found(source_url=url)


def test_found_event_cannot_happen_after_it_was_observed():
    with pytest.raises(ValidationError, match="after observed_at"):
        found(occurred_on="2026-11")
    # Same month as observation is fine even though the month's end is in the future.
    assert found(occurred_on="2026-10").occurred_on.latest == date(2026, 10, 31)


def test_found_without_stated_date_is_allowed():
    assert found(occurred_on=None).occurred_on is None


def test_observed_at_must_be_timezone_aware():
    with pytest.raises(ValidationError, match="timezone-aware"):
        found(observed_at=datetime(2026, 10, 19, 9, 0))


# --- SignalEvent: not found / unknown --------------------------------------


def test_not_found_in_sample_requires_what_was_searched():
    with pytest.raises(ValidationError, match="sample"):
        SignalEvent(
            company_domain="northwind.example",
            type="hiring",
            status="not_found_in_sample",
            observed_at=SEEN,
        )
    e = SignalEvent(
        company_domain="northwind.example",
        type="hiring",
        status="not_found_in_sample",
        observed_at=SEEN,
        source_url="https://northwind.example/careers",
        sample="careers page, 2026-10-19",
    )
    assert e.status is SignalStatus.NOT_FOUND_IN_SAMPLE and not e.is_found


def test_unknown_is_its_own_state_not_a_negative():
    e = SignalEvent(
        company_domain="northwind.example", type="site_visit", status="unknown", observed_at=SEEN
    )
    assert e.status is SignalStatus.UNKNOWN
    assert e.status is not SignalStatus.NOT_FOUND_IN_SAMPLE
    assert not e.is_found


@pytest.mark.parametrize("status", ["not_found_in_sample", "unknown"])
def test_non_found_states_carry_no_evidence_or_date(status):
    with pytest.raises(ValidationError, match="only belong to found"):
        SignalEvent(
            company_domain="northwind.example",
            type="funding",
            status=status,
            observed_at=SEEN,
            sample="press page",
            evidence="Raised a round",
        )
    with pytest.raises(ValidationError, match="only belong to found"):
        SignalEvent(
            company_domain="northwind.example",
            type="funding",
            status=status,
            observed_at=SEEN,
            sample="press page",
            occurred_on="2026-03",
        )


def test_found_event_has_no_sample():
    with pytest.raises(ValidationError, match="sample is only"):
        found(sample="press page")


# --- normalisation ----------------------------------------------------------


@pytest.mark.parametrize("placeholder", ["Unknown", "N/A", " none ", "-", ""])
def test_placeholder_strings_become_blank(placeholder):
    e = found(source_event_id=placeholder, source_name=placeholder)
    assert e.source_event_id is None and e.source_name is None


def test_placeholder_evidence_does_not_count_as_evidence():
    with pytest.raises(ValidationError, match="evidence is required"):
        found(evidence="Unknown")


def test_company_domain_normalised_and_fixture_flagged():
    e = found(company_domain="HTTPS://www.Northwind.example/about")
    assert e.company_domain == "northwind.example"
    assert e.is_fixture
    assert not found(company_domain="northwind.io").is_fixture


def test_unknown_signal_type_rejected():
    with pytest.raises(ValidationError):
        found(type="rumour")


def test_version_matches_pyproject():
    import tomllib
    from pathlib import Path

    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    assert tomllib.loads(pyproject.read_text())["project"]["version"] == signal_engine.__version__
