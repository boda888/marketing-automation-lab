import pytest
from enrichment import LeadRecord, ProviderResult, clean, is_reserved_host
from pydantic import ValidationError


@pytest.mark.parametrize("raw", [None, "", "  ", "Unknown", "N/A", "-", "null", "Not Found", "TBD"])
def test_placeholders_are_none(raw):
    assert clean(raw) is None


def test_clean_strips_real_values():
    assert clean("  ada@northwind.example ") == "ada@northwind.example"
    assert clean(0) == "0"  # a real zero is data, not a placeholder


def test_record_fields_drop_placeholders():
    r = LeadRecord(
        record_id="lead-1", fields={"linkedin_url": "Unknown", " email ": " x@y.example "}
    )
    assert r.get("linkedin_url") is None
    assert r.get("email") == "x@y.example"


@pytest.mark.parametrize("rid", ["", "  ", "unknown", "N/A"])
def test_record_id_must_be_real(rid):
    with pytest.raises(ValidationError):
        LeadRecord(record_id=rid)


@pytest.mark.parametrize(
    "host",
    [
        "northwind.example",
        "example",
        "a.b.test",
        "x.invalid",
        "localhost",
        "example.com",
        "mail.example.org",
    ],
)
def test_reserved_hosts(host):
    assert is_reserved_host(host)


@pytest.mark.parametrize("host", ["northwind.io", "notexample.com", "example.co", "examples.com"])
def test_real_looking_hosts_are_not_reserved(host):
    assert not is_reserved_host(host)


@pytest.mark.parametrize(
    "fields",
    [
        {"company_domain": "northwind.example"},
        {"email": "ada@northwind.example"},
        {"website": "https://www.northwind.example/about"},
        {"website": "northwind.example/careers"},
    ],
)
def test_fixture_detected_from_any_domain_like_field(fields):
    assert LeadRecord(record_id="r", fields=fields).is_fixture


def test_non_fixture_record():
    r = LeadRecord(record_id="r", fields={"company_domain": "northwind.io", "name": "Ada K. Lovel"})
    assert not r.is_fixture


def test_provider_placeholder_answer_is_no_answer():
    assert ProviderResult(value="Unknown").value is None
    with pytest.raises(ValidationError):
        ProviderResult(value="a@b.io", cost=-1)
