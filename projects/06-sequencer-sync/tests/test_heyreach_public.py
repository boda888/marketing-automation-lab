"""HeyReach public API: AddLeadsToCampaignV2 shapes and counts that must confirm a batch."""

import pytest
from sequencer import (
    AddLeadsCounts,
    BatchStatus,
    Campaign,
    DedupeScope,
    HeyReachAdapter,
    HttpResponse,
    InMemoryPushLog,
    PushState,
    SenderAccount,
    SequencerLead,
    execute_plan,
    parse_add_leads_counts,
    plan_linkedin_push,
    reconcile,
)
from sequencer.fakes import FakeTransport

CAMPAIGN = Campaign(campaign_id="4242", name="LI Q4 Logistics")
SENDERS = [SenderAccount(11, "Sender A", 5), SenderAccount(12, "Sender B", 5)]


def url(i):
    return f"https://www.linkedin.com/in/person-{i}"


def plan(n, log):
    leads = [SequencerLead(lead_id=f"l{i}", linkedin_url=url(i)) for i in range(n)]
    return plan_linkedin_push(
        CAMPAIGN, leads, SENDERS, dedupe_scope=DedupeScope.ALL_CAMPAIGNS,
        memberships={x.linkedin_url: () for x in leads}, push_log=log,
    )  # fmt: skip


def counts(added, updated=0, failed=0):
    return {"addedLeadsCount": added, "updatedLeadsCount": updated, "failedLeadsCount": failed}


def push(responses, n=2, max_batch=2):
    log = InMemoryPushLog()
    transport = FakeTransport(responses)
    adapter = HeyReachAdapter.public("pub-key", max_batch=max_batch)
    report = execute_plan(plan(n, log), adapter, transport, log)
    return report, transport, log


def test_public_request_uses_v2_path_key_header_and_explicit_resume_flags():
    report, transport, _ = push([HttpResponse(200, counts(2))])
    req = transport.requests[0]
    assert req.url == "https://api.heyreach.io/api/public/campaign/AddLeadsToCampaignV2"
    assert req.headers == {"X-API-KEY": "pub-key"}
    assert req.json["resumeFinishedCampaign"] is False
    assert req.json["resumePausedCampaign"] is False
    assert [p["linkedInAccountId"] for p in req.json["accountLeadPairs"]] == [11, 12]
    assert "pub-key" not in repr(HeyReachAdapter.public("pub-key"))
    batch = report.batches[0]
    assert batch.status is BatchStatus.ACCEPTED and report.sent == 2
    assert batch.detail == "added=2 updated=0 failed=0 sent=2"
    assert report.summary()["batches"][0]["detail"] == batch.detail


def test_n8n_endpoint_unchanged_and_takes_bare_2xx():
    adapter = HeyReachAdapter("n8n-key")
    log = InMemoryPushLog()
    transport = FakeTransport([HttpResponse(200)])
    report = execute_plan(plan(1, log), adapter, transport, log)
    req = transport.requests[0]
    assert req.url.endswith("/api/n8n/campaign/AddLeadsToCampaign")
    assert req.headers == {"X-N8N-KEY": "n8n-key"}
    assert "resumePausedCampaign" not in req.json
    assert report.batches[0].status is BatchStatus.ACCEPTED


def test_updated_leads_count_as_landed():
    # A lead already in the campaign comes back as "updated": the batch is confirmed.
    report, _, log = push([HttpResponse(200, counts(1, updated=1))])
    assert report.batches[0].status is BatchStatus.ACCEPTED
    assert all(log.state(p.key) is PushState.COMMITTED for p in report.batches[0].leads)


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        (counts(1, failed=1), "failed=1"),  # which lead failed is not said
        (counts(1), "sent=2"),  # one lead unaccounted for
        (counts(3), "added=3"),  # more than sent: not our batch's answer
        ({"success": True}, "without addedLeadsCount"),
        (None, "without addedLeadsCount"),
        (counts(True, updated=1), "without addedLeadsCount"),  # bool is not a count
    ],
)
def test_counts_that_do_not_confirm_keep_keys_reserved(body, reason):
    report, _, log = push([HttpResponse(200, body)])
    batch = report.batches[0]
    assert batch.status is BatchStatus.OUTCOME_UNKNOWN and reason in batch.error
    assert report.sent == 0 and report.unknown == 2
    assert all(log.state(p.key) is PushState.RESERVED for p in batch.leads)


def test_failed_count_settled_by_read_back():
    report, _, log = push([HttpResponse(200, counts(1, failed=1))])
    reconcile(report, log, members_after=["linkedin.com/in/person-1/"], members_before=[])
    assert report.confirmed_unknown == 1 and report.cleared_unknown == 1
    keys = {p.person_key: p.key for p in report.batches[0].leads}
    assert log.state(keys[url(1)]) is PushState.COMMITTED
    assert log.state(keys[url(0)]) is None  # failed lead may be pushed again next run


def test_each_batch_judged_on_its_own_counts():
    report, _, _ = push([HttpResponse(200, counts(2)), HttpResponse(200, counts(0, failed=1))], 3)
    assert [b.status for b in report.batches] == [
        BatchStatus.ACCEPTED,
        BatchStatus.OUTCOME_UNKNOWN,
    ]
    assert report.sent == 2 and report.unknown == 1


def test_parse_counts():
    assert parse_add_leads_counts(counts(2, 1, 0)) == AddLeadsCounts(2, 1, 0)
    assert parse_add_leads_counts(counts(2, 1, 0)).total == 3
    assert parse_add_leads_counts(counts(-1)) is None
    assert parse_add_leads_counts({"addedLeadsCount": 1}) is None
    assert parse_add_leads_counts([1, 0, 0]) is None


def test_unknown_api_refused():
    with pytest.raises(ValueError, match="api must be"):
        HeyReachAdapter("k", api="v3")
