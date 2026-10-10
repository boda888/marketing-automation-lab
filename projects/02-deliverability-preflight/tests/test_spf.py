import pytest
from deliverability_preflight import FakeResolver, SpfSyntaxError, check_spf, parse_spf, worst


def codes(report):
    return {f.code for f in report.findings}


def test_parse_mechanisms_and_modifiers():
    rec = parse_spf("v=spf1 ip4:192.0.2.0/24 a:mail.acme.example/28//64 -mx ~all exp=why.example")
    names = [(m.qualifier, m.name) for m in rec.mechanisms]
    assert names == [("+", "ip4"), ("+", "a"), ("-", "mx"), ("~", "all")]
    assert rec.mechanisms[1].domain == "mail.acme.example"
    assert (rec.mechanisms[1].cidr4, rec.mechanisms[1].cidr6) == (28, 64)
    assert rec.exp == "why.example" and rec.redirect is None
    assert rec.all_mechanism.qualifier == "~"


@pytest.mark.parametrize(
    "raw",
    [
        "v=spf2 -all",
        "v=spf1 foo:bar.example -all",
        "v=spf1 ip4:300.1.1.1 -all",
        "v=spf1 ip4:2001:db8::1 -all",
        "v=spf1 include -all",
        "v=spf1 all:x",
        "v=spf1 a/40 -all",
        "v=spf1 redirect=a.example redirect=b.example",
        "v=spf1 include:x.example/24 -all",
    ],
)
def test_parse_rejects_invalid(raw):
    with pytest.raises(SpfSyntaxError):
        parse_spf(raw)


def test_unknown_modifiers_are_kept_not_rejected():
    rec = parse_spf("v=spf1 -all foo=bar")
    assert rec.unknown_modifiers == {"foo": "bar"}


def test_simple_valid_policy_counts_lookups():
    r = FakeResolver(
        {
            ("acme.example", "TXT"): [
                "google-site-verification=x",
                "v=spf1 include:_spf.esp.example mx -all",
            ],
            ("_spf.esp.example", "TXT"): ["v=spf1 ip4:198.51.100.0/24 ip6:2001:db8::/32 ~all"],
            ("acme.example", "MX"): ["10 mx1.acme.example"],
        }
    )
    rep = check_spf("ACME.example.", r)
    assert rep.status == "valid"
    assert rep.lookups == 2 and rep.void_lookups == 0
    assert rep.visited == ["acme.example", "_spf.esp.example"]
    assert rep.findings == [] and worst(rep.findings) is None


def test_missing_and_multiple_records():
    assert check_spf("none.example", FakeResolver({})).status == "none"
    r = FakeResolver({("two.example", "TXT"): ["v=spf1 -all", "v=spf1 ~all"]})
    rep = check_spf("two.example", r)
    assert rep.status == "permerror" and "SPF_MULTIPLE_RECORDS" in codes(rep)


def test_v_spf10_is_not_an_spf_record():
    rep = check_spf("x.example", FakeResolver({("x.example", "TXT"): ["v=spf10 -all"]}))
    assert rep.status == "none"


def _chain(n):
    """Top record with n includes, each include an ip4-only record."""
    recs = {
        ("big.example", "TXT"): [
            "v=spf1 " + " ".join(f"include:s{i}.esp.example" for i in range(n)) + " -all"
        ]
    }
    for i in range(n):
        recs[(f"s{i}.esp.example", "TXT")] = [f"v=spf1 ip4:203.0.113.{i} -all"]
    return FakeResolver(recs)


def test_lookup_limit_boundary():
    assert check_spf("big.example", _chain(10)).status == "valid"
    assert "SPF_LOOKUPS_NEAR_LIMIT" in codes(check_spf("big.example", _chain(10)))
    rep = check_spf("big.example", _chain(11))
    assert rep.lookups == 11 and rep.status == "permerror"
    assert "SPF_TOO_MANY_LOOKUPS" in codes(rep)


def test_nested_includes_and_redirect_count():
    r = FakeResolver(
        {
            ("acme.example", "TXT"): ["v=spf1 include:a.example redirect=b.example"],
            ("a.example", "TXT"): ["v=spf1 include:c.example ?all"],
            ("c.example", "TXT"): ["v=spf1 ip4:192.0.2.1 -all"],
            ("b.example", "TXT"): ["v=spf1 ip4:192.0.2.2 -all"],
        }
    )
    rep = check_spf("acme.example", r)
    # include a + include c + redirect b
    assert rep.lookups == 3 and rep.status == "valid"
    # '?all' inside an include only ends that include; the redirect target's -all decides
    assert "SPF_NEUTRAL_ALL" not in codes(rep)


