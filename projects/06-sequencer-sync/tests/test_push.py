import pytest
from sequencer import (
    BatchStatus,
    Campaign,
    DedupeScope,
    HttpResponse,
    InMemoryPushLog,
    InstantlyAdapter,
    PushState,
    SequencerLead,
    SmartleadAdapter,
    TransportError,
    execute_plan,
    plan_push,
    reconcile,
    retry_after_seconds,
)
from sequencer.fakes import FakeTransport

CAMPAIGN = Campaign(campaign_id="101", name="Q4 Logistics DE")


def make_plan(vendor, n, log, start=0):
    leads = [SequencerLead(lead_id=f"l{i}", email=f"p{i}@acme.example") for i in range(start, n)]
    return plan_push(
        vendor,
        CAMPAIGN,
        leads,
        dedupe_scope=DedupeScope.ALL_CAMPAIGNS,
        push_log=log,
        allow_fixtures=True,
    )


def states(log):
    return sorted(s.value for s in log.states.values())


def test_accepted_batches_commit_keys_and_second_run_sends_nothing():
    log, transport = InMemoryPushLog(), FakeTransport()
    report = execute_plan(make_plan("smartlead", 3, log), SmartleadAdapter("k"), transport, log)
    assert report.sent == 3 and report.failed == 0 and report.unknown == 0
    assert states(log) == ["committed"] * 3
    again = make_plan("smartlead", 3, log)
    assert again.to_send == [] and again.skipped_by_reason == {"already_pushed": 3}


def test_adapter_vendor_must_match_plan():
    log = InMemoryPushLog()
    with pytest.raises(ValueError, match="instantly"):
        execute_plan(make_plan("smartlead", 1, log), InstantlyAdapter("k"), FakeTransport(), log)


def test_429_retried_with_retry_after_then_accepted():
    log, waits = InMemoryPushLog(), []
    transport = FakeTransport([HttpResponse(429, headers={"Retry-After": "7"})])
    report = execute_plan(
        make_plan("smartlead", 2, log), SmartleadAdapter("k"), transport, log, sleep=waits.append
    )
    assert waits == [7.0]
    assert report.batches[0].status is BatchStatus.ACCEPTED
    assert report.batches[0].attempts == 2
    assert len(transport.requests) == 2


def test_429_exhausted_releases_keys_and_wait_is_capped():
    log, waits = InMemoryPushLog(), []
    transport = FakeTransport([HttpResponse(429, headers={"retry-after": "900"})] * 3)
    report = execute_plan(
        make_plan("smartlead", 2, log),
        SmartleadAdapter("k"),
        transport,
        log,
        sleep=waits.append,
        max_rate_limit_retries=2,
        max_wait_seconds=30,
    )
    assert waits == [30, 30]
    assert report.batches[0].status is BatchStatus.RATE_LIMITED
    assert report.failed == 2 and log.states == {}


@pytest.mark.parametrize(
    "failure",
    [
        HttpResponse(502, {"message": "bad gateway"}),
        TimeoutError("read timed out"),
        TransportError("connection reset"),
    ],
)
def test_5xx_or_timeout_is_never_retried_and_stays_reserved(failure):
    log = InMemoryPushLog()
    transport = FakeTransport([failure])
    report = execute_plan(make_plan("smartlead", 2, log), SmartleadAdapter("k"), transport, log)
    assert len(transport.requests) == 1
    assert report.batches[0].status is BatchStatus.OUTCOME_UNKNOWN
    assert report.unknown == 2
    assert states(log) == ["reserved", "reserved"]
    nxt = make_plan("smartlead", 2, log)
    assert nxt.skipped_by_reason == {"push_pending": 2}


def test_not_sent_and_4xx_release_keys_with_error_text():
    log = InMemoryPushLog()
    transport = FakeTransport(
        [TransportError("dns failure", reached_vendor=False), HttpResponse(422, {"error": "bad"})]
    )
    adapter = SmartleadAdapter("k", max_batch=1)
    report = execute_plan(make_plan("smartlead", 2, log), adapter, transport, log)
    assert [b.status for b in report.batches] == [BatchStatus.NOT_SENT, BatchStatus.REJECTED]
    assert report.batches[1].error == "bad" and report.batches[1].http_status == 422
    assert log.states == {}


def test_concurrent_reservation_keeps_lead_out():
    log = InMemoryPushLog()
    plan = make_plan("smartlead", 2, log)
    log.reserve(plan.to_send[0].key)  # another run got there after planning
    transport = FakeTransport()
    report = execute_plan(plan, SmartleadAdapter("k"), transport, log)
    assert report.sent == 1 and report.skipped_before_send == {"push_pending": 1}
    assert len(transport.requests[0].leads) == 1


def test_reconcile_counts_vendor_skips_and_settles_unknowns():
    log = InMemoryPushLog()
    adapter = SmartleadAdapter("k", max_batch=2)
    transport = FakeTransport([HttpResponse(200), HttpResponse(504)])
    report = execute_plan(make_plan("smartlead", 4, log), adapter, transport, log)
    assert report.sent == 2 and report.unknown == 2
    # p1 sat in another campaign: the vendor skipped it; p2 (unknown) was created, p3 not.
    reconcile(
        report, log, members_after={"p0@acme.example", "P2@acme.example"}, members_before=set()
    )
    assert report.added == 2
    assert report.skipped_by_vendor == 1
    assert report.confirmed_unknown == 1 and report.cleared_unknown == 1
    keys = {p.lead.email: p.key for b in report.batches for p in b.leads}
    assert log.state(keys["p2@acme.example"]) is PushState.COMMITTED
    assert log.state(keys["p3@acme.example"]) is None
    summary = report.summary()
    assert summary["effective_scope"] == "all_campaigns"
    assert summary["batches"][1]["status"] == "outcome_unknown"
    assert "k" not in summary["batches"][0]["url"].split("api_key=")[1].replace("***", "")


def test_reconcile_without_before_snapshot_leaves_added_unknown():
    log = InMemoryPushLog()
    report = execute_plan(
        make_plan("instantly", 1, log), InstantlyAdapter("k"), FakeTransport(), log
    )
    reconcile(report, log, members_after={"p0@acme.example"})
    assert report.added is None and report.skipped_by_vendor == 0


def test_retry_after_formats():
    assert retry_after_seconds({"Retry-After": "12"}) == 12.0
    assert retry_after_seconds({}) is None
    assert retry_after_seconds({"Retry-After": "soon"}) is None
    date = "Sun, 01 Nov 2026 17:00:30 GMT"
    now = 1793552400.0  # 2026-11-01T17:00:00Z
    assert retry_after_seconds({"Retry-After": date}, now=now) == pytest.approx(30.0)
