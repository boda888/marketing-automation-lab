import base64

import pytest
from sequencer import (
    Campaign,
    DedupeScope,
    InstantlyAdapter,
    LemlistAdapter,
    SequencerLead,
    SmartleadAdapter,
    plan_push,
)

CAMPAIGN = Campaign(campaign_id="101", name="Q4 Logistics DE")


def planned(vendor, n, **kw):
    leads = [
        SequencerLead(
            lead_id=f"l{i}",
            email=f"p{i}@acme.example",
            first_name="Ana",
            company_name="Acme",
            custom_variables={"first_line": f"line {i}"},
            **kw,
        )
        for i in range(n)
    ]
    plan = plan_push(
        vendor, CAMPAIGN, leads, dedupe_scope=DedupeScope.CAMPAIGN, allow_fixtures=True
    )
    return plan.to_send


@pytest.mark.parametrize(
    ("scope", "ignore_other", "effective"),
    [
        (DedupeScope.CAMPAIGN, True, DedupeScope.CAMPAIGN),
        (DedupeScope.ALL_CAMPAIGNS, False, DedupeScope.ALL_CAMPAIGNS),
        (DedupeScope.WORKSPACE, False, DedupeScope.ALL_CAMPAIGNS),
    ],
)
def test_smartlead_flags_explicit(scope, ignore_other, effective):
    adapter = SmartleadAdapter(api_key="sl-secret")
    (req,) = adapter.build(CAMPAIGN, planned("smartlead", 2), scope)
    assert req.method == "POST"
    assert req.url == "https://server.smartlead.ai/api/v1/campaigns/101/leads"
    assert req.json["settings"] == {
        "ignore_global_block_list": False,
        "ignore_unsubscribe_list": False,
        "ignore_community_bounce_list": False,
        "ignore_duplicate_leads_in_other_campaign": ignore_other,
    }
    assert adapter.effective_scope(scope) is effective
    first = req.json["lead_list"][0]
    assert first == {
        "email": "p0@acme.example",
        "first_name": "Ana",
        "company_name": "Acme",
        "custom_fields": {"first_line": "line 0"},
    }


def test_smartlead_batches_of_400_and_key_masked():
    adapter = SmartleadAdapter(api_key="sl-secret")
    reqs = adapter.build(CAMPAIGN, planned("smartlead", 401), DedupeScope.CAMPAIGN)
    assert [len(r.leads) for r in reqs] == [400, 1]
    assert reqs[0].params == {"api_key": "sl-secret"}
    assert "sl-secret" not in reqs[0].redacted_url()
    assert reqs[0].redacted_url().endswith("?api_key=***")
    assert "sl-secret" not in repr(adapter) + repr(reqs[0])


@pytest.mark.parametrize(
    ("scope", "workspace", "effective"),
    [
        (DedupeScope.CAMPAIGN, False, DedupeScope.CAMPAIGN),
        (DedupeScope.ALL_CAMPAIGNS, True, DedupeScope.WORKSPACE),
        (DedupeScope.WORKSPACE, True, DedupeScope.WORKSPACE),
    ],
)
def test_instantly_bulk_add_flags(scope, workspace, effective):
    adapter = InstantlyAdapter(api_key="in-secret")
    reqs = adapter.build(CAMPAIGN, planned("instantly", 1001), scope)
    assert [len(r.leads) for r in reqs] == [1000, 1]
    body = reqs[0].json
    assert reqs[0].url == "https://api.instantly.ai/api/v2/leads/add"
    assert reqs[0].headers == {"Authorization": "Bearer in-secret"}
    assert body["campaign_id"] == "101"
    assert body["skip_if_in_campaign"] is True
    assert body["skip_if_in_workspace"] is workspace
    assert "list_id" not in body
    assert body["leads"][0]["custom_variables"] == {"first_line": "line 0"}
    assert adapter.effective_scope(scope) is effective


def test_lemlist_one_call_per_lead_no_paid_lookups():
    adapter = LemlistAdapter(api_key="lm-secret")
    leads = planned("lemlist", 2, linkedin_url="https://www.linkedin.com/in/ana-example")
    reqs = adapter.build(CAMPAIGN, leads, DedupeScope.ALL_CAMPAIGNS)
    assert len(reqs) == 2
    req = reqs[0]
    assert req.url == "https://api.lemlist.com/api/campaigns/101/leads/p0@acme.example"
    assert req.params == {"deduplicate": "true"}
    token = req.headers["Authorization"].removeprefix("Basic ")
    assert base64.b64decode(token).decode() == ":lm-secret"
    assert req.json["findEmail"] is False
    assert req.json["findPhone"] is False
    assert req.json["verifyEmail"] is False
    assert req.json["linkedinEnrichment"] is False
    assert req.json["first_line"] == "line 0"
    assert req.json["linkedinUrl"] == "https://www.linkedin.com/in/ana-example"
    (camp,) = adapter.build(CAMPAIGN, leads[:1], DedupeScope.CAMPAIGN)
    assert camp.params == {"deduplicate": "false"}


def test_lemlist_plus_address_is_encoded_and_field_clash_rejected():
    adapter = LemlistAdapter(api_key="k")
    lead = SequencerLead(lead_id="x", email="ana+q4@acme.example")
    plan = plan_push(
        "lemlist", CAMPAIGN, [lead], dedupe_scope=DedupeScope.CAMPAIGN, allow_fixtures=True
    )
    (req,) = adapter.build(CAMPAIGN, plan.to_send, DedupeScope.CAMPAIGN)
    assert req.url.endswith("/leads/ana%2Bq4@acme.example")
    bad = SequencerLead(lead_id="y", email="b@acme.example", custom_variables={"findEmail": 1})
    with pytest.raises(ValueError, match="shadow"):
        adapter.lead_payload(bad)
