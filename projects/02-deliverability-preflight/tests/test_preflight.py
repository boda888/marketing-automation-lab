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
        "h=from:list-unsubscribe:list-unsubscribe-post; bh=x; b=y",
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


def esp_signature(**over):
    tags = {
        "v": "1",
        "a": "rsa-sha256",
        "d": "acme.example",
        "s": "esp1",
        "h": "from:list-unsubscribe:list-unsubscribe-post",
        "bh": "x",
        "b": "y",
    }
    tags.update(over)
    return ("DKIM-Signature", "; ".join(f"{k}={v}" for k, v in tags.items() if v is not None))


def esp_dns():
    return good_dns({("esp1._domainkey.acme.example", "TXT"): [f"v=DKIM1; p={KEY}"]})


def test_selector_comes_from_dkim_signature_header():
    headers = [*HEADERS[:3], esp_signature()]
    rep = preflight(
        "acme.example", esp_dns(), headers=headers, return_path_domain="bounce.acme.example"
    )
    assert rep.verdict == "pass", rep.findings
    assert [(r.domain, r.selector, r.status) for r in rep.dkim] == [
        ("acme.example", "esp1", "valid")
    ]
    assert rep.dkim_signatures[0].status == "ok"
    assert rep.alignment.dkim_aligned


def test_signature_selector_without_key_blocks():
    headers = [*HEADERS[:3], esp_signature(s="gone")]
    rep = preflight("acme.example", esp_dns(), headers=headers)
    assert rep.verdict == "block"
    assert "DKIM_KEY_MISSING" in codes(rep)
    assert not rep.alignment.dkim_aligned


def test_explicit_and_signed_selector_are_checked_once():
    rep = preflight("acme.example", good_dns(), dkim_selectors=["S1"], headers=HEADERS)
    assert [(r.domain, r.selector) for r in rep.dkim] == [("acme.example", "s1")]


def test_esp_domain_signature_sets_dkim_domain_and_does_not_align():
    dns = good_dns({("esp1._domainkey.esp.example", "TXT"): [f"v=DKIM1; p={KEY}"]})
    headers = [*HEADERS[:3], esp_signature(d="esp.example")]
    rep = preflight("acme.example", dns, headers=headers)
    assert rep.dkim_domain == "esp.example"
    assert rep.dkim[0].status == "valid"
    assert not rep.alignment.dkim_aligned


def test_broken_signature_does_not_count_for_alignment():
    headers = [*HEADERS[:3], esp_signature(a="rsa-sha1")]
    rep = preflight("acme.example", esp_dns(), headers=headers)
    assert "DKIM_SIG_SHA1" in codes(rep)
    assert rep.dkim[0].status == "valid"
    assert not rep.alignment.dkim_aligned
    assert rep.verdict == "block"
