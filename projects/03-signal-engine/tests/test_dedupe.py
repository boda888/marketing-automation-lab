from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from signal_engine import (
    DatePrecision,
    MatchReason,
    SignalEvent,
    dedupe,
    normalise_subject,
)

SEEN = datetime(2026, 10, 19, 9, 0, tzinfo=UTC)


def ev(**kw):
    base = {
        "company_domain": "northwind.example",
        "type": "funding",
        "status": "found",
        "observed_at": SEEN,
        "source_url": "https://news.northwind.example/2026/03/series-a",
        "source_name": "press_release",
        "evidence": "Northwind raised a $12M Series A led by Fictive Ventures.",
        "subject": "Series A",
        "occurred_on": "2026-03-14",
    }
    base.update(kw)
    return SignalEvent(**base)


def opening(job_id, **kw):
    base = {
        "type": "hiring",
        "source_url": f"https://jobs.lever.example/northwind/{job_id}",
        "source_name": "lever",
        "source_event_id": job_id,
        "evidence": "Senior Data Engineer, Lisbon (hybrid).",
        "subject": "Senior Data Engineer",
        "occurred_on": "2026-10-02",
    }
    base.update(kw)
    return ev(**base)


def test_one_funding_round_from_two_sources_is_one_signal_with_both_urls():
    press = ev()
    database = ev(
        source_url="https://db.fictive-data.example/rounds/northwind-series-a",
        source_name="funding_db",
        source_event_id="r-1029",
        evidence="Series A, $12M, announced March 2026.",
        subject="series-A",
        occurred_on="2026-03",
        observed_at=SEEN - timedelta(days=2),
    )
    result = dedupe([press, database])

    assert result.input_found == 2
    assert result.duplicates_removed == 1
    (signal,) = result.signals
    assert signal.match_reasons == [MatchReason.SAME_SUBJECT_AND_DATE]
    assert {p.source_url for p in signal.sightings} == {press.source_url, database.source_url}
    assert signal.source_count == 2
    # the day-precision sighting wins; "2026-03" nests around it
    assert signal.occurred_on.precision is DatePrecision.DAY
    assert signal.primary is press
    assert signal.first_observed_at == SEEN - timedelta(days=2)
    assert not signal.date_conflict


def test_rescrape_of_same_source_id_merges_even_with_new_url():
    a = opening("a1")
    b = opening("a1", source_url="https://jobs.lever.example/northwind/a1?ref=feed")
    (signal,) = dedupe([a, b]).signals
    assert signal.match_reasons == [MatchReason.SAME_SOURCE_ID]
    assert signal.source_count == 2  # two URLs, though one posting


def test_two_openings_with_same_title_in_one_source_stay_two():
    result = dedupe([opening("a1"), opening("a2")])
    assert len(result.signals) == 2
    assert result.duplicates_removed == 0


def test_chain_through_third_source_cannot_join_two_ids_of_one_source():
    a1 = opening("a1")
    board = opening(
        None,
        source_url="https://board.example/jobs/9911",
        source_name="job_board",
        source_event_id="9911",
    )
    a2 = opening("a2")
    result = dedupe([a1, board, a2])
    ids = [sorted(str(p.source_event_id) for p in s.sightings) for s in result.signals]
    assert ids == [["9911", "a1"], ["a2"]]


def test_ids_from_different_sources_are_not_compared():
    a = opening("777")
    b = opening(
        "777",
        source_name="job_board",
        source_url="https://board.example/jobs/777",
        subject="Account Executive",
    )
    assert len(dedupe([a, b]).signals) == 2


def test_same_url_merges_and_url_noise_is_ignored():
    a = ev(subject=None, occurred_on=None, source_name="research_run_1")
    b = ev(
        subject=None,
        occurred_on=None,
        source_name="research_run_2",
        source_url="http://www.news.northwind.example/2026/03/series-a/#top",
    )
    (signal,) = dedupe([a, b]).signals
    assert signal.match_reasons == [MatchReason.SAME_URL]
    assert signal.occurred_on is None


@pytest.mark.parametrize(
    ("a_kw", "b_kw"),
    [
        ({"occurred_on": None}, {}),  # no date on one side: never merge on subject alone
        ({"subject": None}, {}),  # no subject: nothing to compare
        ({"subject": "Series B"}, {}),
        ({"occurred_on": "2025-03"}, {}),  # dates do not overlap
        ({"type": "expansion"}, {}),
        ({"company_domain": "contoso.example"}, {}),
    ],
)
def test_not_the_same_event(a_kw, b_kw):
    base_b = {
        "source_url": "https://db.fictive-data.example/rounds/x",
        "source_name": "funding_db",
    }
    result = dedupe([ev(**a_kw), ev(**{**base_b, **b_kw})])
    assert len(result.signals) == 2


def test_merge_by_id_with_disagreeing_dates_is_flagged():
    a = opening("a1", occurred_on="2026-09-01")
    b = opening("a1", occurred_on="2026-10-02")  # reposted: new date, same id
    (signal,) = dedupe([a, b]).signals
    assert signal.date_conflict
    assert [str(p.occurred_on) for p in signal.sightings] == ["2026-09-01", "2026-10-02"]


def test_checks_pass_through_and_never_cancel_found():
    not_found = SignalEvent(
        company_domain="northwind.example",
        type="funding",
        status="not_found_in_sample",
        observed_at=SEEN,
        sample="funding_db search, 2026-10-19",
    )
    unknown = SignalEvent(
        company_domain="northwind.example", type="funding", status="unknown", observed_at=SEEN
    )
    result = dedupe([not_found, ev(), unknown])
    assert result.checks == [not_found, unknown]
    assert len(result.signals) == 1
    assert result.input_found == 1


def test_order_is_stable_and_empty_input_is_fine():
    a, b = ev(), ev(type="expansion", subject="Lisbon office")
    assert [s.type for s in dedupe([a, b]).signals] == ["funding", "expansion"]
    empty = dedupe([])
    assert (empty.signals, empty.checks, empty.duplicates_removed) == ([], [], 0)


@pytest.mark.parametrize(
    ("raw", "norm"),
    [
        ("Series A", "series a"),
        ("  series-A ", "series a"),
        ("Série A", "serie a"),
        ("Sr. Data Engineer (Lisbon)", "sr data engineer lisbon"),
        ("---", None),
        (None, None),
    ],
)
def test_normalise_subject(raw, norm):
    assert normalise_subject(raw) == norm


def test_subject_placeholder_is_none_and_only_for_found():
    assert ev(subject="Unknown").subject is None
    with pytest.raises(ValidationError, match="only belong to found"):
        SignalEvent(
            company_domain="northwind.example",
            type="funding",
            status="unknown",
            observed_at=SEEN,
            subject="Series A",
        )
