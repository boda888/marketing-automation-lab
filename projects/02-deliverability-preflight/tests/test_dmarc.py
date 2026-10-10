import pytest
from deliverability_preflight import (
    DmarcSyntaxError,
    FakeResolver,
    aligned,
    check_alignment,
    check_dmarc,
    parse_dmarc,
)


def codes(report):
    return {f.code for f in report.findings}


def test_parse_full_record():
    rec = parse_dmarc(
        "v=DMARC1; p=Reject; sp=quarantine; adkim=s; aspf=r; pct=50; "
        "rua=mailto:dmarc@acme.example!10m, mailto:agg@reports.example; ruf=mailto:f@acme.example"
    )
    assert (rec.p, rec.sp, rec.adkim, rec.aspf, rec.pct) == ("reject", "quarantine", "s", "r", 50)
    assert rec.rua == ["mailto:dmarc@acme.example", "mailto:agg@reports.example"]
    assert rec.ruf == ["mailto:f@acme.example"]


def test_parse_defaults_and_unknown_tags():
    rec = parse_dmarc("v=DMARC1;p=none;;foo=bar")
    assert (rec.adkim, rec.aspf, rec.pct, rec.rua) == ("r", "r", 100, [])
    assert rec.tags["foo"] == "bar"


@pytest.mark.parametrize(
    "raw",
    [
        "p=reject; v=DMARC1",
        "v=DMARC2; p=reject",
        "v=DMARC1; p=block",
        "v=DMARC1; p=none; adkim=x",
        "v=DMARC1; p=none; pct=150",
        "v=DMARC1; p=none; pct=abc",
        "v=DMARC1; p=none; p=reject",
        "v=DMARC1; garbage",
    ],
)
def test_parse_rejects(raw):
    with pytest.raises(DmarcSyntaxError):
        parse_dmarc(raw)


def test_valid_enforced_policy_has_no_findings():
    r = FakeResolver(
        {("_dmarc.acme.example", "TXT"): ["v=DMARC1; p=reject; rua=mailto:d@acme.example"]}
    )
    rep = check_dmarc("acme.example", r)
    assert rep.status == "valid" and rep.effective_policy == "reject"
    assert rep.findings == []


def test_missing_record():
    rep = check_dmarc("news.acme.example", FakeResolver({}))
    assert rep.status == "none" and "DMARC_MISSING" in codes(rep)


def test_subdomain_inherits_sp_from_parent():
    r = FakeResolver(
        {
            ("_dmarc.acme.example", "TXT"): [
                "v=DMARC1; p=reject; sp=quarantine; rua=mailto:d@acme.example"
            ]
        }
    )
    rep = check_dmarc("news.mail.acme.example", r)
    assert rep.policy_domain == "acme.example"
    assert rep.effective_policy == "quarantine"
    assert "DMARC_INHERITED" in codes(rep)
    assert ("_dmarc.example", "TXT") not in r.queries  # never queries the TLD


def test_p_none_pct_and_no_rua():
    none = check_dmarc(
        "a.example", FakeResolver({("_dmarc.a.example", "TXT"): ["v=DMARC1; p=none"]})
    )
    assert {"DMARC_POLICY_NONE", "DMARC_NO_RUA"} <= codes(none)
    part = check_dmarc(
        "a.example",
        FakeResolver(
            {
                ("_dmarc.a.example", "TXT"): [
                    "v=DMARC1; p=quarantine; pct=25; rua=mailto:x@a.example"
                ]
            }
        ),
    )
    assert codes(part) == {"DMARC_PCT_PARTIAL"}


def test_missing_p_multiple_and_syntax():
    no_p = FakeResolver({("_dmarc.a.example", "TXT"): ["v=DMARC1; rua=mailto:x@a.example"]})
    rep = check_dmarc("a.example", no_p)
    assert rep.status == "invalid" and "DMARC_NO_POLICY" in codes(rep)
    two = FakeResolver({("_dmarc.a.example", "TXT"): ["v=DMARC1; p=none", "v=DMARC1; p=reject"]})
    assert "DMARC_MULTIPLE_RECORDS" in codes(check_dmarc("a.example", two))
    bad = FakeResolver({("_dmarc.a.example", "TXT"): ["v=DMARC1; p=maybe"]})
    assert "DMARC_SYNTAX" in codes(check_dmarc("a.example", bad))


def test_non_dmarc_txt_is_ignored():
    r = FakeResolver({("_dmarc.a.example", "TXT"): ["some-verification=1"]})
    assert check_dmarc("a.example", r).status == "none"


def test_external_report_destination_authorisation():
    rec = "v=DMARC1; p=reject; rua=mailto:agg@reports.vendor.example"
    unauth = FakeResolver({("_dmarc.acme.example", "TXT"): [rec]})
    rep = check_dmarc("acme.example", unauth)
    assert "DMARC_EXTERNAL_REPORTS_UNAUTHORIZED" in codes(rep)
    assert ("acme.example._report._dmarc.reports.vendor.example", "TXT") in unauth.queries

    auth = FakeResolver(
        {
            ("_dmarc.acme.example", "TXT"): [rec],
            ("acme.example._report._dmarc.reports.vendor.example", "TXT"): ["v=DMARC1"],
        }
    )
    assert check_dmarc("acme.example", auth).findings == []


def test_non_mailto_report_uri():
    r = FakeResolver(
        {("_dmarc.a.example", "TXT"): ["v=DMARC1; p=reject; rua=https://r.example/dmarc"]}
    )
    assert "DMARC_REPORT_URI_NOT_MAILTO" in codes(check_dmarc("a.example", r))


def test_temp_error():
    r = FakeResolver({}, fail=[("_dmarc.a.example", "TXT")])
    assert check_dmarc("a.example", r).status == "temperror"


def test_aligned_modes():
    assert aligned("bounce.acme.example", "acme.example", "r")
    assert not aligned("bounce.acme.example", "acme.example", "s")
    assert aligned("ACME.example.", "acme.example", "s")
    assert not aligned("esp.example", "acme.example", "r")


def test_custom_org_domain_for_multi_label_suffix():
    def psl(d):  # pretend "co.example" is a public suffix
        labels = d.split(".")
        n = 3 if d.endswith(".co.example") else 2
        return ".".join(labels[-n:])

    assert aligned("a.shop.co.example", "b.shop.co.example", "r", psl)
    assert not aligned("shop.co.example", "other.co.example", "r", psl)
    assert aligned("shop.co.example", "other.co.example", "r")  # naive default gets it wrong


def test_check_alignment_outcomes():
    ok = check_alignment(
        "acme.example", spf_domain="bounce.acme.example", dkim_domains=["acme.example"]
    )
    assert ok.dmarc_pass_possible and ok.findings == []

    spf_only = check_alignment(
        "acme.example", spf_domain="bounce.acme.example", dkim_domains=["esp.example"]
    )
    assert [f.code for f in spf_only.findings] == ["DMARC_SPF_ONLY_ALIGNMENT"]

    none = check_alignment("acme.example", spf_domain="esp.example", dkim_domains=["esp.example"])
    assert not none.dmarc_pass_possible
    assert [f.code for f in none.findings] == ["DMARC_NOT_ALIGNED"]

    strict = check_alignment(
        "acme.example", spf_domain=None, dkim_domains=["mail.acme.example"], adkim="s"
    )
    assert not strict.dkim_aligned
