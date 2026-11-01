import pytest
from pydantic import ValidationError
from sequencer import (
    AmbiguousCampaign,
    Campaign,
    CampaignNotFound,
    SequencerLead,
    SuppressionList,
    SuppressionReason,
    idempotency_key,
    resolve_campaign,
)
from sequencer.models import DedupeScope, stricter_or_equal


def test_email_normalised_and_placeholders_dropped():
    lead = SequencerLead(
        lead_id="l1", email="  Ana@Northwind.Example ", first_name="Unknown", company_name=" - "
    )
    assert lead.email == "ana@northwind.example"
    assert lead.first_name is None
    assert lead.company_name is None


@pytest.mark.parametrize("raw", ["", "n/a", "not-an-email", "a@b", "two@@at.example", None])
def test_unusable_email_becomes_none(raw):
    assert SequencerLead(lead_id="l1", email=raw).email is None


def test_fixture_detection():
    assert SequencerLead(lead_id="l", email="a@acme.example").is_fixture
    assert SequencerLead(lead_id="l", email="a@mail.example.com").is_fixture
    assert not SequencerLead(lead_id="l", email="a@acme.io").is_fixture
    assert not SequencerLead(lead_id="l").is_fixture


def test_custom_variables_validated():
    lead = SequencerLead(lead_id="l", custom_variables={"first_line": " Unknown ", "score": 7})
    assert lead.custom_variables == {"first_line": None, "score": 7}
    assert lead.missing_variables({"first_line", "score", "city"}) == ["city", "first_line"]
    with pytest.raises(ValidationError):
        SequencerLead(lead_id="l", custom_variables={"first line": "x"})
    with pytest.raises(ValidationError):
        SequencerLead(lead_id="l", custom_variables={"tags": ["a"]})


def test_idempotency_key_stable_and_scoped():
    a = idempotency_key("smartlead", "101", "Ana@X.example")
    assert a == idempotency_key("Smartlead", "101", "ana@x.example")
    assert a != idempotency_key("smartlead", "102", "ana@x.example")
    assert a != idempotency_key("instantly", "101", "ana@x.example")


def test_scope_order():
    assert stricter_or_equal(DedupeScope.WORKSPACE, DedupeScope.ALL_CAMPAIGNS)
    assert stricter_or_equal(DedupeScope.CAMPAIGN, DedupeScope.CAMPAIGN)
    assert not stricter_or_equal(DedupeScope.CAMPAIGN, DedupeScope.ALL_CAMPAIGNS)


CAMPAIGNS = [
    Campaign(campaign_id="101", name="Q4 Logistics DE"),
    Campaign(campaign_id="102", name="Q4 Logistics  NL"),
    Campaign(campaign_id="103", name="Retail UK"),
    Campaign(campaign_id="104", name="retail uk"),
]


def test_resolve_by_name_ignores_case_and_spacing():
    assert resolve_campaign("q4 logistics nl", CAMPAIGNS).campaign_id == "102"


def test_missing_campaign_is_loud_with_hint():
    with pytest.raises(CampaignNotFound, match="similar"):
        resolve_campaign("Q4 Logistics", CAMPAIGNS)
    with pytest.raises(CampaignNotFound):
        resolve_campaign("   ", CAMPAIGNS)


def test_duplicate_campaign_name_is_loud():
    with pytest.raises(AmbiguousCampaign, match="103, 104"):
        resolve_campaign("Retail UK", CAMPAIGNS)


def test_suppression_email_and_domain_with_subdomains():
    s = SuppressionList()
    s.add_email("Opt.Out@Fabrikam.example", SuppressionReason.UNSUBSCRIBED)
    s.add_domain("@contoso.example", SuppressionReason.CUSTOMER)
    assert s.check("opt.out@fabrikam.example").reason is SuppressionReason.UNSUBSCRIBED
    assert s.check("x@eu.contoso.example").matched == "contoso.example"
    assert s.check("x@notcontoso.example") is None
    assert s.check("Unknown") is None
    assert len(s) == 2
    with pytest.raises(ValueError):
        s.add_domain("localhost", SuppressionReason.MANUAL)
    with pytest.raises(ValueError):
        s.add_email("nope", SuppressionReason.MANUAL)
