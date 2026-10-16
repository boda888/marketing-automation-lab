"""DMARCbis (draft-ietf-dmarc-dmarcbis-41) policy rules: t, pct, np, invalid p."""

import pytest
from deliverability_preflight import FakeResolver, check_dmarc, parse_dmarc


def codes(report):
    return {f.code for f in report.findings}


def one(name, record):
    return FakeResolver({(f"_dmarc.{name}", "TXT"): [record]})


RUA = "rua=mailto:d@acme.example"


def test_parse_keeps_record_with_invalid_policy_tags():
    rec = parse_dmarc("v=DMARC1; p=block; sp=reject; np=maybe; t=yes; " + RUA)
    assert rec.p is None and rec.sp == "reject" and rec.np is None and rec.t == "n"
    assert rec.invalid_tags == {"p": "block", "np": "maybe", "t": "yes"}


def test_parse_t_and_np():
    rec = parse_dmarc("v=DMARC1; p=reject; np=Reject; t=Y")
    assert (rec.t, rec.np, rec.invalid_tags) == ("y", "reject", {})


@pytest.mark.parametrize(
    ("record", "code"),
    [
        ("v=DMARC1; " + RUA, "DMARC_POLICY_FALLBACK_NONE"),
        ("v=DMARC1; p=block; " + RUA, "DMARC_POLICY_FALLBACK_NONE"),
        # an invalid sp makes the whole record count as p=none, even with p=reject
        ("v=DMARC1; p=reject; sp=strict; " + RUA, "DMARC_POLICY_FALLBACK_NONE"),
        ("v=DMARC1; p=reject; np=strict; " + RUA, "DMARC_POLICY_FALLBACK_NONE"),
    ],
)
def test_invalid_or_missing_p_with_valid_rua_acts_as_none(record, code):
    rep = check_dmarc("acme.example", one("acme.example", record))
    assert rep.status == "valid"
    assert rep.effective_policy == "none" and rep.requested_policy == "none"
    assert code in codes(rep)
    assert "DMARC_POLICY_NONE" not in codes(rep)  # one finding, not two
    assert next(f for f in rep.findings if f.code == code).severity == "warning"


@pytest.mark.parametrize(
    ("record", "code"),
    [
        ("v=DMARC1; p=block", "DMARC_INVALID_POLICY"),
        ("v=DMARC1; p=block; rua=d-at-acme.example", "DMARC_INVALID_POLICY"),
        ("v=DMARC1; p=reject; sp=strict; rua=mailto:nobody", "DMARC_INVALID_POLICY"),
        ("v=DMARC1; rua=", "DMARC_NO_POLICY"),
    ],
)
def test_invalid_p_without_valid_rua_means_no_dmarc(record, code):
    rep = check_dmarc("acme.example", one("acme.example", record))
    assert rep.status == "invalid" and rep.effective_policy is None
    assert next(f for f in rep.findings if f.code == code).severity == "error"


@pytest.mark.parametrize(
    ("p", "applied"),
    [("reject", "quarantine"), ("quarantine", "none")],
)
def test_testing_flag_steps_policy_down_one_level(p, applied):
    rep = check_dmarc("acme.example", one("acme.example", f"v=DMARC1; p={p}; t=y; {RUA}"))
    assert rep.requested_policy == p and rep.effective_policy == applied
    assert "DMARC_TESTING_MODE" in codes(rep)
    assert ("DMARC_POLICY_NONE" in codes(rep)) == (applied == "none")


def test_testing_flag_has_no_effect_on_none_and_t_n_is_default():
    none = check_dmarc("acme.example", one("acme.example", f"v=DMARC1; p=none; t=y; {RUA}"))
    assert none.effective_policy == "none" and "DMARC_TESTING_MODE" not in codes(none)
    n = check_dmarc("acme.example", one("acme.example", f"v=DMARC1; p=reject; t=n; {RUA}"))
    assert n.effective_policy == "reject" and n.findings == []


def test_testing_flag_applies_to_inherited_sp():
    r = one("acme.example", f"v=DMARC1; p=reject; sp=reject; t=y; {RUA}")
    rep = check_dmarc("news.acme.example", r)
    assert rep.requested_policy == "reject" and rep.effective_policy == "quarantine"


def test_invalid_t_value_falls_back_to_n():
    rep = check_dmarc("acme.example", one("acme.example", f"v=DMARC1; p=reject; t=1; {RUA}"))
    assert rep.status == "valid" and rep.effective_policy == "reject"
    assert "DMARC_INVALID_TAG" in codes(rep)


def test_pct_is_reported_as_historic():
    rep = check_dmarc("acme.example", one("acme.example", f"v=DMARC1; p=reject; pct=0; {RUA}"))
    # DMARCbis receivers ignore pct, so the applied policy is unchanged
    assert rep.effective_policy == "reject"
    msg = next(f.message for f in rep.findings if f.code == "DMARC_PCT_PARTIAL")
    assert "historic" in msg and "t=y" in msg


NP_RECORD = f"v=DMARC1; p=reject; sp=quarantine; np=reject; {RUA}"


def test_np_applies_to_non_existent_subdomain():
    r = one("acme.example", NP_RECORD)
    gone = check_dmarc("ghost.acme.example", r, author_domain_exists=False)
    assert gone.effective_policy == "reject"
    real = check_dmarc("news.acme.example", r, author_domain_exists=True)
    assert real.effective_policy == "quarantine"
    assert "DMARC_NP_IF_NXDOMAIN" not in codes(real) | codes(gone)


def test_np_unknown_existence_uses_sp_and_reports_np():
    rep = check_dmarc("news.acme.example", one("acme.example", NP_RECORD))
    assert rep.effective_policy == "quarantine"
    assert "DMARC_NP_IF_NXDOMAIN" in codes(rep)


def test_np_absent_falls_back_to_sp_then_p():
    sp = one("acme.example", f"v=DMARC1; p=reject; sp=none; {RUA}")
    assert check_dmarc("x.acme.example", sp, author_domain_exists=False).effective_policy == "none"
    p = one("acme.example", f"v=DMARC1; p=quarantine; {RUA}")
    assert (
        check_dmarc("x.acme.example", p, author_domain_exists=False).effective_policy
        == "quarantine"
    )


def test_np_does_not_apply_to_the_author_domain_itself():
    r = one("acme.example", f"v=DMARC1; p=quarantine; np=reject; {RUA}")
    rep = check_dmarc("acme.example", r, author_domain_exists=False)
    assert rep.effective_policy == "quarantine" and "DMARC_NP_IF_NXDOMAIN" not in codes(rep)
