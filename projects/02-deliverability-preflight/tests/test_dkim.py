import base64

import pytest
from deliverability_preflight import (
    DkimKeyError,
    FakeResolver,
    check_dkim,
    check_dkim_selectors,
    parse_dkim_key,
    rsa_key_bits,
)

NAME = "s1._domainkey.acme.example"


def _tlv(tag: int, body: bytes) -> bytes:
    n = len(body)
    if n < 0x80:
        length = bytes([n])
    else:
        raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
        length = bytes([0x80 | len(raw)]) + raw
    return bytes([tag]) + length + body


def _int(value: int) -> bytes:
    raw = value.to_bytes(value.bit_length() // 8 + 1, "big")  # leading 0x00 keeps it positive
    return _tlv(0x02, raw)


def rsa_public_key(bits: int) -> bytes:
    modulus = (1 << (bits - 1)) | 1  # synthetic odd modulus of exactly `bits` bits
    return _tlv(0x30, _int(modulus) + _int(65537))


def spki(bits: int) -> bytes:
    alg = _tlv(0x30, _tlv(0x06, bytes.fromhex("2a864886f70d010101")) + b"\x05\x00")
    return _tlv(0x30, alg + _tlv(0x03, b"\x00" + rsa_public_key(bits)))


def b64(der: bytes) -> str:
    return base64.b64encode(der).decode()


def codes(rep):
    return {f.code for f in rep.findings}


def dns(*txts: str) -> FakeResolver:
    return FakeResolver({(NAME, "TXT"): list(txts)})


@pytest.mark.parametrize("bits", [512, 1024, 2048, 4096])
def test_rsa_key_bits_spki_and_bare(bits):
    assert rsa_key_bits(spki(bits)) == bits
    assert rsa_key_bits(rsa_public_key(bits)) == bits


@pytest.mark.parametrize(
    "der",
    [b"", b"\x04\x00", b"\x30\x05\x02\x01", spki(2048)[:-10]],
)
def test_rsa_key_bits_rejects_garbage(der):
    with pytest.raises(DkimKeyError):
        rsa_key_bits(der)


def test_rsa_key_bits_rejects_non_rsa_spki():
    alg = _tlv(0x30, _tlv(0x06, bytes.fromhex("2b6570")))  # Ed25519 OID
    with pytest.raises(DkimKeyError, match="rsaEncryption"):
        rsa_key_bits(_tlv(0x30, alg + _tlv(0x03, b"\x00" + b"k" * 32)))


def test_parse_defaults_and_tags():
    rec = parse_dkim_key("v=DKIM1; h=sha256 : sha1; s=email; t=y:s; n=hi; p=AB CD")
    assert rec.key_type == "rsa" and rec.version == "DKIM1"
    assert rec.hash_algs == ["sha256", "sha1"] and rec.service_types == ["email"]
    assert rec.flags == ["y", "s"] and rec.testing
    assert rec.public_key == "ABCD" and rec.tags["n"] == "hi"


def test_parse_without_version_is_allowed():
    rec = parse_dkim_key("k=rsa; p=AAAA")
    assert rec.version is None and rec.hash_algs is None and rec.service_types == ["*"]


@pytest.mark.parametrize(
    "raw",
    [
        "k=rsa; v=DKIM1; p=AAAA",
        "v=DKIM2; p=AAAA",
        "v=DKIM1; k=rsa",
        "v=DKIM1; p=A; p=B",
        "v=DKIM1; x",
    ],
)
def test_parse_rejects(raw):
    with pytest.raises(DkimKeyError):
        parse_dkim_key(raw)


def test_2048_bit_key_is_clean():
    rep = check_dkim("ACME.example.", "S1", dns(f"v=DKIM1; k=rsa; p={b64(spki(2048))}"))
    assert rep.name == NAME
    assert rep.status == "valid" and rep.key_bits == 2048 and rep.findings == []


def test_bare_rsapublickey_is_accepted():
    rep = check_dkim("acme.example", "s1", dns(f"p={b64(rsa_public_key(2048))}"))
    assert rep.status == "valid" and rep.key_bits == 2048


def test_1024_bit_key_warns():
    rep = check_dkim("acme.example", "s1", dns(f"v=DKIM1; p={b64(spki(1024))}"))
    assert rep.status == "valid" and codes(rep) == {"DKIM_RSA_BELOW_RECOMMENDED"}


def test_short_key_is_invalid():
    rep = check_dkim("acme.example", "s1", dns(f"v=DKIM1; p={b64(spki(768))}"))
    assert rep.status == "invalid" and rep.key_bits == 768
    assert codes(rep) == {"DKIM_RSA_TOO_SHORT"}


def test_thresholds_are_configurable():
    rep = check_dkim("acme.example", "s1", dns(f"p={b64(spki(2048))}"), recommended_rsa_bits=3072)
    assert codes(rep) == {"DKIM_RSA_BELOW_RECOMMENDED"}


def test_key_split_over_txt_strings_with_spaces():
    key = b64(spki(2048))
    rep = check_dkim("acme.example", "s1", dns(f"v=DKIM1; p={key[:100]} {key[100:]}"))
    assert rep.status == "valid" and rep.key_bits == 2048


def test_missing_key():
    rep = check_dkim("acme.example", "s1", FakeResolver({}))
    assert rep.status == "none" and codes(rep) == {"DKIM_KEY_MISSING"}


def test_unrelated_txt_is_not_a_key():
    rep = check_dkim("acme.example", "s1", dns("v=spf1 -all"))
    assert codes(rep) == {"DKIM_KEY_MISSING"}


def test_revoked_key():
    rep = check_dkim("acme.example", "s1", dns("v=DKIM1; k=rsa; p="))
    assert rep.status == "revoked" and codes(rep) == {"DKIM_KEY_REVOKED"}


def test_syntax_error():
    rep = check_dkim("acme.example", "s1", dns("k=rsa; v=DKIM1; p=AAAA"))
    assert rep.status == "invalid" and codes(rep) == {"DKIM_SYNTAX"}


def test_malformed_base64_and_der():
    assert codes(check_dkim("acme.example", "s1", dns("p=not*base64"))) == {"DKIM_KEY_MALFORMED"}
    assert codes(check_dkim("acme.example", "s1", dns("p=AAAA"))) == {"DKIM_KEY_MALFORMED"}


def test_sha1_only_hash():
    rep = check_dkim("acme.example", "s1", dns(f"v=DKIM1; h=sha1; p={b64(spki(2048))}"))
    assert rep.status == "invalid" and codes(rep) == {"DKIM_NO_SHA256"}


def test_service_type_and_testing_flag():
    rep = check_dkim("acme.example", "s1", dns(f"v=DKIM1; s=other; t=y; p={b64(spki(2048))}"))
    assert rep.status == "invalid"
    assert codes(rep) == {"DKIM_SERVICE_NOT_EMAIL", "DKIM_TESTING_MODE"}


def test_ed25519_key():
    rep = check_dkim("acme.example", "s1", dns(f"v=DKIM1; k=ed25519; p={b64(b'k' * 32)}"))
    assert rep.status == "valid" and rep.key_bits == 256
    assert codes(rep) == {"DKIM_ED25519_KEY"}
    bad = check_dkim("acme.example", "s1", dns(f"v=DKIM1; k=ed25519; p={b64(b'k' * 31)}"))
    assert bad.status == "invalid" and codes(bad) == {"DKIM_KEY_MALFORMED"}


def test_unknown_key_type():
    rep = check_dkim("acme.example", "s1", dns("v=DKIM1; k=dsa; p=AAAA"))
    assert rep.status == "invalid" and codes(rep) == {"DKIM_UNKNOWN_KEY_TYPE"}


def test_multiple_records_warn():
    key = b64(spki(2048))
    rep = check_dkim("acme.example", "s1", dns(f"p={key}", f"v=DKIM1; p={key}"))
    assert rep.status == "valid" and codes(rep) == {"DKIM_MULTIPLE_RECORDS"}


def test_temperror():
    rep = check_dkim("acme.example", "s1", FakeResolver({}, fail=[(NAME, "TXT")]))
    assert rep.status == "temperror" and codes(rep) == {"DKIM_DNS_TEMPERROR"}


def test_several_selectors():
    r = FakeResolver({(NAME, "TXT"): [f"p={b64(spki(2048))}"]})
    reps = check_dkim_selectors("acme.example", ["s1", "s2"], r)
    assert [x.status for x in reps] == ["valid", "none"]
    assert r.queries == [(NAME, "TXT"), ("s2._domainkey.acme.example", "TXT")]
