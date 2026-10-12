"""DKIM public-key record check (RFC 6376 §3.6, RFC 8301, RFC 8463).

``check_dkim(domain, selector, resolver)`` fetches
``<selector>._domainkey.<domain>`` (RFC 6376 §3.6.2.1) and explains the key
record a receiver would use to verify mail signed with ``d=<domain>;
s=<selector>``:

- missing / ambiguous / malformed records, ``v=`` other than ``DKIM1``;
- revoked keys (empty ``p=``, §3.6.1);
- RSA keys under 1024 bits (verifiers MUST NOT accept them, RFC 8301 §3.2)
  and under 2048 bits (signers SHOULD use 2048, RFC 8301 §3.2);
- ``h=`` that allows only SHA-1 (``rsa-sha1`` MUST NOT be used, RFC 8301 §3.1);
- ``s=`` that excludes email, ``t=y`` testing mode;
- Ed25519 keys (RFC 8463) of the wrong length.

The key length is read from the DER structure without any crypto dependency.
Nothing here verifies a signature; it only checks what is published.
"""

from __future__ import annotations

import base64
import binascii
from typing import Literal

from pydantic import BaseModel

from .dns import DNSTempError, Resolver, normalize_name
from .findings import Finding

DkimStatus = Literal["valid", "revoked", "invalid", "none", "temperror"]

# rsaEncryption OID 1.2.840.113549.1.1.1, DER-encoded
_RSA_OID = bytes.fromhex("2a864886f70d010101")
ED25519_KEY_BYTES = 32


class DkimKeyError(ValueError):
    """The ``p=`` value is not a usable public key."""


class DkimKeyRecord(BaseModel):
    raw: str
    version: str | None = None
    key_type: str = "rsa"
    hash_algs: list[str] | None = None  # None = all algorithms allowed
    service_types: list[str] = ["*"]
    flags: list[str] = []
    public_key: str = ""
    tags: dict[str, str] = {}

    @property
    def revoked(self) -> bool:
        return self.public_key == ""

    @property
    def testing(self) -> bool:
        return "y" in self.flags


class DkimReport(BaseModel):
    domain: str
    selector: str
    name: str
    status: DkimStatus
    record: DkimKeyRecord | None = None
    key_bits: int | None = None
    findings: list[Finding] = []


def _f(code: str, severity: str, message: str, ref: str) -> Finding:
    return Finding(code=code, severity=severity, message=message, ref=ref)


def _colon_list(value: str) -> list[str]:
    return [v.strip().lower() for v in value.split(":") if v.strip()]


def parse_dkim_key(raw: str) -> DkimKeyRecord:
    """Parse a DKIM key TXT string (tag=value list, RFC 6376 §3.2, §3.6.1).

    Raises ``DkimKeyError`` for duplicate tags, a ``v=`` that is not the first
    tag or not ``DKIM1``, or a missing ``p=`` tag. Unknown tags are kept.
    """
    tags: dict[str, str] = {}
    order: list[str] = []
    for part in raw.split(";"):
        part = part.strip()
        if not part:
            continue
        key, sep, value = part.partition("=")
        key = key.strip().lower()
        if not sep or not key:
            raise DkimKeyError(f"malformed tag {part!r}")
        if key in tags:
            raise DkimKeyError(f"tag {key!r} appears more than once")
        # whitespace inside values (e.g. a p= split over strings) is not significant
        tags[key] = "".join(value.split())
        order.append(key)
    if "v" in tags and (order[0] != "v" or tags["v"] != "DKIM1"):
        raise DkimKeyError("v= must be the first tag and equal 'DKIM1'")
    if "p" not in tags:
        raise DkimKeyError("record has no p= tag")
    rec = DkimKeyRecord(raw=raw, version=tags.get("v"), public_key=tags["p"], tags=tags)
    if "k" in tags:
        rec.key_type = tags["k"].lower()
    if "h" in tags:
        rec.hash_algs = _colon_list(tags["h"])
    if "s" in tags:
        rec.service_types = _colon_list(tags["s"])
    if "t" in tags:
        rec.flags = _colon_list(tags["t"])
    return rec


