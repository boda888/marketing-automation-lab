from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError
from signal_engine import (
    DEFAULT_WINDOWS,
    Band,
    HeadcountSeries,
    HeadcountState,
    HeadcountThresholds,
    SignalStatus,
    SignalType,
    Tier,
    assess_headcount,
    dedupe,
    prioritise_events,
)

SEEN = datetime(2026, 10, 20, 7, 0, tzinfo=UTC)
DOMAIN = "northwind.example"


def series(counts, **kw):
    base = {
        "company_domain": DOMAIN,
        "source_name": "people_data_fixture",
        "source_url": "https://data.provider.example/companies/northwind/headcount",
        "observed_at": SEEN,
        "counts": counts,
    }
    base.update(kw)
    return HeadcountSeries(**base)


def states(trend):
    return {g.months: g.state for g in trend.growth}


# --- arithmetic and thresholds ------------------------------------------------


def test_strong_growth_over_six_and_twelve_months():
    trend = assess_headcount(series({"2025-10": 40, "2026-04": 50, "2026-10": 62}))
    assert trend.state is HeadcountState.STRONG_GROWTH
    six, twelve = trend.growth
    assert (six.base_month, six.latest_month, six.pct) == ("2026-04", "2026-10", 24.0)
    assert (twelve.base_month, twelve.pct) == ("2025-10", 55.0)
    assert trend.latest_month == "2026-10" and trend.latest_count == 62


@pytest.mark.parametrize(
    ("base", "latest", "state"),
    [
        (50, 60, HeadcountState.STRONG_GROWTH),  # exactly +20%: strong, no float drift
        (3, 4, HeadcountState.STRONG_GROWTH),  # +33.3%
        (100, 119, HeadcountState.STEADY),
        (100, 91, HeadcountState.STEADY),
        (100, 90, HeadcountState.CONTRACTION),  # exactly -10%
        (70, 63, HeadcountState.CONTRACTION),
    ],
)
def test_threshold_edges_are_exact(base, latest, state):
    trend = assess_headcount(series({"2026-04": base, "2026-10": latest}), horizons=(6,))
    assert trend.state is state


def test_custom_thresholds_and_horizons():
    s = series({"2026-07": 100, "2026-10": 112})
    assert assess_headcount(s, horizons=(3,)).state is HeadcountState.STEADY
    loose = HeadcountThresholds(strong_growth_pct=10)
    assert assess_headcount(s, loose, horizons=(3,)).state is HeadcountState.STRONG_GROWTH
    with pytest.raises(ValueError, match="horizons"):
        assess_headcount(s, horizons=(0,))
    with pytest.raises(ValidationError):
        HeadcountThresholds(contraction_pct=5)


def test_twelve_point_one_percent_rounds_for_display_only():
    trend = assess_headcount(series({"2026-04": 33, "2026-10": 37}), horizons=(6,))
    assert trend.growth[0].pct == 12.1


# --- missing data is unknown, never 0 ----------------------------------------


def test_missing_base_month_is_unknown_not_zero_growth():
    trend = assess_headcount(series({"2026-04": 50, "2026-10": 52}))
    assert states(trend) == {6: HeadcountState.STEADY, 12: HeadcountState.UNKNOWN}
    twelve = trend.growth[1]
    assert twelve.pct is None and twelve.base is None
    assert twelve.unknown_reason == "base month missing"
    # 12 months could still be strong: overall unknown, not "steady"
    assert trend.state is HeadcountState.UNKNOWN


def test_none_count_and_zero_base_are_unknown():
    trend = assess_headcount(series({"2025-10": 0, "2026-04": None, "2026-10": 12}))
    six, twelve = trend.growth
    assert six.unknown_reason == "base month not reported"
    assert twelve.unknown_reason == "base count is 0"
    assert trend.state is HeadcountState.UNKNOWN


def test_neighbouring_month_is_never_used_as_base():
    trend = assess_headcount(series({"2026-03": 10, "2026-05": 10, "2026-10": 50}))
    assert trend.growth[0].state is HeadcountState.UNKNOWN


def test_latest_month_is_the_latest_reported_one():
    trend = assess_headcount(series({"2026-03": 40, "2026-09": 50, "2026-10": None}))
    assert trend.latest_month == "2026-09"
    assert trend.growth[0].pct == 25.0


def test_no_reported_count_at_all():
    trend = assess_headcount(series({"2026-10": None}))
    assert trend.state is HeadcountState.UNKNOWN and trend.growth == []
    assert trend.to_event().status is SignalStatus.UNKNOWN


