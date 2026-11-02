import pytest
from sequencer import (
    BatchStatus,
    Campaign,
    DedupeScope,
    HeyReachAdapter,
    HttpResponse,
    InMemoryPushLog,
    PlannedLead,
    PushState,
    SenderAccount,
    SequencerLead,
    SkipReason,
    SuppressionList,
    SuppressionReason,
    execute_plan,
    idempotency_key,
    plan_linkedin_push,
    reconcile,
)
from sequencer.fakes import FakeTransport

CAMPAIGN = Campaign(campaign_id="4242", name="LI Q4 Logistics")
SENDERS = [SenderAccount(11, "Sender A", 3), SenderAccount(12, "Sender B", 3)]


def url(i):
    return f"https://www.linkedin.com/in/person-{i}"


def lead(i, profile=None, **kw):
    return SequencerLead(
        lead_id=f"l{i}", linkedin_url=profile if profile is not None else url(i), **kw
    )


def plan(leads, *, memberships=None, scope=DedupeScope.ALL_CAMPAIGNS, **kw):
    if memberships is None:
        memberships = {x.linkedin_url: () for x in leads if x.linkedin_url}
    return plan_linkedin_push(
        CAMPAIGN, leads, kw.pop("senders", SENDERS), dedupe_scope=scope,
        memberships=memberships, **kw,
    )  # fmt: skip


def test_every_skip_reason_in_order_and_survivors_get_a_sender():
    suppression = SuppressionList()
    suppression.add_profile("de.linkedin.com/in/Opted-Out/", SuppressionReason.UNSUBSCRIBED)
    suppression.add_email("gone@fabrikam.example", SuppressionReason.BOUNCED)
    log = InMemoryPushLog()
    log.commit(idempotency_key("heyreach", "4242", url(5)))
    log.reserve(idempotency_key("heyreach", "4242", url(6)))
    leads = [
        lead(0, custom_variables={"icebreaker": "Saw the Hamburg hub"}),  # sent
        lead(1, profile="https://www.linkedin.com/company/acme"),
        lead(2, profile="https://www.linkedin.com/in/opted-out"),
        lead(3, email="gone@fabrikam.example"),
        lead(4, profile=url(0) + "/?trk=x"),  # same person as l0
        lead(5),
        lead(6),
        lead(7),  # no membership read
        lead(8),  # already in this campaign
        lead(9),  # in another campaign
        lead(10, custom_variables={"icebreaker": " "}),
    ]
    memberships = {url(i): () for i in (0, 5, 6, 10)} | {url(8): ["4242"], url(9): [77]}
    p = plan(
        leads,
        memberships=memberships,
        suppression=suppression,
        push_log=log,
        required_variables={"icebreaker"},
        allow_fixtures=True,
    )
    reasons = {s.lead_id: s.reason for s in p.skipped}
    assert reasons == {
        "l1": SkipReason.NO_PROFILE_URL,
        "l2": SkipReason.SUPPRESSED,
        "l3": SkipReason.SUPPRESSED,
        "l4": SkipReason.DUPLICATE_IN_BATCH,
        "l5": SkipReason.ALREADY_PUSHED,
        "l6": SkipReason.PUSH_PENDING,
        "l7": SkipReason.MEMBERSHIP_UNCHECKED,
        "l8": SkipReason.ALREADY_IN_CAMPAIGN,
        "l9": SkipReason.IN_OTHER_CAMPAIGN,
        "l10": SkipReason.MISSING_VARIABLES,
    }
    detail = {s.lead_id: s.detail for s in p.skipped}
    assert detail["l2"] == "unsubscribed: https://www.linkedin.com/in/opted-out"
    assert detail["l3"] == "bounced: gone@fabrikam.example"
    assert detail["l9"] == "77"
    [sent] = p.to_send
    assert sent.person_key == url(0) and sent.sender_id == 11
    assert sent.key == idempotency_key("heyreach", "4242", url(0))
    assert p.summary()["person_key"] == "linkedin_url"


def test_campaign_scope_ignores_other_campaigns_but_not_this_one():
    memberships = {url(1): ["77"], url(2): ["4242", "77"]}
    p = plan([lead(1), lead(2)], memberships=memberships, scope=DedupeScope.CAMPAIGN)
    assert [s.lead.lead_id for s in p.to_send] == ["l1"]
    assert p.skipped_by_reason == {"already_in_campaign": 1}


