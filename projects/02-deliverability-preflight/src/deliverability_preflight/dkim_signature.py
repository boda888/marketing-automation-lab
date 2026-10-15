"""DKIM-Signature header check (RFC 6376 §3.5, §5.4, §6.1.1; RFC 8301; RFC 8463).

``check_dkim_signature(value)`` reads the ``DKIM-Signature`` header a signer
(the ESP or your MTA) put on a test message and explains what a verifier
would reject before any cryptography runs:

- tag-list syntax errors and duplicate tags (§3.2);
- a missing required tag — ``v a b bh d h s`` (§6.1.1);
- ``v=`` other than ``1``; an algorithm other than ``rsa-sha256`` /
  ``ed25519-sha256`` (``rsa-sha1`` MUST NOT be used, RFC 8301 §3.1);
- ``h=`` without ``from`` — the From header MUST be signed (§5.4);
- ``i=`` whose domain is not ``d=`` or a subdomain of it (§3.5);
- ``x=`` not after ``t=`` (§3.5), or already past at check time;
- ``c=`` other than ``simple`` / ``relaxed``;
- ``l=`` (a body-length limit lets anyone append content, §8.2) as a warning.

It also gives ``d=`` and ``s=``, so ``preflight()`` can check the key the
signature really points at instead of a selector typed in by hand. Nothing
here verifies the signature; ``b=`` and ``bh=`` are only checked to be there.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel

from .dns import normalize_name
from .findings import Finding

REQUIRED_TAGS = ("v", "a", "b", "bh", "d", "h", "s")
ALGORITHMS = ("rsa-sha256", "ed25519-sha256")
CANONICALIZATIONS = ("simple", "relaxed")

SignatureStatus = Literal["ok", "invalid"]


class DkimSignatureError(ValueError):
    """The header is not a parseable DKIM tag-list."""


class DkimSignature(BaseModel):
    raw: str
    version: str | None = None
    algorithm: str | None = None
    domain: str | None = None  # d=
    selector: str | None = None  # s=
    signed_headers: list[str] = []  # h=, lower-cased
    identity: str | None = None  # i=
    header_canon: str = "simple"
    body_canon: str = "simple"
    body_length: int | None = None  # l=
    timestamp: int | None = None  # t=, seconds since the epoch
    expires: int | None = None  # x=
    tags: dict[str, str] = {}


class DkimSignatureReport(BaseModel):
    status: SignatureStatus
    signature: DkimSignature | None = None
    findings: list[Finding] = []

    @property
    def key_ref(self) -> tuple[str, str] | None:
        """``(d, s)`` of the key a verifier would fetch, if both are present."""
        sig = self.signature
        if sig is None or not sig.domain or not sig.selector:
            return None
        return sig.domain, sig.selector


def _f(code: str, severity: str, message: str, ref: str) -> Finding:
    return Finding(code=code, severity=severity, message=message, ref=ref)


def _int(tags: dict[str, str], key: str) -> int | None:
    value = tags.get(key)
    if value is None:
        return None
    if not value.isdigit():
        raise DkimSignatureError(f"{key}= must be a decimal number, got {value!r}")
    return int(value)


def parse_dkim_signature(raw: str) -> DkimSignature:
    """Parse a DKIM-Signature header value (tag=value list, RFC 6376 §3.2, §3.5).

    Raises ``DkimSignatureError`` for malformed or duplicate tags and for
    non-numeric ``l=`` / ``t=`` / ``x=``. Missing tags are not an error here;
    ``check_dkim_signature`` reports them.
    """
    tags: dict[str, str] = {}
    for part in raw.split(";"):
        part = part.strip()
        if not part:
            continue
        key, sep, value = part.partition("=")
        key = key.strip().lower()
        if not sep or not key:
            raise DkimSignatureError(f"malformed tag {part!r}")
        if key in tags:
            raise DkimSignatureError(f"tag {key!r} appears more than once")
        # folding whitespace inside values (b=, bh=, h=) is not significant
        tags[key] = "".join(value.split())

    sig = DkimSignature(
        raw=raw,
        version=tags.get("v"),
        algorithm=tags["a"].lower() if "a" in tags else None,
        domain=normalize_name(tags["d"]) if tags.get("d") else None,
        selector=normalize_name(tags["s"]) if tags.get("s") else None,
        signed_headers=[h.lower() for h in tags.get("h", "").split(":") if h],
        identity=tags.get("i") or None,
        body_length=_int(tags, "l"),
        timestamp=_int(tags, "t"),
        expires=_int(tags, "x"),
        tags=tags,
    )
    if "c" in tags:
        header, _, body = tags["c"].lower().partition("/")
        sig.header_canon = header
        sig.body_canon = body or "simple"
    return sig


def _in_domain(identity: str, domain: str) -> bool:
    host = normalize_name(identity.rpartition("@")[2])
    return host == domain or host.endswith("." + domain)


def check_dkim_signature(raw: str, *, now: datetime | None = None) -> DkimSignatureReport:
    """Explain what a verifier would reject in one DKIM-Signature header value.

    ``now`` (default: current UTC time) is compared with ``x=``.
    """
    try:
        sig = parse_dkim_signature(raw)
    except DkimSignatureError as exc:
        return DkimSignatureReport(
            status="invalid",
            findings=[_f("DKIM_SIG_SYNTAX", "error", str(exc), "RFC 6376 §3.2")],
        )
    rep = DkimSignatureReport(status="ok", signature=sig)
    add = rep.findings.append

    missing = [t for t in REQUIRED_TAGS if not sig.tags.get(t)]
    if missing:
        add(
            _f(
                "DKIM_SIG_MISSING_TAG",
                "error",
                f"DKIM-Signature lacks required tag(s): {', '.join(t + '=' for t in missing)}",
                "RFC 6376 §6.1.1",
            )
        )
    if sig.version is not None and sig.version != "1":
        add(_f("DKIM_SIG_VERSION", "error", f"v={sig.version}; must be 1", "RFC 6376 §3.5"))

    if sig.algorithm == "rsa-sha1":
        add(
            _f(
                "DKIM_SIG_SHA1",
                "error",
                "a=rsa-sha1 must not be used; sign with rsa-sha256",
                "RFC 8301 §3.1",
            )
        )
    elif sig.algorithm and sig.algorithm not in ALGORITHMS:
        add(
            _f(
                "DKIM_SIG_ALGORITHM",
                "error",
                f"a={sig.algorithm} is not a DKIM signing algorithm verifiers support",
                "RFC 6376 §3.3",
            )
        )

    if sig.tags.get("h") and "from" not in sig.signed_headers:
        add(
            _f(
                "DKIM_SIG_FROM_NOT_SIGNED",
                "error",
                "h= does not include From; the From header must be signed",
                "RFC 6376 §5.4",
            )
        )

    if sig.identity and sig.domain and not _in_domain(sig.identity, sig.domain):
        add(
            _f(
                "DKIM_SIG_IDENTITY_MISMATCH",
                "error",
                f"i={sig.identity} is not in d={sig.domain} or a subdomain of it",
                "RFC 6376 §3.5",
            )
        )

    bad_canon = {sig.header_canon, sig.body_canon} - set(CANONICALIZATIONS)
    if bad_canon:
        add(
            _f(
                "DKIM_SIG_CANONICALIZATION",
                "error",
                f"c={sig.tags['c']}: canonicalization must be simple or relaxed",
                "RFC 6376 §3.4",
            )
        )

    if sig.timestamp is not None and sig.expires is not None and sig.expires <= sig.timestamp:
        add(
            _f(
                "DKIM_SIG_EXPIRY_ORDER",
                "error",
                f"x={sig.expires} is not after t={sig.timestamp}",
                "RFC 6376 §3.5",
            )
        )
    check_time = int((now or datetime.now(UTC)).timestamp())
    if sig.expires is not None and sig.expires < check_time:
        add(
            _f(
                "DKIM_SIG_EXPIRED",
                "error",
                f"signature expired at x={sig.expires}; verifiers may treat it as invalid",
                "RFC 6376 §3.5",
            )
        )

    if sig.body_length is not None:
        add(
            _f(
                "DKIM_SIG_BODY_LENGTH",
                "warning",
                f"l={sig.body_length} signs only part of the body; content appended "
                "after it still verifies",
                "RFC 6376 §8.2",
            )
        )

    if any(f.severity == "error" for f in rep.findings):
        rep.status = "invalid"
    return rep


def dkim_signatures(headers: Iterable[tuple[str, str]]) -> list[str]:
    """Values of every DKIM-Signature header, in order (names are case-insensitive)."""
    return [v for n, v in headers if n.strip().lower() == "dkim-signature"]