def _der_read(data: bytes, pos: int) -> tuple[int, bytes, int]:
    """Read one DER TLV at ``pos``; return (tag, value, next_pos)."""
    try:
        tag = data[pos]
        length = data[pos + 1]
        pos += 2
        if length & 0x80:
            n = length & 0x7F
            if n == 0 or n > 4:
                raise DkimKeyError("unsupported DER length")
            length = int.from_bytes(data[pos : pos + n], "big")
            pos += n
    except IndexError as exc:
        raise DkimKeyError("truncated DER") from exc
    end = pos + length
    if end > len(data):
        raise DkimKeyError("truncated DER")
    return tag, data[pos:end], end


def _rsa_public_key_bits(rsa_public_key: bytes) -> int:
    tag, body, _ = _der_read(rsa_public_key, 0)
    if tag != 0x30:
        raise DkimKeyError("RSAPublicKey is not a SEQUENCE")
    tag, modulus, _ = _der_read(body, 0)
    if tag != 0x02:
        raise DkimKeyError("RSA modulus is not an INTEGER")
    return int.from_bytes(modulus, "big").bit_length()


def rsa_key_bits(der: bytes) -> int:
    """Modulus size of an RSA key given as SubjectPublicKeyInfo or RSAPublicKey DER.

    RFC 6376 §3.6.1 says RSAPublicKey, but published keys are almost always
    SubjectPublicKeyInfo (as OpenSSL writes them); both are accepted.
    """
    tag, body, _ = _der_read(der, 0)
    if tag != 0x30:
        raise DkimKeyError("key is not a DER SEQUENCE")
    first_tag, first, after = _der_read(body, 0)
    if first_tag == 0x02:  # bare RSAPublicKey: SEQUENCE { modulus, exponent }
        return int.from_bytes(first, "big").bit_length()
    if first_tag != 0x30:
        raise DkimKeyError("unrecognised key structure")
    oid_tag, oid, _ = _der_read(first, 0)
    if oid_tag != 0x06 or oid != _RSA_OID:
        raise DkimKeyError("SubjectPublicKeyInfo is not an rsaEncryption key")
    bits_tag, bit_string, _ = _der_read(body, after)
    if bits_tag != 0x03 or not bit_string or bit_string[0] != 0:
        raise DkimKeyError("bad subjectPublicKey BIT STRING")
    return _rsa_public_key_bits(bit_string[1:])


def _decode_p(value: str) -> bytes:
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise DkimKeyError("p= is not valid base64") from exc


