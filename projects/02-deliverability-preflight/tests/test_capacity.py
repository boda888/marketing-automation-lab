from datetime import date, timedelta

import pytest
from deliverability_preflight import CapacityPolicy, Mailbox, mailbox_capacity, plan_capacity
from pydantic import ValidationError

START = date(2026, 9, 1)
DAY0 = START + timedelta(days=14)  # first campaign day with the default 14-day warmup


def mb(address, started=START, **kw):
    return Mailbox(address=address, warmup_started=started, **kw)


def codes(plan):
    return {f.code for f in plan.findings}


def test_warmup_then_ramp_then_steady():
    box = mb("sam@outreach-acme.example")
    assert mailbox_capacity(box, START - timedelta(days=1)).phase == "not_started"
    warm = mailbox_capacity(box, DAY0 - timedelta(days=1))
    assert (warm.phase, warm.capacity) == ("warmup", 0)
    first = mailbox_capacity(box, DAY0)
    assert (first.phase, first.capacity) == ("ramp", 5)
    assert mailbox_capacity(box, DAY0 + timedelta(days=3)).capacity == 11
    # 5 + 2*13 = 31 -> capped at 30 on campaign day 13
    assert mailbox_capacity(box, DAY0 + timedelta(days=12)).capacity == 29
    steady = mailbox_capacity(box, DAY0 + timedelta(days=13))
    assert (steady.phase, steady.capacity) == ("steady", 30)
    assert steady.steady_from == DAY0 + timedelta(days=13)


def test_complaint_status_holds_or_pauses():
    later = DAY0 + timedelta(days=30)
    held = mailbox_capacity(mb("a@acme-mail.example", complaint_status="watch"), later)
    assert (held.phase, held.capacity) == ("held", 5)
    paused = mailbox_capacity(mb("a@acme-mail.example", complaint_status="stop"), later)
    assert (paused.phase, paused.capacity) == ("paused", 0)
    # a mailbox still in warmup stays in warmup when its domain is on watch
    assert mailbox_capacity(mb("a@acme-mail.example", complaint_status="watch"), START).phase == (
        "warmup"
    )


def test_per_mailbox_cap_override():
    box = mb("lee@acme-mail.example", daily_cap=12)
    assert mailbox_capacity(box, DAY0 + timedelta(days=40)).capacity == 12


def test_plan_reaches_target_while_ramping():
    boxes = [mb(f"rep{i}@acme-mail.example") for i in range(3)]
    plan = plan_capacity(boxes, DAY0, target_daily=60)
    assert plan.capacity_today == 15 and plan.steady_state_capacity == 90
    # 3 boxes x (5 + 2d) >= 60 -> d = 8
    assert plan.target_reached_on == DAY0 + timedelta(days=8)
    assert plan.additional_mailboxes == 0 and plan.additional_domains == 0
    assert codes(plan) == {"CAPACITY_RAMPING"}
    assert plan.by_domain == {"acme-mail.example": 15}
    assert plan.schedule[0].day == DAY0 and len(plan.schedule) == 30


def test_plan_target_already_met_has_no_findings():
    boxes = [mb("a@acme-mail.example"), mb("b@acme-mail.example")]
    plan = plan_capacity(boxes, DAY0 + timedelta(days=60), target_daily=50)
    assert plan.capacity_today == 60 and plan.target_reached_on == plan.on
    assert plan.findings == []


def test_shortfall_counts_mailboxes_and_domains():
    boxes = [mb(f"r{i}@acme-mail.example") for i in range(2)]
    plan = plan_capacity(boxes, DAY0 + timedelta(days=60), target_daily=300)
    assert plan.steady_state_capacity == 60 and plan.target_reached_on is None
    assert plan.mailboxes_needed == 10
    # 240 short -> 8 more mailboxes; 1 free slot on acme-mail.example -> 7 on new domains -> 3
    assert (plan.additional_mailboxes, plan.additional_domains) == (8, 3)
    assert "CAPACITY_SHORTFALL" in codes(plan)


def test_target_beyond_horizon():
    plan = plan_capacity([mb("a@acme-mail.example")], DAY0, target_daily=30, days=5)
    assert plan.target_reached_on is None and codes(plan) == {"CAPACITY_TARGET_AFTER_HORIZON"}


def test_crowded_domain_duplicate_and_warming_findings():
    boxes = [mb(f"r{i}@acme-mail.example") for i in range(4)]
    boxes.append(mb("R0@acme-mail.example"))
    boxes.append(mb("new@acme-reach.example", started=DAY0))
    plan = plan_capacity(boxes, DAY0, target_daily=10)
    found = codes(plan)
    assert {"CAPACITY_DOMAIN_CROWDED", "CAPACITY_DUPLICATE_MAILBOX"} <= found
    assert "CAPACITY_MAILBOX_WARMING" in found
    assert len(plan.mailboxes) == 5


def test_paused_and_held_mailboxes_reported_and_excluded():
    later = DAY0 + timedelta(days=60)
    boxes = [
        mb("a@acme-mail.example", complaint_status="stop"),
        mb("b@acme-mail.example", complaint_status="watch"),
        mb("c@acme-mail.example"),
    ]
    plan = plan_capacity(boxes, later, target_daily=35)
    assert plan.capacity_today == 35 and plan.steady_state_capacity == 35
    assert {"CAPACITY_MAILBOX_PAUSED", "CAPACITY_MAILBOX_HELD"} <= codes(plan)


def test_cap_at_provider_limit_is_error():
    box = mb("bulk@acme-mail.example", daily_cap=2000)
    plan = plan_capacity([box], DAY0 + timedelta(days=60), target_daily=10)
    assert "CAPACITY_CAP_AT_PROVIDER_LIMIT" in codes(plan)
    smtp = mb("bulk@acme-mail.example", provider="smtp", daily_cap=2000)
    assert "CAPACITY_CAP_AT_PROVIDER_LIMIT" not in codes(
        plan_capacity([smtp], DAY0 + timedelta(days=60), target_daily=10)
    )


def test_policy_and_input_validation():
    with pytest.raises(ValidationError):
        CapacityPolicy(ramp_start=40, campaign_cap=30)
    with pytest.raises(ValidationError):
        Mailbox(address="not-an-address", warmup_started=START)
    with pytest.raises(ValueError):
        plan_capacity([], DAY0, target_daily=-1)


def test_empty_fleet_with_custom_policy():
    policy = CapacityPolicy(campaign_cap=40, max_mailboxes_per_domain=2)
    plan = plan_capacity([], DAY0, target_daily=200, policy=policy)
    assert plan.capacity_today == 0 and plan.mailboxes_needed == 5
    assert (plan.additional_mailboxes, plan.additional_domains) == (5, 3)
