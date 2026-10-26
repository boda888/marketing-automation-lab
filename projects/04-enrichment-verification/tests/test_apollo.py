import json
import urllib.error
from datetime import UTC, datetime

import pytest
from enrichment import (
    AttemptStatus,
    CheckResult,
    CreditBudget,
    EmailCheck,
    EmailStatus,
    LeadRecord,
    ProviderError,
    ProviderTimeout,
    Waterfall,
    WaterfallStatus,
    assess_outcome,
)
from enrichment.apollo import (
    MATCH_PATH,
    ApolloAuthError,
    ApolloPeopleMatch,
    FakeApolloTransport,
    UrllibTransport,
    build_match_body,
)

LI = "https://www.linkedin.com/in/ada-fictional"
DOMAIN = "northwind.example"


def person(email="ada@northwind.example", status="verified", org_domain=DOMAIN):
    return {
        "person": {
            "id": "p-1",
            "email": email,
            "email_status": status,
            "linkedin_url": LI,
            "organization": {"primary_domain": org_domain} if org_domain else None,
        }
    }


def apollo(answers=None, **kw):
    transport = FakeApolloTransport(answers=answers or {}, **kw)
    return ApolloPeopleMatch(transport), transport


# ── request body ──────────────────────────────────────────────────────────────


def test_body_maps_lab_fields_and_keeps_reveals_off():
    body = build_match_body(
        {
            "first_name": "Ada",
            "last_name": "Lovelace",
            "company_domain": "https://www.Northwind.example/",
        }
    )
    assert body == {
        "first_name": "Ada",
        "last_name": "Lovelace",
        "domain": "northwind.example",
        "reveal_personal_emails": False,
        "reveal_phone_number": False,
    }


@pytest.mark.parametrize(
    "inputs",
    [
        {"linkedin_url": "Unknown"},
        {"company_domain": "", "first_name": "A", "last_name": "B"},
        {"email": "a@b.io"},
    ],
)
def test_body_refuses_placeholders_and_unmapped_fields(inputs):
    with pytest.raises(ValueError):
        build_match_body(inputs)


# ── parsing ───────────────────────────────────────────────────────────────────


def test_match_returns_work_email_with_apollo_label_and_credit():
    provider, transport = apollo({LI: person()})
    result = provider.lookup({"linkedin_url": LI})
    assert result.value == "ada@northwind.example"
    assert result.provider_status == "apollo:verified" and result.cost == 1
    assert transport.requests == [
        (
            MATCH_PATH,
            {"linkedin_url": LI, "reveal_personal_emails": False, "reveal_phone_number": False},
        )
    ]


def test_no_match_is_a_free_miss_unless_misses_are_billed():
    provider, _ = apollo()
    result = provider.lookup({"linkedin_url": LI})
    assert result.value is None and result.cost == 0
    assert result.provider_status == "apollo:no_match"
    billed = ApolloPeopleMatch(FakeApolloTransport(), charge_on_miss=True, credits_per_match=2)
    assert billed.lookup({"linkedin_url": LI}).cost == 2


def test_previous_employer_match_is_not_an_answer():
    provider, _ = apollo({DOMAIN: person(email="ada@oldco.example", org_domain="oldco.example")})
    result = provider.lookup({"first_name": "Ada", "last_name": "L", "company_domain": DOMAIN})
    assert result.value is None
    assert result.provider_status == "apollo:employer_mismatch:oldco.example"


def test_linkedin_match_is_not_checked_against_a_domain_we_did_not_send():
    provider, _ = apollo({LI: person(org_domain="newco.example", email="ada@newco.example")})
    assert provider.lookup({"linkedin_url": LI}).value == "ada@newco.example"


@pytest.mark.parametrize(
    ("email", "status", "label"),
    [
        (None, "unavailable", "apollo:unavailable"),
        ("email_not_unlocked@domain.com", "verified", "apollo:verified"),
        ("N/A", None, "apollo:no_status"),
    ],
)
def test_locked_or_missing_email_is_no_value(email, status, label):
    provider, _ = apollo({LI: person(email=email, status=status)})
    result = provider.lookup({"linkedin_url": LI})
    assert result.value is None and result.provider_status == label and result.cost == 0


# ── errors ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("status", [401, 403])
def test_bad_key_stops_the_run(status):
    provider, _ = apollo(http_errors={LI: status})
    wf = Waterfall("email", [provider])
    with pytest.raises(ApolloAuthError):
        wf.run(LeadRecord(record_id="lead-1", fields={"linkedin_url": LI}))


