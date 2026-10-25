from datetime import UTC, datetime, timedelta

import pytest
from enrichment import (
    PRECHECK_CHECKER,
    CheckResult,
    DNSTempError,
    EmailCheck,
    EmailPrecheck,
    EmailStatus,
    FakeResolver,
    PrecheckSeverity,
    assess_email,
    catch_all_domains,
    load_domain_list,
)

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)

RECORDS = {
    ("northwind.example", "MX"): ["20 mx2.northwind.example.", "10 mx1.northwind.example."],
    ("contoso.example", "MX"): ["10 mx.contoso.example."],
    ("parked.example", "MX"): ["0 ."],
    ("mixed.example", "MX"): ["0 .", "10 mx.mixed.example."],
    ("bare.example", "A"): ["192.0.2.10"],
    ("bare6.example", "AAAA"): ["2001:db8::1"],
    ("garbled.example", "MX"): ["mx.garbled.example"],
    ("xn--mller-kva.example", "MX"): ["10 mx.xn--mller-kva.example."],
}


def make(**kw):
    resolver = FakeResolver(
        RECORDS, nxdomain={"gone.example"}, fail={"flaky.example"}, **kw.pop("resolver_kw", {})
    )
    return EmailPrecheck(resolver, **kw), resolver


def check(email, **kw):
    pre, _ = make(**kw)
    return pre.check(email)


# --- a precheck never proves an address ----------------------------------------------


def test_clean_address_is_unknown_never_valid():
    r = check("Ada.Lovelace@Northwind.example")
    assert r.result is CheckResult.UNKNOWN
    assert r.risk == 0
    assert r.findings == []
    assert r.email == "ada.lovelace@northwind.example"
    assert r.mx_hosts == ["mx1.northwind.example", "mx2.northwind.example"]


def test_clean_precheck_alone_puts_the_address_at_checked_not_verified():
    r = check("ada@northwind.example")
    a = assess_email(r.email, "finder", [r.to_check(checked_at=NOW)], now=NOW)
    assert a.status is EmailStatus.CHECKED
    assert a.reason == "unknown"
    assert a.deciding_check.checker == PRECHECK_CHECKER
    assert a.deciding_check.provider_status == "clean"


def test_a_verifier_valid_still_verifies_next_to_a_precheck():
    r = check("ada@northwind.example")
    valid = EmailCheck(
        email="ada@northwind.example",
        checker="zerobounce",
        checked_at=NOW - timedelta(hours=1),
        result=CheckResult.VALID,
    )
    a = assess_email(r.email, "finder", [r.to_check(checked_at=NOW), valid], now=NOW)
    assert a.status is EmailStatus.VERIFIED


def test_precheck_invalid_puts_the_address_at_invalid():
    r = check("ada@parked.example")
    a = assess_email(r.email, "finder", [r.to_check(checked_at=NOW)], now=NOW)
    assert a.status is EmailStatus.INVALID
    assert a.deciding_check.provider_status == "null_mx"


# --- syntax ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "email",
    [
        "no-at-sign.example",
        "@northwind.example",
        "ada@",
        "ada..lovelace@northwind.example",
        ".ada@northwind.example",
        "ada.@northwind.example",
        '"ada lovelace"@northwind.example',
        "ada@northwind",
        "ada@-northwind.example",
        "ada@north_wind.example",
        "ada@192.0.2.1",
        "a" * 65 + "@northwind.example",
        "ada@" + "a" * 64 + ".example",
        "ada@" + ".".join(["a" * 60] * 5) + ".example",
        "adä@northwind.example",
    ],
)
def test_malformed_addresses_are_invalid_without_dns(email):
    pre, resolver = make()
    r = pre.check(email)
    assert r.result is CheckResult.INVALID
    assert r.codes() == ["syntax_invalid"]
    assert r.risk == 100
    assert resolver.queries == []


def test_plus_tags_and_dot_atoms_pass_syntax():
    assert check("ada.lovelace+outbound@northwind.example").result is CheckResult.UNKNOWN
    assert check("o'brien@northwind.example").result is CheckResult.UNKNOWN


def test_idn_domain_is_looked_up_as_its_a_label_and_flagged():
    pre, resolver = make()
    r = pre.check("ada@müller.example")
    assert r.result is CheckResult.UNKNOWN
    assert r.codes() == ["non_ascii"]
    assert r.domain == "xn--mller-kva.example"
    assert resolver.queries == [("xn--mller-kva.example", "MX")]


# --- mail host -------------------------------------------------------------------------


def test_null_mx_means_the_domain_accepts_no_mail():
    r = check("ada@parked.example")
    assert r.result is CheckResult.INVALID
    assert r.codes() == ["null_mx"]
    assert r.mx_hosts == []


def test_null_mx_mixed_with_real_mx_is_still_blocked():
    r = check("ada@mixed.example")
    assert r.result is CheckResult.INVALID
    assert "forbids" in r.findings[0].detail


def test_nxdomain_is_invalid():
    r = check("ada@gone.example")
    assert r.result is CheckResult.INVALID
    assert r.codes() == ["domain_not_found"]