def test_redirect_ignored_when_all_present():
    r = FakeResolver({("acme.example", "TXT"): ["v=spf1 -all redirect=b.example"]})
    rep = check_spf("acme.example", r)
    assert "SPF_REDIRECT_IGNORED" in codes(rep) and rep.lookups == 0
    assert ("b.example", "TXT") not in r.queries


def test_void_lookups():
    r = FakeResolver(
        {
            ("acme.example", "TXT"): [
                "v=spf1 a:old1.acme.example a:old2.acme.example mx:gone.example -all"
            ]
        }
    )
    rep = check_spf("acme.example", r)
    assert rep.void_lookups == 3 and rep.status == "permerror"
    assert "SPF_TOO_MANY_VOID_LOOKUPS" in codes(rep)


def test_a_with_only_aaaa_is_not_void():
    r = FakeResolver(
        {("acme.example", "TXT"): ["v=spf1 a -all"], ("acme.example", "AAAA"): ["2001:db8::1"]}
    )
    assert check_spf("acme.example", r).void_lookups == 0


def test_include_without_record_is_permerror():
    r = FakeResolver({("acme.example", "TXT"): ["v=spf1 include:gone.example -all"]})
    rep = check_spf("acme.example", r)
    assert rep.status == "permerror" and "SPF_TARGET_NO_RECORD" in codes(rep)


def test_include_loop_detected():
    r = FakeResolver(
        {
            ("a.example", "TXT"): ["v=spf1 include:b.example -all"],
            ("b.example", "TXT"): ["v=spf1 include:a.example -all"],
        }
    )
    rep = check_spf("a.example", r)
    assert "SPF_LOOP" in codes(rep) and rep.status == "permerror"


def test_dangerous_and_weak_policies():
    pass_all = check_spf("p.example", FakeResolver({("p.example", "TXT"): ["v=spf1 +all"]}))
    assert "SPF_PASS_ALL" in codes(pass_all) and worst(pass_all.findings) == "error"
    bare_all = check_spf("p.example", FakeResolver({("p.example", "TXT"): ["v=spf1 all"]}))
    assert "SPF_PASS_ALL" in codes(bare_all)
    neutral = check_spf("n.example", FakeResolver({("n.example", "TXT"): ["v=spf1 ?all"]}))
    assert "SPF_NEUTRAL_ALL" in codes(neutral)
    no_all = check_spf("o.example", FakeResolver({("o.example", "TXT"): ["v=spf1 ip4:192.0.2.1"]}))
    assert "SPF_NO_ALL" in codes(no_all) and no_all.status == "valid"


def test_ptr_and_terms_after_all():
    r = FakeResolver({("acme.example", "TXT"): ["v=spf1 ptr -all include:late.example"]})
    rep = check_spf("acme.example", r)
    assert {"SPF_PTR_USED", "SPF_TERMS_AFTER_ALL"} <= codes(rep)
    assert rep.lookups == 1  # ptr counts; include after -all is never evaluated


def test_mx_with_more_than_ten_records():
    r = FakeResolver(
        {
            ("acme.example", "TXT"): ["v=spf1 mx -all"],
            ("acme.example", "MX"): [f"10 mx{i}.acme.example" for i in range(11)],
        }
    )
    rep = check_spf("acme.example", r)
    assert "SPF_MX_TOO_MANY" in codes(rep) and rep.status == "permerror"


def test_macros_counted_but_not_resolved():
    r = FakeResolver({("acme.example", "TXT"): ["v=spf1 exists:%{i}._spf.acme.example -all"]})
    rep = check_spf("acme.example", r)
    assert rep.lookups == 1 and rep.void_lookups == 0
    assert all("%" not in name for name, _ in r.queries)


def test_dns_temp_error():
    r = FakeResolver({}, fail=[("acme.example", "TXT")])
    rep = check_spf("acme.example", r)
    assert rep.status == "temperror" and "SPF_DNS_TEMPERROR" in codes(rep)


def test_pathological_policy_stops_at_safety_cap():
    rep = check_spf("big.example", _chain(60))
    assert "SPF_WALK_ABORTED" in codes(rep) and rep.status == "permerror"
