import pytest
from sequencer import (
    Campaign,
    HeyReachAdapter,
    SenderAccount,
    SequencerLead,
    Unassigned,
    assign_senders,
    normalise_linkedin_url,
)


def lead(i, url=None, **kw):
    return SequencerLead(
        lead_id=f"l{i}",
        linkedin_url=url if url is not None else f"https://www.linkedin.com/in/person-{i}",
        **kw,
    )


@pytest.mark.parametrize(
    "raw",
    [
        "https://www.linkedin.com/in/Ana-Lopez",
        "http://linkedin.com/in/ana-lopez/",
        "de.linkedin.com/in/ana-lopez?trk=public_profile",
        "https://m.linkedin.com/in/ana-lopez/details/experience/#top",
        "  https://www.linkedin.com/in/ana%2Dlopez  ",
    ],
)
def test_profile_url_variants_share_one_key(raw):
    assert normalise_linkedin_url(raw) == "https://www.linkedin.com/in/ana-lopez"


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "n/a",
        "https://www.linkedin.com/company/acme-example",
        "https://www.linkedin.com/sales/lead/ACwAAA,NAME_SEARCH",
        "https://www.linkedin.com/in/",
        "https://evil.example/in/ana-lopez",
        "https://linkedin.com.evil.example/in/ana-lopez",
        "https://www.linkedin.com/in/a b",
    ],
)
def test_non_member_urls_are_refused(raw):
    assert normalise_linkedin_url(raw) is None


def test_unowned_leads_spread_to_sender_with_most_budget_left():
    senders = [
        SenderAccount(1, "Sender A", weekly_invite_budget=100, used_this_week=98),
        SenderAccount(2, "Sender B", weekly_invite_budget=100, used_this_week=97),
    ]
    out = assign_senders([lead(i) for i in range(6)], senders)
    assert [p.sender.account_id for p in out.pairs] == [2, 1, 2, 1, 2]
    assert [h.reason for h in out.held] == [Unassigned.NO_SENDER_BUDGET]
    assert out.used == {1: 2, 2: 3}
    assert out.summary()["held"] == {"no_sender_budget": 1}


def test_budget_is_never_exceeded():
    senders = [SenderAccount(i, f"S{i}", weekly_invite_budget=3) for i in (1, 2, 3)]
    out = assign_senders([lead(i) for i in range(20)], senders)
    assert len(out.pairs) == 9
    assert all(n == 3 for n in out.used.values())


def test_owner_is_kept_even_when_others_have_budget():
    senders = [
        SenderAccount(1, "Owner", weekly_invite_budget=1),
        SenderAccount(2, "Spare", weekly_invite_budget=50),
    ]
    owners = {"l0": 1, "l1": 1, "l2": 9}
    out = assign_senders([lead(0), lead(1), lead(2)], senders, owners=owners)
    assert [(p.lead.lead_id, p.sender.account_id) for p in out.pairs] == [("l0", 1)]
    assert [(h.lead.lead_id, h.reason) for h in out.held] == [
        ("l1", Unassigned.OWNER_OVER_BUDGET),
        ("l2", Unassigned.UNKNOWN_OWNER),
    ]


def test_inactive_sender_gets_nothing():
    senders = [
        SenderAccount(1, "Logged out", weekly_invite_budget=100, active=False),
        SenderAccount(2, "Live", weekly_invite_budget=1),
    ]
    out = assign_senders([lead(0), lead(1)], senders)
    assert [p.sender.account_id for p in out.pairs] == [2]
    assert out.held[0].reason is Unassigned.NO_SENDER_BUDGET


def test_missing_duplicate_and_member_profiles_are_held():
    leads = [
        lead(0, url="n/a"),
        lead(1, url="https://www.linkedin.com/in/dup"),
        lead(2, url="linkedin.com/in/DUP/"),
        lead(3, url="https://www.linkedin.com/in/member"),
    ]
    out = assign_senders(
        leads,
        [SenderAccount(1, "S", weekly_invite_budget=10)],
        in_campaign=["https://de.linkedin.com/in/member?x=1"],
    )
    assert [(h.lead.lead_id, h.reason) for h in out.held] == [
        ("l0", Unassigned.NO_PROFILE_URL),
        ("l2", Unassigned.DUPLICATE_PROFILE),
        ("l3", Unassigned.ALREADY_IN_CAMPAIGN),
    ]
    assert out.used == {1: 1}


def test_sender_validation():
    with pytest.raises(ValueError):
        SenderAccount(1, "S", weekly_invite_budget=-1)
    with pytest.raises(ValueError):
        assign_senders([], [SenderAccount(1, "A", 5), SenderAccount(1, "B", 5)])


def test_heyreach_add_leads_pairs_every_lead_with_its_sender():
    senders = [SenderAccount(11, "A", 10), SenderAccount(22, "B", 10)]
    leads = [
        lead(0, email="ana@acme.example", first_name="Ana", custom_variables={"first_line": "Hi"}),
        lead(1, company_name="Acme", custom_variables={"n": 3}),
    ]
    out = assign_senders(leads, senders, owners={"l1": 11})
    adapter = HeyReachAdapter(api_key="secret-key")
    [req] = adapter.add_leads(Campaign(campaign_id="4242", name="LI Q4"), out.pairs)
    assert req.url == "https://api.heyreach.io/api/n8n/campaign/AddLeadsToCampaign"
    assert req.headers == {"X-N8N-KEY": "secret-key"}
    assert req.json == {
        "campaignId": 4242,
        "accountLeadPairs": [
            {
                "linkedInAccountId": 11,
                "lead": {
                    "profileUrl": "https://www.linkedin.com/in/person-0",
                    "firstName": "Ana",
                    "emailAddress": "ana@acme.example",
                    "customUserFields": [{"name": "first_line", "value": "Hi"}],
                },
            },
            {
                "linkedInAccountId": 11,
                "lead": {
                    "profileUrl": "https://www.linkedin.com/in/person-1",
                    "companyName": "Acme",
                    "customUserFields": [{"name": "n", "value": "3"}],
                },
            },
        ],
    }
    assert "secret-key" not in repr(adapter) and "secret-key" not in repr(req)


def test_heyreach_batches_and_rejects_non_numeric_campaign():
    senders = [SenderAccount(1, "A", 500)]
    pairs = assign_senders([lead(i) for i in range(250)], senders).pairs
    adapter = HeyReachAdapter(api_key="k")
    reqs = adapter.add_leads(Campaign(campaign_id="7", name="x"), pairs)
    assert [len(r.json["accountLeadPairs"]) for r in reqs] == [100, 100, 50]
    with pytest.raises(ValueError):
        adapter.add_leads(Campaign(campaign_id="abc", name="x"), pairs)


def test_campaigns_for_lead_precheck_request():
    adapter = HeyReachAdapter(api_key="k")
    req = adapter.campaigns_for_lead("de.linkedin.com/in/Ana-Lopez/", offset=100)
    assert req.url.endswith("/campaign/GetCampaignsForLead")
    assert req.json == {
        "profileUrl": "https://www.linkedin.com/in/ana-lopez",
        "offset": 100,
        "limit": 100,
    }
    with pytest.raises(ValueError):
        adapter.campaigns_for_lead("https://www.linkedin.com/company/acme")