# --- contraction and mixed ----------------------------------------------------


def test_contraction_is_its_own_state():
    trend = assess_headcount(series({"2025-10": 100, "2026-04": 95, "2026-10": 80}))
    assert trend.state is HeadcountState.CONTRACTION
    assert "-20.0% over 12 months" in trend.reason


def test_contraction_wins_over_an_unknown_horizon():
    trend = assess_headcount(series({"2026-04": 100, "2026-10": 80}))
    assert trend.state is HeadcountState.CONTRACTION


def test_strong_and_contraction_together_is_mixed():
    # +25% over 12 months, -16.7% over the last 6
    trend = assess_headcount(series({"2025-10": 80, "2026-04": 120, "2026-10": 100}))
    assert states(trend) == {6: HeadcountState.CONTRACTION, 12: HeadcountState.STRONG_GROWTH}
    assert trend.state is HeadcountState.MIXED
    assert trend.to_event().status is SignalStatus.UNKNOWN


# --- series validation -------------------------------------------------------


@pytest.mark.parametrize(
    ("counts", "match"),
    [
        ({"2026-10-01": 5}, "YYYY-MM"),
        ({"2026": 5}, "YYYY-MM"),
        ({"Q3 2026": 5}, "ISO 8601"),
        ({"2026-10": -1}, "negative"),
        ({"2026-11": 5}, "after observed_at"),
    ],
)
def test_series_rejects_bad_months_and_counts(counts, match):
    with pytest.raises(ValidationError, match=match):
        series(counts)


def test_series_rejects_naive_time_and_bad_url():
    with pytest.raises(ValidationError, match="timezone-aware"):
        series({"2026-10": 5}, observed_at=datetime(2026, 10, 20))
    with pytest.raises(ValidationError, match="http"):
        series({"2026-10": 5}, source_url="data.provider.example/x")


# --- to_event -----------------------------------------------------------------


def test_strong_growth_becomes_a_found_event_with_the_figures():
    event = assess_headcount(series({"2025-10": 40, "2026-04": 50, "2026-10": 62})).to_event()
    assert event.status is SignalStatus.FOUND
    assert event.type is SignalType.HEADCOUNT_GROWTH
    assert str(event.occurred_on) == "2026-10"
    assert event.subject == "headcount"
    assert "50 (2026-04) -> 62 (2026-10): +24.0% over 6 months" in event.evidence
    assert event.is_fixture


def test_steady_and_contraction_become_not_found_with_the_sample():
    steady = assess_headcount(series({"2025-10": 50, "2026-04": 51, "2026-10": 52})).to_event()
    assert steady.status is SignalStatus.NOT_FOUND_IN_SAMPLE
    assert "people_data_fixture" in steady.sample and "+4.0% over 12 months" in steady.sample
    shrink = assess_headcount(series({"2026-04": 100, "2026-10": 80})).to_event()
    assert shrink.status is SignalStatus.NOT_FOUND_IN_SAMPLE
    assert "contraction" in shrink.sample


# --- into dedupe + priority ----------------------------------------------------


def test_default_window_dates_by_latest_month():
    w = DEFAULT_WINDOWS[SignalType.HEADCOUNT_GROWTH]
    assert (w.band(90), w.band(91), w.band(181)) == (Band.STRONG, Band.MEDIUM, Band.NONE)


def test_two_sources_reporting_the_same_month_merge_into_one_signal():
    a = assess_headcount(series({"2026-04": 50, "2026-10": 62})).to_event()
    b = assess_headcount(
        series(
            {"2026-04": 48, "2026-10": 60},
            source_name="second_provider_fixture",
            source_url="https://other.provider.example/org/northwind",
        )
    ).to_event()
    result = dedupe([a, b])
    assert result.duplicates_removed == 1
    assert result.signals[0].source_count == 2


def test_recent_strong_growth_tiers_like_any_strong_signal():
    event = assess_headcount(series({"2026-03": 40, "2026-09": 50})).to_event()
    (account,) = prioritise_events([event], date(2026, 10, 20))
    assert account.tier is Tier.TIER_2
    assert account.best_signal.startswith("headcount growth: headcount (2026-09)")


def test_unknown_headcount_alone_goes_to_review():
    event = assess_headcount(series({"2026-10": 52})).to_event()
    (account,) = prioritise_events([event], date(2026, 10, 20))
    assert account.tier is Tier.REVIEW and account.upgrade_possible