def check_dkim(
    domain: str,
    selector: str,
    resolver: Resolver,
    *,
    min_rsa_bits: int = 1024,
    recommended_rsa_bits: int = 2048,
) -> DkimReport:
    """Explain the DKIM key published for ``d=domain; s=selector``."""
    domain, selector = normalize_name(domain), normalize_name(selector)
    name = f"{selector}._domainkey.{domain}"
    rep = DkimReport(domain=domain, selector=selector, name=name, status="none")
    add = rep.findings.append

    try:
        txts = resolver.lookup(name, "TXT")
    except DNSTempError as exc:
        add(_f("DKIM_DNS_TEMPERROR", "error", str(exc), "RFC 6376 §6.1.2"))
        rep.status = "temperror"
        return rep
    # a key record may omit v=; skip only TXT strings that clearly belong to something else
    candidates = [t for t in txts if "p=" in t.replace(" ", "")]
    if not candidates:
        add(
            _f(
                "DKIM_KEY_MISSING",
                "error",
                f"no DKIM key at {name}: signatures with s={selector} cannot verify",
                "RFC 6376 §3.6.2.1",
            )
        )
        return rep
    if len(candidates) > 1:
        add(
            _f(
                "DKIM_MULTIPLE_RECORDS",
                "warning",
                f"{name} has {len(candidates)} key records; verifiers may pick any of them",
                "RFC 6376 §3.6.2.2",
            )
        )
    try:
        rec = parse_dkim_key(candidates[0])
    except DkimKeyError as exc:
        add(_f("DKIM_SYNTAX", "error", f"{name}: {exc}", "RFC 6376 §3.6.1"))
        rep.status = "invalid"
        return rep
    rep.record = rec

    if rec.revoked:
        add(
            _f(
                "DKIM_KEY_REVOKED",
                "error",
                f"{name} has an empty p=: the key is revoked and signatures fail",
                "RFC 6376 §3.6.1",
            )
        )
        rep.status = "revoked"
        return rep

    invalid = False
    if rec.key_type == "rsa":
        try:
            rep.key_bits = rsa_key_bits(_decode_p(rec.public_key))
        except DkimKeyError as exc:
            add(_f("DKIM_KEY_MALFORMED", "error", f"{name}: {exc}", "RFC 6376 §3.6.1"))
            invalid = True
        else:
            if rep.key_bits < min_rsa_bits:
                add(
                    _f(
                        "DKIM_RSA_TOO_SHORT",
                        "error",
                        f"{rep.key_bits}-bit RSA key: verifiers must not accept keys under "
                        f"{min_rsa_bits} bits",
                        "RFC 8301 §3.2",
                    )
                )
                invalid = True
            elif rep.key_bits < recommended_rsa_bits:
                add(
                    _f(
                        "DKIM_RSA_BELOW_RECOMMENDED",
                        "warning",
                        f"{rep.key_bits}-bit RSA key; rotate to {recommended_rsa_bits} bits",
                        "RFC 8301 §3.2",
                    )
                )
    elif rec.key_type == "ed25519":
        try:
            key = _decode_p(rec.public_key)
        except DkimKeyError as exc:
            add(_f("DKIM_KEY_MALFORMED", "error", f"{name}: {exc}", "RFC 8463 §4.2"))
            invalid = True
        else:
            if len(key) != ED25519_KEY_BYTES:
                add(
                    _f(
                        "DKIM_KEY_MALFORMED",
                        "error",
                        f"{name}: Ed25519 key is {len(key)} bytes, expected {ED25519_KEY_BYTES}",
                        "RFC 8463 §4.2",
                    )
                )
                invalid = True
            else:
                rep.key_bits = 256
                add(
                    _f(
                        "DKIM_ED25519_KEY",
                        "info",
                        "Ed25519 key: not every receiver verifies Ed25519, so make sure an RSA "
                        "selector signs the same mail",
                        "RFC 8463",
                    )
                )
    else:
        add(
            _f(
                "DKIM_UNKNOWN_KEY_TYPE",
                "error",
                f"k={rec.key_type}: verifiers ignore keys of unknown type",
                "RFC 6376 §3.6.1",
            )
        )
        invalid = True

    if rec.hash_algs is not None and "sha256" not in rec.hash_algs:
        add(
            _f(
                "DKIM_NO_SHA256",
                "error",
                f"h={':'.join(rec.hash_algs)} does not allow sha256; rsa-sha1 must not be used",
                "RFC 8301 §3.1",
            )
        )
        invalid = True
    if not {"*", "email"} & set(rec.service_types):
        add(
            _f(
                "DKIM_SERVICE_NOT_EMAIL",
                "error",
                f"s={':'.join(rec.service_types)} excludes email",
                "RFC 6376 §3.6.1",
            )
        )
        invalid = True
    if rec.testing:
        add(
            _f(
                "DKIM_TESTING_MODE",
                "warning",
                "t=y: the domain is testing DKIM, so verifiers treat its mail like unsigned "
                "mail; remove the flag once signing works",
                "RFC 6376 §3.6.1",
            )
        )

    rep.status = "invalid" if invalid else "valid"
    return rep


def check_dkim_selectors(
    domain: str, selectors: list[str], resolver: Resolver, **kwargs: int
) -> list[DkimReport]:
    """``check_dkim`` for each selector the sending platform signs with."""
    return [check_dkim(domain, s, resolver, **kwargs) for s in selectors]