@pytest.mark.parametrize("domain", ["bare.example", "bare6.example"])
def test_no_mx_but_an_address_record_is_an_implicit_mx(domain):
    r = check(f"ada@{domain}")
    assert r.result is CheckResult.UNKNOWN
    assert r.codes() == ["implicit_mx"]
    assert r.risk == 20
    assert r.mx_hosts == [domain]


def test_no_mx_and_no_address_record_is_invalid():
    r = check("ada@empty.example")
    assert r.result is CheckResult.INVALID
    assert r.codes() == ["no_mail_host"]


def test_dns_failure_is_unknown_never_invalid():
    r = check("ada@flaky.example")
    assert r.result is CheckResult.UNKNOWN
    assert r.codes() == ["dns_temp_error"]
    assert r.findings[0].severity is PrecheckSeverity.RISK


def test_temporary_failure_on_the_address_fallback_is_unknown():
    class FailOnA(FakeResolver):
        def lookup(self, name, rtype):
            if rtype == "A":
                raise DNSTempError("timeout")
            return super().lookup(name, rtype)

    r = EmailPrecheck(FailOnA({})).check("ada@empty.example")
    assert r.result is CheckResult.UNKNOWN
    assert r.codes() == ["dns_temp_error"]


def test_unparseable_mx_answer_is_unknown_not_a_host():
    r = check("ada@garbled.example")
    assert r.result is CheckResult.UNKNOWN
    assert r.codes() == ["dns_temp_error"]
    assert r.mx_hosts == []


def test_one_dns_lookup_per_domain_across_a_batch():
    pre, resolver = make()
    emails = [f"person{i}@northwind.example" for i in range(50)] + ["x@bare.example"] * 3
    reports = pre.check_many(emails)
    assert len(reports) == 53
    assert resolver.queries == [
        ("northwind.example", "MX"),
        ("bare.example", "MX"),
        ("bare.example", "A"),
    ]


# --- lists -----------------------------------------------------------------------------


def test_disposable_domain_and_its_subdomains_are_do_not_mail_without_dns():
    pre, resolver = make()
    for email in ("someone@mailinator.com", "someone@eu.mailinator.com"):
        r = pre.check(email)
        assert r.result is CheckResult.DO_NOT_MAIL
        assert r.codes() == ["disposable"]
        assert r.risk == 100
    assert resolver.queries == []


def test_disposable_list_is_replaceable(tmp_path):
    path = tmp_path / "blocklist.conf"
    path.write_text("# test list\nthrowaway.example\n\nBurner.Example.\n", encoding="utf-8")
    domains = load_domain_list(path)
    assert domains == {"throwaway.example", "burner.example"}
    r = check("ada@burner.example", disposable_domains=domains)
    assert r.result is CheckResult.DO_NOT_MAIL
    assert check("someone@mailinator.com", disposable_domains=domains).codes() != ["disposable"]


@pytest.mark.parametrize("local", ["info", "Sales", "support+eu", "no-reply"])
def test_role_addresses_add_risk_but_stay_unknown(local):
    r = check(f"{local}@northwind.example")
    assert r.result is CheckResult.UNKNOWN
    assert r.codes() == ["role_address"]
    assert r.risk == 30


def test_role_list_is_replaceable():
    assert check("info@northwind.example", role_local_parts=["ops"]).codes() == []
    assert check("ops@northwind.example", role_local_parts=["ops"]).codes() == ["role_address"]


# --- catch-all ---------------------------------------------------------------------------


def test_known_catch_all_domain_is_catch_all_and_stays_checked():
    r = check("ada@contoso.example", catch_all_domains={"Contoso.example"})
    assert r.result is CheckResult.CATCH_ALL
    assert r.codes() == ["catch_all_domain"]
    a = assess_email(r.email, "finder", [r.to_check(checked_at=NOW)], now=NOW)
    assert a.status is EmailStatus.CHECKED
    assert a.reason == "catch_all"


def test_catch_all_domains_come_from_independent_verifier_checks_only():
    def c(email, result, checker="zerobounce"):
        return EmailCheck(email=email, checker=checker, checked_at=NOW, result=result)

    checks = [
        c("bob@Contoso.example", CheckResult.CATCH_ALL),
        c("eve@northwind.example", CheckResult.VALID),
        c("x@bare.example", CheckResult.CATCH_ALL, checker=PRECHECK_CHECKER),
    ]
    assert catch_all_domains(checks) == {"contoso.example"}


def test_risks_add_up_and_cap_at_100():
    r = check("info@bare.example", catch_all_domains={"bare.example"})
    assert set(r.codes()) == {"implicit_mx", "role_address", "catch_all_domain"}
    assert r.risk == 90
    assert r.result is CheckResult.CATCH_ALL
    assert r.to_check(checked_at=NOW).provider_status == "implicit_mx,role_address,catch_all_domain"


def test_block_wins_over_catch_all_and_role():
    r = check("info@parked.example", catch_all_domains={"parked.example"})
    assert r.result is CheckResult.INVALID
    assert r.risk == 100
