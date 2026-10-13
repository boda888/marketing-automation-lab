import pytest
from deliverability_preflight.capacity import (
    CapacityConfig,
    Mailbox,
    mailbox_cap,
    plan_capacity,
)

CFG = CapacityConfig()


def boxes(n, age=0, domains=3):
    return [Mailbox(address=f"s{i}@d{i % domains}.example", age_days=age) for i in range(n)]


def codes(plan):
    return {f.code for f in plan.findings}


def test_ramp_is_monotonic_and_bounded():
    caps = [mailbox_cap(a, CFG) for a in range(0, 40)]
    assert caps[0] == 5 and caps[-1] == 30
    assert caps == sorted(caps)


def test_new_mailboxes_ramp_to_target():
    plan = plan_capacity(boxes(6), 150)
    assert plan.days[0].capacity == 30  # 6 x 5
    assert plan.steady_state_capacity == 180
    assert plan.day_target_reached is not None and plan.day_target_reached > 0
    assert "CAPACITY_RAMP_REQUIRED" in codes(plan)
    assert plan.additional_mailboxes_needed == 0


def test_warmed_mailboxes_hit_target_today():
    plan = plan_capacity(boxes(6, age=30), 150)
    assert plan.day_target_reached == 0
    assert plan.findings == []


def test_target_exceeds_capacity_reports_missing_mailboxes():
    plan = plan_capacity(boxes(3, age=30), 200)
    assert plan.steady_state_capacity == 90
    assert plan.additional_mailboxes_needed == 4  # ceil(110 / 30)
    assert "CAPACITY_TARGET_EXCEEDS_CAPACITY" in codes(plan)


def test_domain_cap_limits_capacity_and_flags_concentration():
    mbs = [Mailbox(address=f"s{i}@one.example", age_days=30) for i in range(5)]
    plan = plan_capacity(mbs, 100)
    assert plan.steady_state_capacity == 90  # domain cap, not 150
    assert "CAPACITY_TOO_MANY_MAILBOXES_PER_DOMAIN" in codes(plan)


def test_paused_zeroes_volume():
    plan = plan_capacity(boxes(6, age=30), 100, paused=True)
    assert all(d.capacity == 0 for d in plan.days)
    assert "CAPACITY_PAUSED" in codes(plan)


def test_no_mailboxes_and_horizon():
    assert "CAPACITY_NO_MAILBOXES" in codes(plan_capacity([], 10))
    plan = plan_capacity(boxes(6), 180, horizon_days=5)
    assert plan.day_target_reached is None
    assert "CAPACITY_TARGET_BEYOND_HORIZON" in codes(plan)


def test_validation():
    with pytest.raises(ValueError):
        CapacityConfig(start_per_mailbox=40)
    with pytest.raises(ValueError):
        plan_capacity([], -1)
