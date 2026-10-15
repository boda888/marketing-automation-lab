from datetime import UTC, datetime

import pytest
from deliverability_preflight import (
    DkimSignatureError,
    check_dkim_signature,
    dkim_signatures,
    parse_dkim_signature,
)

GOOD = (
    "v=1; a=rsa-sha256; c=relaxed/relaxed; d=Acme.Example; s=S1;\r\n"
    "\th=from:to:subject:date; i=news@mail.acme.example; t=1760000000; x=1760600000;\r\n"
    "\tbh=2jUSOH9NhtVGCQWNr9BrIAPreKQjO6Sn7XIkfJVOzv8=; b=dGVzdA ==\r\n"
)
NOW = datetime(2025, 10, 10, tzinfo=UTC)  # 1760054400, between t= and x=


def codes(rep):
    return {f.code for f in rep.findings}


def with_tag(tag, value):
    tags = [p for p in GOOD.split(";") if p.strip().partition("=")[0].strip() != tag]
    if value is not None:
        tags.append(f" {tag}={value}")
    return ";".join(tags)


def test_parse_good_signature():
    sig = parse_dkim_signature(GOOD)
    assert (sig.domain, sig.selector) == ("acme.example", "s1")
    assert sig.algorithm == "rsa-sha256"
    assert sig.signed_headers == ["from", "to", "subject", "date"]
    assert (sig.header_canon, sig.body_canon) == ("relaxed", "relaxed")
    assert (sig.timestamp, sig.expires) == (1760000000, 1760600000)
    assert sig.tags["b"] == "dGVzdA=="  # folding whitespace removed


def test_good_signature_has_no_findings():
    rep = check_dkim_signature(GOOD, now=NOW)
    assert rep.status == "ok" and rep.findings == []
    assert rep.key_ref == ("acme.example", "s1")


def test_canonicalization_defaults_and_body_part():
    sig = parse_dkim_signature(with_tag("c", None))
    assert (sig.header_canon, sig.body_canon) == ("simple", "simple")
    sig = parse_dkim_signature(with_tag("c", "relaxed"))
    assert (sig.header_canon, sig.body_canon) == ("relaxed", "simple")


@pytest.mark.parametrize(
    "raw",
    ["v=1; a=rsa-sha256; d=a.example; d=b.example", "v=1; garbage", "v=1; l=12x"],
)
def test_parse_errors(raw):
    with pytest.raises(DkimSignatureError):
        parse_dkim_signature(raw)
    rep = check_dkim_signature(raw)
    assert rep.status == "invalid" and codes(rep) == {"DKIM_SIG_SYNTAX"}
    assert rep.key_ref is None


@pytest.mark.parametrize("tag", ["v", "a", "b", "bh", "d", "h", "s"])
def test_every_required_tag_is_checked(tag):
    rep = check_dkim_signature(with_tag(tag, None), now=NOW)
    assert "DKIM_SIG_MISSING_TAG" in codes(rep)
    assert f"{tag}=" in rep.findings[0].message
    assert rep.status == "invalid"


def test_empty_b_counts_as_missing():
    assert "DKIM_SIG_MISSING_TAG" in codes(check_dkim_signature(with_tag("b", ""), now=NOW))


@pytest.mark.parametrize(
    ("tag", "value", "code"),
    [
        ("v", "2", "DKIM_SIG_VERSION"),
        ("a", "rsa-sha1", "DKIM_SIG_SHA1"),
        ("a", "hmac-md5", "DKIM_SIG_ALGORITHM"),
        ("h", "to:subject", "DKIM_SIG_FROM_NOT_SIGNED"),
        ("i", "@esp.example", "DKIM_SIG_IDENTITY_MISMATCH"),
        ("i", "x@notacme.example", "DKIM_SIG_IDENTITY_MISMATCH"),
        ("c", "relaxed/loose", "DKIM_SIG_CANONICALIZATION"),
        ("x", "1760000000", "DKIM_SIG_EXPIRY_ORDER"),
    ],
)
def test_errors(tag, value, code):
    rep = check_dkim_signature(with_tag(tag, value), now=NOW)
    assert code in codes(rep)
    assert rep.status == "invalid"


def test_ed25519_and_identity_forms_are_accepted():
    for tag, value in [("a", "ed25519-sha256"), ("i", "@acme.example"), ("i", "@ACME.example")]:
        assert check_dkim_signature(with_tag(tag, value), now=NOW).findings == []


def test_from_match_is_case_insensitive():
    assert check_dkim_signature(with_tag("h", "From:To"), now=NOW).findings == []


def test_expired_signature():
    later = datetime(2026, 1, 1, tzinfo=UTC)
    rep = check_dkim_signature(GOOD, now=later)
    assert codes(rep) == {"DKIM_SIG_EXPIRED"}


def test_body_length_is_a_warning():
    rep = check_dkim_signature(with_tag("l", "200"), now=NOW)
    assert codes(rep) == {"DKIM_SIG_BODY_LENGTH"}
    assert rep.status == "ok"
    assert rep.signature.body_length == 200


def test_dkim_signatures_picks_every_header():
    headers = [("From", "a@acme.example"), ("dkim-signature", "v=1"), ("DKIM-Signature", "v=2")]
    assert dkim_signatures(headers) == ["v=1", "v=2"]