def test_skipped_leads_do_not_spend_sender_budget_and_caps_hold():
    senders = [SenderAccount(11, "A", 2, used_this_week=1)]
    leads = [lead(i, custom_variables={"x": "" if i == 0 else "v"}) for i in range(3)]
    p = plan(leads, senders=senders, required_variables={"x"})
    # l0 is skipped before assignment, so l1 gets the last invite; l2 waits for next week.
    assert [s.lead.lead_id for s in p.to_send] == ["l1"]
    assert p.skipped_by_reason == {"missing_variables": 1, "no_sender_budget": 1}


def test_pinned_owner_survives_planning():
    p = plan([lead(0), lead(1)], owners={"l0": 12, "l1": 99})
    assert [(s.lead.lead_id, s.sender_id) for s in p.to_send] == [("l0", 12)]
    assert p.skipped[0].reason is SkipReason.UNKNOWN_OWNER


def test_fixture_email_needs_allow_fixtures():
    leads = [lead(0, email="ana@northwind.example")]
    assert plan(leads).skipped_by_reason == {"fixture": 1}
    assert len(plan(leads, allow_fixtures=True).to_send) == 1


def test_execute_sends_sender_per_lead_and_second_run_skips():
    log, transport = InMemoryPushLog(), FakeTransport()
    adapter = HeyReachAdapter("secret", max_batch=2)
    leads = [lead(i) for i in range(3)]
    report = execute_plan(plan(leads, push_log=log), adapter, transport, log)
    assert report.sent == 3 and len(transport.requests) == 2
    assert report.effective_scope == "all_campaigns" and report.person_key == "linkedin_url"
    body = transport.requests[0].json
    assert body["campaignId"] == 4242
    assert [p["linkedInAccountId"] for p in body["accountLeadPairs"]] == [11, 12]
    assert body["accountLeadPairs"][0]["lead"]["profileUrl"] == url(0)
    again = plan(leads, push_log=log)
    assert again.to_send == [] and again.skipped_by_reason == {"already_pushed": 3}


def test_outcome_unknown_reconciled_by_profile_url_in_any_form():
    log = InMemoryPushLog()
    transport = FakeTransport([HttpResponse(200), HttpResponse(502, {"message": "bad gateway"})])
    leads = [lead(i) for i in range(4)]
    adapter = HeyReachAdapter("k", max_batch=2)
    report = execute_plan(plan(leads, push_log=log), adapter, transport, log)
    assert [b.status for b in report.batches] == [
        BatchStatus.ACCEPTED,
        BatchStatus.OUTCOME_UNKNOWN,
    ]
    # Read back with GetLeadsFromCampaign: l1 was dropped by the vendor, l2 created, l3 not.
    after = ["linkedin.com/in/PERSON-0/", "https://de.linkedin.com/in/person-2?trk=1"]
    reconcile(report, log, members_after=after, members_before=[])
    assert report.added == 2 and report.skipped_by_vendor == 1
    assert report.confirmed_unknown == 1 and report.cleared_unknown == 1
    keys = {p.person_key: p.key for b in report.batches for p in b.leads}
    assert log.state(keys[url(2)]) is PushState.COMMITTED
    assert log.state(keys[url(3)]) is None  # safe to push on the next run


def test_build_refuses_leads_not_planned_for_linkedin():
    stray = PlannedLead(lead(0), "k")
    with pytest.raises(ValueError, match="plan_linkedin_push"):
        HeyReachAdapter("k").build(CAMPAIGN, [stray], DedupeScope.CAMPAIGN)
    with pytest.raises(ValueError, match="numeric"):
        HeyReachAdapter("k").build(Campaign(campaign_id="abc", name="x"), [], DedupeScope.CAMPAIGN)


def test_profile_suppression_rejects_non_profiles():
    suppression = SuppressionList()
    with pytest.raises(ValueError):
        suppression.add_profile("https://www.linkedin.com/company/acme", SuppressionReason.MANUAL)
    suppression.add_profile("linkedin.com/in/ana-lopez", SuppressionReason.DO_NOT_CONTACT)
    assert suppression.check_profile("https://m.linkedin.com/in/Ana-Lopez/").matched.endswith(
        "/in/ana-lopez"
    )
    assert suppression.check_profile(None) is None and len(suppression) == 1
