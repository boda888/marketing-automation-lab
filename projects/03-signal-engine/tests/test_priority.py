from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError
from signal_engine import (
    DEFAULT_WINDOWS,
    Band,
    SignalEvent,
    SignalType,
    Tier,
    Window,
    dedupe,
    prioritise,
    prioritise_events,
)
from signal_engine.priority import BEST_SIGNAL_MAX_WORDS, REASON_MAX_WORDS

AS_OF = date(2026, 10, 19)
SEEN = datetime(2026, 10, 19, 9, 0, tzinfo=UTC)
DOMAIN = "northwind.example"


def ev(type_, occurred_on, n=0, **kw):
    base = {
        "company_domain": DOMAIN,
        "type": type_,
        "status": "found",
        "observed_at": SEEN,
        "source_url": f"https://news.northwind.example/{type_}/{n}",
        "evidence": f"Evidence for {type_} {n}.",
        "occurred_on": occurred_on,
    }
    base.update(kw)
    return SignalEvent(**base)


def check(type_, status, **kw):
    base = {"company_domain": DOMAIN, "type": type_, "status": status, "observed_at": SEEN}
    if status == "not_found_in_sample":
        base["sample"] = "careers page, 2026-10-19"
    base.update(kw)
    return SignalEvent(**base)


def tier_of(*events):
    (account,) = prioritise_events(list(events), AS_OF)
    return account


# --- windows ----------------------------------------------------------------


def test_window_bands_and_ordering():
    w = Window(strong_days=365, medium_days=730)
    assert [w.band(d) for d in (0, 365, 366, 730, 731)] == [
        Band.STRONG,
        Band.STRONG,
        Band.MEDIUM,
        Band.MEDIUM,
        Band.NONE,
    ]
    assert Window(medium_days=180).band(0) is Band.MEDIUM  # capped at medium
    with pytest.raises(ValidationError):
        Window(strong_days=400, medium_days=365)


def test_tech_change_is_never_strong_by_default():
    assert DEFAULT_WINDOWS[SignalType.TECH_CHANGE].band(0) is Band.MEDIUM


# --- the arithmetic the LLM got wrong ----------------------------------------


def test_funding_day_dated_inside_12_months_is_tier_2():
    account = tier_of(ev("funding", "2026-03-14", subject="Series A"))
    assert account.tier is Tier.TIER_2
    assert not account.upgrade_possible
    assert account.best_signal.startswith(
        "funding: Series A (2026-03-14), 219 days old, strong (<=365d)"
    )


def test_year_precision_straddling_the_12_month_edge_is_not_strong():
    # "2025" is 292..656 days old on 2026-10-19: maybe <=12 months, maybe not
    account = tier_of(ev("funding", "2025"))
    (a,) = account.assessments
    assert (a.best, a.worst) == (Band.STRONG, Band.MEDIUM)
    assert a.pending_reason == "date straddles a window edge"
    assert account.tier is Tier.TIER_3  # worst case stands
    assert account.best_case_tier is Tier.TIER_2
    assert account.upgrade_possible
    assert "Could reach Tier 2" in account.reason


def test_month_precision_fully_inside_window_is_settled():
    # "2025-11" is 323..352 days old: wholly inside 365
    account = tier_of(ev("funding", "2025-11"))
    (a,) = account.assessments
    assert a.settled and a.worst is Band.STRONG
    assert "323-352 days old" in account.best_signal


def test_old_signal_only_is_hold():
    account = tier_of(ev("funding", "2023-01-10"))
    assert account.tier is Tier.HOLD
    assert account.best_signal is None
    assert account.reason.startswith("Hold: no settled signal")


# --- tiers -----------------------------------------------------------------


def test_strong_plus_another_type_is_tier_1():
    account = tier_of(ev("funding", "2026-03-14"), ev("hiring", "2026-10-01"))
    assert account.tier is Tier.TIER_1
    assert account.reason.startswith("Tier 1: 2 strong, 0 medium settled")


def test_two_strong_of_the_same_type_is_still_tier_2():
    account = tier_of(ev("hiring", "2026-10-01", 1), ev("hiring", "2026-10-05", 2))
    assert account.tier is Tier.TIER_2


def test_medium_only_is_tier_3():
    assert tier_of(ev("tech_change", "2026-09-01")).tier is Tier.TIER_3


def test_hold_with_pending_upgrade_becomes_review():
    account = tier_of(ev("hiring", "2026-03"), ev("funding", None, subject="Seed"))
    assert account.worst_case_tier is Tier.HOLD
    assert account.best_case_tier is Tier.TIER_2
    assert account.tier is Tier.REVIEW
    assert "funding undated" in account.reason


def test_unknown_check_is_pending_but_not_found_is_settled():
    unknown = tier_of(check("funding", "unknown"))
    assert unknown.tier is Tier.REVIEW
    assert "funding check result unknown" in unknown.reason
    not_found = tier_of(check("funding", "not_found_in_sample"))
    assert not_found.tier is Tier.HOLD
    assert not not_found.upgrade_possible


def test_unknown_check_on_a_tier_2_account_keeps_tier_and_flags_upgrade():
    account = tier_of(ev("funding", "2026-03-14"), check("hiring", "unknown"))
    assert account.tier is Tier.TIER_2
    assert account.upgrade_possible and account.best_case_tier is Tier.TIER_1


def test_unknown_check_is_ignored_when_a_found_signal_of_that_type_exists():
    account = tier_of(ev("hiring", "2026-10-01"), check("hiring", "unknown"))
    assert len(account.assessments) == 1


# --- dedupe feeds priority ---------------------------------------------------


def test_duplicate_round_from_two_sources_counts_once():
    press = ev("funding", "2026-03-14", 1, subject="Series A")
    db = ev("funding", "2026-03", 2, subject="series a", source_name="funding_db")  # same round
    account = tier_of(press, db)
    assert len(account.assessments) == 1
    assert account.tier is Tier.TIER_2  # not Tier 1 from double counting


def test_date_conflict_uses_span_of_all_sightings():
    a = ev("hiring", "2026-10-01", source_name="lever", source_event_id="j1")
    b = ev("hiring", "2026-04-01", 9, source_name="lever", source_event_id="j1")
    (signal,) = dedupe([a, b]).signals
    assert signal.date_conflict
    account = prioritise(DOMAIN, [signal], AS_OF)
    (assessment,) = account.assessments
    assert assessment.pending_reason == "dates disagree"
    assert account.tier is Tier.REVIEW


def test_accounts_are_kept_apart_in_first_seen_order():
    other = ev("funding", "2026-03-14", company_domain="contoso.example")
    accounts = prioritise_events([other, ev("hiring", "2026-10-01")], AS_OF)
    assert [a.company_domain for a in accounts] == ["contoso.example", DOMAIN]
    assert all(len(a.assessments) == 1 for a in accounts)


def test_custom_windows():
    windows = {**DEFAULT_WINDOWS, SignalType.FUNDING: Window(strong_days=90, medium_days=180)}
    (account,) = prioritise_events([ev("funding", "2026-03-14")], AS_OF, windows)
    assert account.tier is Tier.HOLD


def test_text_fields_respect_word_caps():
    long_subject = " ".join(["word"] * 35)  # 174 chars, under the 200 cap
    events = [ev(t, "2025", i, subject=long_subject) for i, t in enumerate(SignalType)]
    account = tier_of(*events, check("funding", "unknown"))
    assert len(account.reason.split()) <= REASON_MAX_WORDS
    assert len(account.best_signal.split()) <= BEST_SIGNAL_MAX_WORDS
