import pytest
from sequencer import (
    Campaign,
    DedupeScope,
    HeyReachAdapter,
    HttpResponse,
    IncompleteRead,
    InMemoryPushLog,
    InstantlyAdapter,
    InstantlyMembers,
    PushState,
    SequencerLead,
    SmartleadAdapter,
    SmartleadMembers,
    TransportError,
    execute_plan,
    plan_push,
    read_members,
    reconcile,
)
from sequencer.fakes import FakeTransport

CAMPAIGN = Campaign(campaign_id="101", name="Q4 Logistics DE")


def sl_page(emails, total=None):
    body = {"data": [{"lead": {"email": e}, "status": "STARTED"} for e in emails]}
    if total is not None:
        body["total_leads"] = total
    return HttpResponse(200, body)


def test_smartlead_reads_every_offset_page_and_masks_key():
    page1 = [f"P{i}@acme.example" for i in range(100)]
    transport = FakeTransport([sl_page(page1, total=102), sl_page(["x@acme.example", None])])
    read = read_members(SmartleadMembers(SmartleadAdapter("sekret")), CAMPAIGN, transport)
    assert read.pages == 2 and len(read.members) == 101  # None row ignored, emails lowered
    assert "p0@acme.example" in read.members
    assert [r.params["offset"] for r in transport.requests] == ["0", "100"]
    assert "sekret" not in transport.requests[0].redacted_url()


def test_smartlead_full_page_at_total_stops_and_bare_list_is_accepted():
    full = [f"p{i}@acme.example" for i in range(100)]
    transport = FakeTransport([sl_page(full, total=100)])
    read = read_members(SmartleadMembers(SmartleadAdapter("k")), CAMPAIGN, transport)
    assert read.pages == 1
    bare = FakeTransport([HttpResponse(200, [{"lead": {"email": "a@acme.example"}}])])
    read = read_members(SmartleadMembers(SmartleadAdapter("k")), CAMPAIGN, bare)
    assert read.members == {"a@acme.example"}


def test_instantly_follows_cursor_and_drops_other_campaign_items():
    pages = [
        HttpResponse(
            200,
            {
                "items": [
                    {"email": "a@acme.example", "campaign": "101"},
                    {"email": "b@acme.example", "campaign": "999"},  # filter ignored?
                ],
                "next_starting_after": "c1",
            },
        ),
        HttpResponse(200, {"items": [{"email": "c@acme.example"}], "next_starting_after": "c2"}),
        HttpResponse(200, {"items": [], "next_starting_after": "c3"}),
    ]
    transport = FakeTransport(pages)
    read = read_members(InstantlyMembers(InstantlyAdapter("k")), CAMPAIGN, transport)
    assert read.members == {"a@acme.example", "c@acme.example"} and read.dropped == 1
    bodies = [r.json for r in transport.requests]
    assert bodies[0] == {"campaign": "101", "limit": 100}
    assert bodies[1]["starting_after"] == "c1"


def test_repeated_cursor_and_page_cap_raise_instead_of_truncating():
    loop = {"items": [{"email": "a@acme.example"}], "next_starting_after": "same"}
    transport = FakeTransport(default=HttpResponse(200, loop))
    with pytest.raises(IncompleteRead, match="repeated"):
        read_members(InstantlyMembers(InstantlyAdapter("k")), CAMPAIGN, transport)
    full = sl_page([f"p{i}@acme.example" for i in range(100)])
    with pytest.raises(IncompleteRead, match="pages"):
        read_members(
            SmartleadMembers(SmartleadAdapter("k")),
            CAMPAIGN,
            FakeTransport(default=full),
            max_pages=3,
        )


def test_reads_retry_429_5xx_and_timeouts_then_give_up():
    waits = []
    transport = FakeTransport(
        [
            HttpResponse(429, headers={"Retry-After": "3"}),
            TransportError("reset"),
            sl_page(["a@acme.example"]),
        ]
    )
    read = read_members(
        SmartleadMembers(SmartleadAdapter("k")), CAMPAIGN, transport, sleep=waits.append
    )
    assert read.members == {"a@acme.example"} and read.requests == 3 and waits == [3.0, 2.0]
    failing = FakeTransport(default=HttpResponse(503))
    with pytest.raises(IncompleteRead, match="503"):
        read_members(
            SmartleadMembers(SmartleadAdapter("k")), CAMPAIGN, failing, sleep=lambda _: None
        )
    assert len(failing.requests) == 3


def test_4xx_and_odd_body_are_incomplete():
    with pytest.raises(IncompleteRead, match="401"):
        read_members(
            InstantlyMembers(InstantlyAdapter("k")), CAMPAIGN, FakeTransport([HttpResponse(401)])
        )
    with pytest.raises(IncompleteRead, match="unexpected"):
        read_members(
            InstantlyMembers(InstantlyAdapter("k")),
            CAMPAIGN,
            FakeTransport([HttpResponse(200, "<html>")]),
        )


def test_read_back_feeds_reconcile_end_to_end():
    log = InMemoryPushLog()
    leads = [SequencerLead(lead_id=f"l{i}", email=f"p{i}@acme.example") for i in range(2)]
    plan = plan_push(
        "smartlead",
        CAMPAIGN,
        leads,
        dedupe_scope=DedupeScope.ALL_CAMPAIGNS,
        push_log=log,
        allow_fixtures=True,
    )
    adapter = SmartleadAdapter("k", max_batch=1)
    push = FakeTransport([HttpResponse(200), HttpResponse(504)])
    report = execute_plan(plan, adapter, push, log)
    read = read_members(
        SmartleadMembers(adapter), CAMPAIGN, FakeTransport([sl_page(["p1@acme.example"])])
    )
    reconcile(report, log, read.members, members_before=set())
    # p0 accepted but not kept (vendor dedupe); p1 timed out but was created.
    assert report.skipped_by_vendor == 1 and report.confirmed_unknown == 1
    assert log.state(plan.to_send[1].key) is PushState.COMMITTED


def test_heyreach_campaign_leads_request():
    req = HeyReachAdapter("k").campaign_leads(Campaign(campaign_id="7", name="x"), offset=200)
    assert req.url.endswith("/campaign/GetLeadsFromCampaign")
    assert req.json == {"campaignId": 7, "offset": 200, "limit": 100}
    with pytest.raises(ValueError):
        HeyReachAdapter("k").campaign_leads(Campaign(campaign_id="7", name="x"), offset=-1)
