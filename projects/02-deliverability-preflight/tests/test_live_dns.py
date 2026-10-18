"""DnsPythonResolver mapping, tested with a stub in place of dnspython's
resolver: no network. Real dnspython exceptions and rdata classes are used so
the mapping matches the library, not a guess about it."""

import dns.exception
import dns.rdata
import dns.rdataclass
import dns.rdatatype
import dns.resolver
import pytest
from deliverability_preflight import DnsPythonResolver, DNSTempError, FakeResolver, check_dmarc
from deliverability_preflight.api import create_app, resolver_from_env
from fastapi.testclient import TestClient

RUA = "rua=mailto:d@acme.example"
NP_RECORD = f"v=DMARC1; p=reject; sp=quarantine; np=reject; {RUA}"


def rdata(rtype: str, text: str):
    return dns.rdata.from_text(dns.rdataclass.IN, dns.rdatatype.from_text(rtype), text)


class StubResolver:
    """Answers ``(name, rtype)`` from a dict; values are rdata lists or exceptions."""

    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def resolve(self, qname, rdtype, *, raise_on_no_answer=True, lifetime=None, search=None):
        self.calls.append((qname, rdtype, raise_on_no_answer, lifetime, search))
        result = self.answers.get((qname.rstrip("."), rdtype), dns.resolver.NoAnswer())
        if isinstance(result, Exception):
            if isinstance(result, dns.resolver.NoAnswer) and not raise_on_no_answer:
                return []
            raise result
        return result


def live(answers, **kw):
    stub = StubResolver(answers)
    return DnsPythonResolver(stub, **kw), stub


def test_txt_character_strings_are_joined_without_separator():
    # a long DKIM key is published as several 255-byte strings (RFC 6376 §3.6.2.2)
    key = rdata("TXT", '"v=DKIM1; k=rsa; p=MIIB" "IjANBgkq"')
    r, _ = live({("s1._domainkey.acme.example", "TXT"): [key]})
    assert r.lookup("S1._domainkey.Acme.Example.", "txt") == ["v=DKIM1; k=rsa; p=MIIBIjANBgkq"]
    assert r.queries == [("s1._domainkey.acme.example", "TXT")]


def test_queries_are_absolute_without_search_list_and_with_lifetime():
    r, stub = live({("acme.example", "MX"): [rdata("MX", "10 mx1.acme.example.")]}, lifetime=2.5)
    assert r.lookup("acme.example", "MX") == ["10 mx1.acme.example."]
    assert stub.calls == [("acme.example.", "MX", True, 2.5, False)]


@pytest.mark.parametrize(
    "exc", [dns.resolver.NXDOMAIN(), dns.resolver.NoAnswer(), dns.resolver.YXDOMAIN()]
)
def test_non_existent_or_empty_is_an_empty_answer(exc):
    r, _ = live({("acme.example", "TXT"): exc})
    assert r.lookup("acme.example", "TXT") == []


@pytest.mark.parametrize(
    "exc",
    [
        dns.resolver.LifetimeTimeout(timeout=5.0, errors=[]),
        dns.resolver.NoNameservers(),
        dns.exception.Timeout(),
        dns.exception.SyntaxError("garbled"),
    ],
)
def test_failures_are_temp_errors_never_empty(exc):
    r, _ = live({("acme.example", "TXT"): exc})
    with pytest.raises(DNSTempError, match="acme.example TXT"):
        r.lookup("acme.example", "TXT")


def test_domain_exists_distinguishes_nxdomain_from_no_records():
    r, stub = live(
        {
            ("ghost.acme.example", "A"): dns.resolver.NXDOMAIN(),
            ("broken.acme.example", "A"): dns.resolver.NoNameservers(),
        }
    )
    assert r.domain_exists("ghost.acme.example") is False
    assert r.domain_exists("news.acme.example") is True  # exists, just no A record
    assert r.domain_exists("broken.acme.example") is None
    assert all(raise_on is False for _, _, raise_on, _, _ in stub.calls)


def test_domain_exists_reuses_nxdomain_seen_by_lookup():
    r, stub = live({("ghost.acme.example", "TXT"): dns.resolver.NXDOMAIN()})
    r.lookup("ghost.acme.example", "TXT")
    assert r.domain_exists("ghost.acme.example") is False
    assert len(stub.calls) == 1


def test_check_dmarc_applies_np_when_the_resolver_sees_nxdomain():
    r, _ = live(
        {
            ("_dmarc.acme.example", "TXT"): [rdata("TXT", f'"{NP_RECORD}"')],
            ("ghost.acme.example", "A"): dns.resolver.NXDOMAIN(),
        }
    )
    gone = check_dmarc("ghost.acme.example", r)
    assert gone.effective_policy == "reject"
    real = check_dmarc("news.acme.example", r)
    assert real.effective_policy == "quarantine"
    assert "DMARC_NP_IF_NXDOMAIN" not in {f.code for f in gone.findings + real.findings}


def test_existence_is_not_probed_when_np_would_not_change_the_policy():
    rec = f"v=DMARC1; p=reject; np=reject; {RUA}"
    r, stub = live({("_dmarc.acme.example", "TXT"): [rdata("TXT", f'"{rec}"')]})
    assert check_dmarc("news.acme.example", r).effective_policy == "reject"
    assert ("news.acme.example", "A") not in r.queries
    assert all(rdtype == "TXT" for _, rdtype, *_ in stub.calls)


def test_explicit_author_domain_exists_wins_over_the_probe():
    r, _ = live(
        {
            ("_dmarc.acme.example", "TXT"): [rdata("TXT", f'"{NP_RECORD}"')],
            ("ghost.acme.example", "A"): dns.resolver.NXDOMAIN(),
        }
    )
    rep = check_dmarc("ghost.acme.example", r, author_domain_exists=True)
    assert rep.effective_policy == "quarantine"
    assert ("ghost.acme.example", "A") not in r.queries


def test_fake_resolver_nxdomain_drives_np():
    records = {("_dmarc.acme.example", "TXT"): [NP_RECORD]}
    fake = FakeResolver(records, nxdomain=["ghost.acme.example"])
    assert check_dmarc("ghost.acme.example", fake).effective_policy == "reject"
    unknown = check_dmarc("news.acme.example", fake)
    assert unknown.effective_policy == "quarantine"
    assert "DMARC_NP_IF_NXDOMAIN" in {f.code for f in unknown.findings}


def test_api_live_mode_and_author_domain_exists(monkeypatch):
    monkeypatch.setenv("PREFLIGHT_DNS", "live")
    monkeypatch.setenv("PREFLIGHT_DNS_LIFETIME", "1.5")
    res = resolver_from_env()
    assert isinstance(res, DnsPythonResolver) and res._lifetime == 1.5

    monkeypatch.setenv("PREFLIGHT_DNS", "carrier-pigeon")
    with pytest.raises(ValueError, match="PREFLIGHT_DNS"):
        resolver_from_env()

    client = TestClient(create_app(FakeResolver({("_dmarc.acme.example", "TXT"): [NP_RECORD]})))
    body = client.post(
        "/v1/preflight", json={"from_domain": "ghost.acme.example", "author_domain_exists": False}
    ).json()
    assert body["dmarc"]["effective_policy"] == "reject"