@pytest.mark.parametrize("status", [429, 500, 503])
def test_rate_limit_and_server_errors_move_the_waterfall_on(status):
    provider, _ = apollo(http_errors={LI: status})
    with pytest.raises(ProviderError):
        provider.lookup({"linkedin_url": LI})
    out = Waterfall("email", [provider]).run(LeadRecord(record_id="r", fields={"linkedin_url": LI}))
    assert out.attempts[0].status is AttemptStatus.ERROR


def test_timeout_is_an_unknown_outcome_booked_at_max_cost():
    provider, _ = apollo(timeouts=frozenset({LI}))
    budget = CreditBudget(3)
    out = Waterfall("email", [provider]).run(
        LeadRecord(record_id="r", fields={"linkedin_url": LI}), budget=budget
    )
    assert out.status is WaterfallStatus.OUTCOME_UNKNOWN and budget.spent == 1


# ── in a waterfall ────────────────────────────────────────────────────────────


def test_fixture_records_never_reach_apollo():
    provider, transport = apollo()
    record = LeadRecord(
        record_id="r", fields={"linkedin_url": LI, "company_domain": "acme.example"}
    )
    out = Waterfall("email", [provider]).run(record)
    assert out.attempts[0].status is AttemptStatus.SKIPPED_FIXTURE and transport.requests == []


def test_name_and_domain_set_used_when_linkedin_missing_and_label_does_not_verify():
    # paid=False only to get a .example fixture past the waterfall's fixture gate offline.
    transport = FakeApolloTransport(answers={DOMAIN: person()})
    provider = ApolloPeopleMatch(transport, paid=False)
    record = LeadRecord(
        record_id="r",
        fields={
            "first_name": "Ada",
            "last_name": "Lovelace",
            "company_domain": DOMAIN,
            "linkedin_url": "-",
        },
    )
    out = Waterfall("email", [provider]).run(record)
    assert out.answered_by == "apollo"
    assert out.attempts[0].inputs_used == ["first_name", "last_name", "company_domain"]
    assert "linkedin_url" not in transport.requests[0][1]
    # Apollo's own "verified" label, recorded as a check by the finder, is not verification.
    now = datetime(2026, 10, 26, tzinfo=UTC)
    own_label = EmailCheck(
        email=out.value, checker="apollo", checked_at=now, result=CheckResult.VALID
    )
    assessment = assess_outcome(out, [own_label], now=now)
    assert assessment.status is EmailStatus.CHECKED


# ── live transport (no network: urlopen patched) ─────────────────────────────


class _Resp:
    def __init__(self, body):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_urllib_transport_sends_key_header_and_json(monkeypatch):
    seen = {}

    def fake_urlopen(request, timeout):
        seen.update(url=request.full_url, headers=dict(request.header_items()), timeout=timeout)
        seen["body"] = json.loads(request.data)
        return _Resp(json.dumps(person()).encode())

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    transport = UrllibTransport(api_key="test-key", timeout=5)
    payload = transport.post(MATCH_PATH, {"linkedin_url": LI})
    assert seen["url"] == "https://api.apollo.io/api/v1/people/match"
    assert seen["headers"]["X-api-key"] == "test-key" and seen["timeout"] == 5
    assert seen["body"] == {"linkedin_url": LI}
    assert payload["person"]["email"] == "ada@northwind.example"


def test_urllib_transport_refuses_test_domains_before_any_request(monkeypatch):
    def no_network(request, timeout):
        raise AssertionError("request sent")

    monkeypatch.setattr("urllib.request.urlopen", no_network)
    transport = UrllibTransport(api_key="test-key")
    for body in ({"linkedin_url": "https://acme.test/in/x"}, {"domain": "northwind.example"}):
        with pytest.raises(ValueError, match="reserved test domain"):
            transport.post(MATCH_PATH, body)


def test_urllib_transport_maps_http_and_timeout_errors(monkeypatch):
    def http_401(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {}, None)

    monkeypatch.setattr("urllib.request.urlopen", http_401)
    provider = ApolloPeopleMatch(UrllibTransport(api_key="bad"))
    with pytest.raises(ApolloAuthError):
        provider.lookup({"linkedin_url": LI})

    def timed_out(request, timeout):
        raise urllib.error.URLError(TimeoutError("read timed out"))

    monkeypatch.setattr("urllib.request.urlopen", timed_out)
    with pytest.raises(ProviderTimeout):
        provider.lookup({"linkedin_url": LI})
