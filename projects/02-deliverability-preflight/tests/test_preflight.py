import base64

from deliverability_preflight import ComplaintStats, FakeResolver, preflight
from test_dkim import spki

KEY = base64.b64encode(spki(2048)).decode()
HEADERS = [
    ("From", "Sam <sam@acme.example>"),
    ("List-Unsubscribe", "<https://u.acme.example/u/abc>, <mailto:u@acme.example>"),
    ("List-Unsubscribe-Post", "List-Unsubscribe=One-Click"),
    (
        "DKIM-Signature",
        "v=1; a=rsa-sha256; d=acme.example; s=s1; "
        "h=from:list-unsubscribe:list-unsubscribe-post; b=x",
    ),
]


def good_dns(overrides=None):
    records = {
        ("acme.example", "TXT"): ["v=spf1 ip4:198.51.100.0/24 -all"],
        ("bounce.acme.example", "TXT"): ["v=spf1 ip4:198.51.100.0/24 -all"],
        ("_dmarc.acme.example", "TXT"): ["v=DMARC1; p=reject; rua=mailto:d@acme.example"],
        ("s1._domainkey.acme.example", "TXT"): [f"v=DKIM1; k=rsa; p={KEY}"],
    }
    records.update(overrides or {})
    return FakeResolver(records)


def codes(rep):
    return {f.code for f in rep.findings}


def test_clean_setup_passes():
    rep = preflight(
        "acme.example",
        good_dns(),
        dkim_selectors=["s1"],
        return_path_domain="bounce.acme.example",
        headers=HEADERS,
        complaint_stats=[ComplaintStats(delivered=5000, complaints=2, provider="gmail")],
    )
    assert rep.verdict == "pass", rep.findings
    assert rep.spf.domain == "bounce.acme.example"  # SPF is checked on the Return-Path domain
    assert rep.alignment.spf_aligned and rep.alignment.dkim_aligned
    assert rep.unsubscribe.one_click and rep.complaints[0].status == "ok"


def test_warnings_give_review():
    rep = preflight(
        "acme.example",
        good_dns(),
        dkim_selectors=["s1"],
        complaint_stats=[ComplaintStats(delivered=10_000, complaints=15)],
    )
    assert rep.verdict == "review" and codes(rep) == {"COMPLAINT_RATE_OVER_TARGET"}
    assert rep.unsubscribe is None


def test_revoked_dkim_key_breaks_dkim_alignment():
    dns = good_dns({("s1._domainkey.acme.example", "TXT"): ["v=DKIM1; p="]})
    rep = preflight("acme.example", dns, dkim_selectors=["s1"])
    assert rep.verdict == "block"
    assert {"DKIM_KEY_REVOKED", "DMARC_SPF_ONLY_ALIGNMENT"} <= codes(rep)
    assert not rep.alignment.dkim_aligned


def test_third_party_return_path_and_no_selectors_blocks():
    dns = good_dns({("esp.example", "TXT"): ["v=spf1 ip4:203.0.113.0/24 -all"]})
    rep = preflight("acme.example", dns, dkim_selectors=[], return_path_domain="esp.example")
    assert rep.verdict == "block"
    assert {"DKIM_NO_SELECTORS", "DMARC_NOT_ALIGNED"} <= codes(rep)


def test_strict_alignment_comes_from_dmarc_record():
    dns = good_dns(
        {
            ("_dmarc.acme.example", "TXT"): [
                "v=DMARC1; p=reject; adkim=s; aspf=s; rua=mailto:d@acme.example"
            ]
        }
    )
    rep = preflight(
        "acme.example", dns, dkim_selectors=["s1"], return_path_domain="bounce.acme.example"
    )
    # strict aspf: bounce.acme.example != acme.example, but d=acme.example still aligns
    assert not rep.alignment.spf_aligned and rep.alignment.dkim_aligned
    assert rep.verdict == "pass"


def test_complaint_stop_and_bad_headers_block():
    rep = preflight(
        "acme.example",
        good_dns(),
        dkim_selectors=["s1"],
        headers=[("From", "sam@acme.example")],
        complaint_stats=[ComplaintStats(delivered=2000, complaints=9)],
    )
    assert rep.verdict == "block"
    assert {"UNSUB_HEADER_MISSING", "COMPLAINT_RATE_OVER_LIMIT"} <= codes(rep)


def test_report_is_json_serialisable():
    rep = preflight("acme.example", good_dns(), dkim_selectors=["s1"])
    data = rep.model_dump(mode="json")
    assert data["verdict"] == "pass" and data["dkim"][0]["key_bits"] == 2048
