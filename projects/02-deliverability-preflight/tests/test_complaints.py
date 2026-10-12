import pytest
from deliverability_preflight import ComplaintStats, check_complaint_rate
from pydantic import ValidationError


def check(delivered, complaints, **kw):
    return check_complaint_rate(ComplaintStats(delivered=delivered, complaints=complaints), **kw)


def codes(rep):
    return {f.code for f in rep.findings}


def test_under_target_is_ok():
    rep = check(20_000, 10)  # 0.05%
    assert rep.status == "ok" and rep.findings == [] and rep.rate == pytest.approx(0.0005)
    assert rep.complaints_until_limit == 49  # 59 total stays under 0.3% of 20,000


def test_over_target_watch():
    rep = check(10_000, 15)  # 0.15%
    assert rep.status == "watch" and codes(rep) == {"COMPLAINT_RATE_OVER_TARGET"}


def test_exactly_target_is_watch_and_exactly_limit_is_stop():
    assert check(10_000, 10).status == "watch"
    rep = check(10_000, 30)
    assert rep.status == "stop" and codes(rep) == {"COMPLAINT_RATE_OVER_LIMIT"}
    assert rep.complaints_until_limit == 0


def test_one_complaint_on_tiny_sample_does_not_stop():
    rep = check(50, 1)  # 2% but on 50 messages
    assert rep.status == "watch" and codes(rep) == {"COMPLAINT_RATE_SMALL_SAMPLE"}
    assert not rep.sufficient_sample


def test_small_sample_with_too_many_complaints_still_stops():
    rep = check(200, 5)  # 5 complaints would breach 0.3% even of 1,000 sends
    assert rep.status == "stop"


def test_small_clean_sample_is_info_only():
    rep = check(300, 0)
    assert rep.status == "ok" and codes(rep) == {"COMPLAINT_SMALL_SAMPLE"}


def test_no_deliveries():
    rep = check(0, 0)
    assert rep.rate is None and rep.complaints_until_limit is None
    assert rep.status == "ok" and codes(rep) == {"COMPLAINT_NO_DATA"}


def test_custom_thresholds():
    rep = check(10_000, 15, target=0.002, hard_limit=0.005)
    assert rep.status == "ok"
    with pytest.raises(ValueError):
        check(10_000, 15, target=0.005, hard_limit=0.002)


@pytest.mark.parametrize(
    "kw", [{"delivered": -1, "complaints": 0}, {"delivered": 5, "complaints": 6}]
)
def test_invalid_stats(kw):
    with pytest.raises(ValidationError):
        ComplaintStats(**kw)
