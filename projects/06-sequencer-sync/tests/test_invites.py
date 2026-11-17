from datetime import UTC, datetime, timedelta

import pytest
from sequencer import SenderAccount, SequencerLead, assign_senders
from sequencer.invites import InviteLedger, InvitePolicy, InviteRecord, InviteStatus

NOW = datetime(2026, 11, 17, 9, 0, tzinfo=UTC)
ANA = "https://www.linkedin.com/in/ana-lopez-example"
BEN = "https://www.linkedin.com/in/ben-okafor-example"
CAL = "https://www.linkedin.com/in/cal-nguyen-example"


def sent(sender, url, days_ago, **kw):
    return InviteRecord(sender, url, NOW - timedelta(days=days_ago), **kw)


def lead(i, url):
    return SequencerLead(lead_id=f"l{i}", email=None, linkedin_url=url)


def test_rolling_window_counts_withdrawn_sends_and_drops_old_ones():
    ledger = InviteLedger(
        [
            sent(1, ANA, 1),
            sent(1, BEN, 6.9),
            sent(
                1,
                CAL,
                3,
                status=InviteStatus.WITHDRAWN,
                withdrawn_at=NOW - timedelta(days=1),
            ),
            sent(1, "linkedin.com/in/dee-example/", 7.5),  # outside the 7-day window
            sent(2, ANA, 0.5),
        ]
    )
    assert ledger.sent_in_window(1, NOW) == 3  # the withdrawal did not refund the send
    assert ledger.sent_in_window(2, NOW) == 1
    # Monday is not special: a send ages out exactly window_days after it went out.
    assert ledger.sent_in_window(1, NOW + timedelta(days=0.2)) == 2


def test_apply_sets_usage_and_a_limit_error_fills_the_sender_for_the_cooldown():
    senders = [SenderAccount(1, "Ava", 100), SenderAccount(2, "Bo", 100, used_this_week=99)]
    ledger = InviteLedger(
        [sent(1, ANA, 2), sent(2, BEN, 2)], limit_errors={1: NOW - timedelta(days=2)}
    )
    applied = ledger.apply(senders, NOW)
    assert [s.remaining for s in applied] == [0, 99]  # stale manual counter replaced
    assert ledger.full_until(1, NOW) == NOW + timedelta(days=5)
    later = ledger.apply(senders, NOW + timedelta(days=5, minutes=1))
    assert later[0].remaining == 100 - 0  # both sends have aged out by then
    # The held lead waits for its sender instead of failing or moving to another one.
    out = assign_senders([lead(1, CAL)], applied, owners={"l1": 1})
    assert [h.reason.value for h in out.held] == ["owner_over_budget"]


def test_later_limit_error_wins_and_naive_times_are_refused():
    ledger = InviteLedger()
    ledger.record_limit_error(1, NOW)
    ledger.record_limit_error(1, NOW - timedelta(days=3))
    assert ledger.full_until(1, NOW) == NOW + timedelta(days=7)
    with pytest.raises(ValueError):
        ledger.record_limit_error(1, datetime(2026, 11, 17, 9, 0))
    with pytest.raises(ValueError):
        InviteRecord(1, ANA, datetime(2026, 11, 1))
    with pytest.raises(ValueError):
        ledger.sent_in_window(1, datetime(2026, 11, 17))


def test_sweep_plans_stale_pending_only_oldest_first_and_flags_missing_ids():
    ledger = InviteLedger(
        [
            sent(1, ANA, 25, invite_id="inv-a"),
            sent(2, BEN, 30, invite_id="inv-b"),
            sent(1, CAL, 22),  # stale but no vendor id: cannot be cancelled
            sent(1, "https://www.linkedin.com/in/dee-example", 5, invite_id="inv-d"),
            sent(1, "https://www.linkedin.com/in/eve-example", 40, status=InviteStatus.ACCEPTED),
        ]
    )
    plan = ledger.plan_sweep(NOW)
    assert [(w.record.invite_id, w.age_days) for w in plan.withdraw] == [
        ("inv-b", 30),
        ("inv-a", 25),
    ]
    assert [r.profile_url for r in plan.no_invite_id] == [CAL]
    assert plan.blocked_until == {BEN: NOW + timedelta(days=21), ANA: NOW + timedelta(days=21)}
    assert [r.status for r in ledger.records].count(InviteStatus.PENDING) == 4  # planning only


def test_withdrawal_blocks_reinvite_then_expires():
    ledger = InviteLedger([sent(1, ANA, 25, invite_id="inv-a")])
    assert ledger.reinvite_blocked_until(ANA, NOW) is not None  # pending: never re-invite
    ledger.withdrawn("inv-a", NOW)
    assert ledger.reinvite_blocked_until("http://linkedin.com/in/Ana-Lopez-Example/", NOW) == (
        NOW + timedelta(days=21)
    )
    assert ledger.blocked_profiles(NOW) == {ANA}
    assert ledger.reinvite_blocked_until(ANA, NOW + timedelta(days=21, seconds=1)) is None
    assert ledger.blocked_profiles(NOW + timedelta(days=22)) == set()
    with pytest.raises(ValueError):
        ledger.withdrawn("inv-a", NOW)  # already withdrawn
    with pytest.raises(KeyError):
        ledger.withdrawn("inv-zzz", NOW)


def test_blocked_profiles_feed_assign_senders_as_held():
    ledger = InviteLedger([sent(1, ANA, 2), sent(1, BEN, 30, invite_id="inv-b")])
    ledger.withdrawn("inv-b", NOW - timedelta(days=1))
    senders = ledger.apply([SenderAccount(1, "Ava", 50)], NOW)
    out = assign_senders(
        [lead(1, ANA), lead(2, BEN), lead(3, CAL)],
        senders,
        in_campaign=ledger.blocked_profiles(NOW),
    )
    assert [p.lead.lead_id for p in out.pairs] == ["l3"]
    assert sorted(h.lead.lead_id for h in out.held) == ["l1", "l2"]


def test_records_and_policy_are_validated():
    with pytest.raises(ValueError):
        InviteLedger([InviteRecord(1, "https://www.linkedin.com/company/acme-example", NOW)])
    with pytest.raises(ValueError):
        InviteRecord(1, ANA, NOW, status=InviteStatus.WITHDRAWN)  # no withdrawn_at
    with pytest.raises(ValueError):
        InvitePolicy(window_days=0)
    custom = InviteLedger(
        [sent(1, ANA, 10, invite_id="x")], policy=InvitePolicy(stale_after_days=7)
    )
    assert [w.record.invite_id for w in custom.plan_sweep(NOW).withdraw] == ["x"]
