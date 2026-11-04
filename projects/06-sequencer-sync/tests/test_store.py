"""SqlitePushLog: the PushLog contract on disk, across restarts and concurrent writers."""

from __future__ import annotations

import threading

from sequencer import (
    BatchStatus,
    Campaign,
    DedupeScope,
    HttpResponse,
    PushState,
    SequencerLead,
    SkipReason,
    SmartleadAdapter,
    SqlitePushLog,
    execute_plan,
    plan_push,
    reconcile,
)
from sequencer.fakes import FakeTransport

CAMPAIGN = Campaign(campaign_id="101", name="Q4 Logistics DE")
LEADS = [
    SequencerLead(lead_id="l1", email="ana@northwind.example"),
    SequencerLead(lead_id="l2", email="ben@contoso.example"),
]


def plan(log):
    return plan_push(
        "smartlead",
        CAMPAIGN,
        LEADS,
        dedupe_scope=DedupeScope.CAMPAIGN,
        push_log=log,
        allow_fixtures=True,
    )


def test_contract():
    log = SqlitePushLog(":memory:")
    assert log.state("k") is None
    assert log.reserve("k") is True
    assert log.reserve("k") is False
    assert log.state("k") is PushState.RESERVED
    log.release("k")
    assert log.state("k") is None
    log.reserve("k")
    log.commit("k")
    log.release("k")  # never drops a committed key
    assert log.state("k") is PushState.COMMITTED
    assert log.reserve("k") is False
    log.commit("direct")  # reconcile may commit an unreserved key
    assert log.counts() == {"committed": 2}


def test_unknown_outcome_survives_restart_and_blocks_next_run(tmp_path):
    db = tmp_path / "push.sqlite"
    with SqlitePushLog(db) as log:
        report = execute_plan(
            plan(log), SmartleadAdapter("k"), FakeTransport([HttpResponse(502)]), log
        )
        assert report.batches[0].status is BatchStatus.OUTCOME_UNKNOWN

    with SqlitePushLog(db) as log:  # a new process after a crash
        again = plan(log)
        assert again.to_send == []
        assert {s.reason for s in again.skipped} == {SkipReason.PUSH_PENDING}
        assert len(log.pending()) == 2
        # read-back: only ana landed -> ana committed, ben released and pushable again
        reconcile(report, log, members_after=["ana@northwind.example"])
        third = plan(log)
        assert [p.lead.lead_id for p in third.to_send] == ["l2"]
        assert log.pending() == []


def test_two_writers_one_winner(tmp_path):
    db = tmp_path / "push.sqlite"
    SqlitePushLog(db).close()  # create schema once
    logs = [SqlitePushLog(db) for _ in range(8)]
    wins: list[bool] = []
    barrier = threading.Barrier(len(logs))

    def race(log):
        barrier.wait()
        wins.append(log.reserve("same-key"))

    threads = [threading.Thread(target=race, args=(log,)) for log in logs]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(wins) == [False] * 7 + [True]
    for log in logs:
        log.close()


def test_pending_older_than():
    now = [1000.0]
    log = SqlitePushLog(":memory:", clock=lambda: now[0])
    log.reserve("old")
    now[0] = 4000.0
    log.reserve("new")
    old = log.pending(older_than=3600)
    assert [p.key for p in old] == []
    now[0] = 4601.0
    old = log.pending(older_than=3600)
    assert [(p.key, p.age_seconds) for p in old] == [("old", 3601.0)]
    assert [p.key for p in log.pending()] == ["old", "new"]


def test_no_person_data_stored(tmp_path):
    db = tmp_path / "push.sqlite"
    with SqlitePushLog(db) as log:
        execute_plan(plan(log), SmartleadAdapter("k"), FakeTransport(), log)
    raw = b"".join(p.read_bytes() for p in tmp_path.iterdir())
    assert b"northwind" not in raw and b"contoso" not in raw
