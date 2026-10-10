"""One-click unsubscribe header check (RFC 8058, building on RFC 2369).

For a message to offer one-click unsubscribe (RFC 8058 §3.1, §4):

- ``List-Unsubscribe`` holds angle-bracketed, comma-separated URIs and MUST
  contain exactly one HTTPS URI (other schemes such as ``mailto:`` may sit
  alongside it);
- ``List-Unsubscribe-Post`` MUST contain the single key/value pair
  ``List-Unsubscribe=One-Click``;
- the message MUST carry a valid DKIM signature whose ``h=`` covers both
  headers.

This module checks the headers as written. It cannot tell whether a DKIM
signature will verify, and it does not call the unsubscribe URL.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from email import message_from_string
from email.policy import compat32

from pydantic import BaseModel

from .findings import Finding

ONE_CLICK_VALUE = "List-Unsubscribe=One-Click"
_URI_RE = re.compile(r"<([^<>]*)>")
_REF = "RFC 8058 §3.1"


class UnsubscribeReport(BaseModel):
    https_uris: list[str] = []
    mailto_uris: list[str] = []
    dkim_covers_headers: bool | None = None
    one_click: bool = False
    findings: list[Finding] = []


def _unfold(value: str) -> str:
    return " ".join(value.split())


def headers_from_message(raw: str) -> list[tuple[str, str]]:
    """Header ``(name, value)`` pairs of a raw RFC 5322 message, in order."""
    msg = message_from_string(raw, policy=compat32)
    return [(k, _unfold(str(v))) for k, v in msg.items()]


def dkim_signed_headers(dkim_signature: str) -> list[str]:
    """Lower-cased header names from a DKIM-Signature ``h=`` tag (RFC 6376 §3.5)."""
    for part in _unfold(dkim_signature).split(";"):
        key, _, value = part.partition("=")
        if key.strip().lower() == "h":
            return [h.strip().lower() for h in value.split(":") if h.strip()]
    return []


def check_one_click(headers: Iterable[tuple[str, str]]) -> UnsubscribeReport:
    """Check List-Unsubscribe / List-Unsubscribe-Post / DKIM ``h=`` coverage."""
    by_name: dict[str, list[str]] = {}
    for name, value in headers:
        by_name.setdefault(name.strip().lower(), []).append(_unfold(value))
    rep = UnsubscribeReport()

    def add(code: str, severity: str, message: str, ref: str = _REF) -> None:
        rep.findings.append(Finding(code=code, severity=severity, message=message, ref=ref))

    lu = by_name.get("list-unsubscribe", [])
    lup = by_name.get("list-unsubscribe-post", [])

    if not lu:
        add("UNSUB_HEADER_MISSING", "error", "no List-Unsubscribe header", "RFC 2369 §3.2")
    elif len(lu) > 1:
        add("UNSUB_HEADER_DUPLICATE", "error", f"{len(lu)} List-Unsubscribe headers; use one")
    else:
        value = lu[0]
        uris = [u.strip() for u in _URI_RE.findall(value)]
        if not uris or _URI_RE.sub("", value).replace(",", "").strip():
            add(
                "UNSUB_HEADER_SYNTAX",
                "error",
                "List-Unsubscribe must be comma-separated URIs in angle brackets",
                "RFC 2369 §2",
            )
        for uri in uris:
            scheme = uri.split(":", 1)[0].lower()
            if scheme == "https":
                rep.https_uris.append(uri)
            elif scheme == "mailto":
                rep.mailto_uris.append(uri)
            elif scheme == "http":
                add("UNSUB_HTTP_NOT_HTTPS", "error", f"{uri}: one-click needs an HTTPS URI")
        if not rep.https_uris:
            add("UNSUB_NO_HTTPS_URI", "error", "List-Unsubscribe has no HTTPS URI")
        elif len(rep.https_uris) > 1:
            add("UNSUB_MULTIPLE_HTTPS_URIS", "error", "List-Unsubscribe must have one HTTPS URI")

    if not lup:
        add("UNSUB_POST_MISSING", "error", f"no List-Unsubscribe-Post: {ONE_CLICK_VALUE} header")
    elif len(lup) > 1 or lup[0].strip() != ONE_CLICK_VALUE:
        add(
            "UNSUB_POST_INVALID",
            "error",
            f"List-Unsubscribe-Post must be exactly one header with {ONE_CLICK_VALUE!r}",
        )

    signatures = by_name.get("dkim-signature", [])
    if not signatures:
        rep.dkim_covers_headers = None
        add(
            "UNSUB_DKIM_UNKNOWN",
            "warning",
            "no DKIM-Signature yet; make sure the signer covers List-Unsubscribe and "
            "List-Unsubscribe-Post in h=",
            "RFC 8058 §4",
        )
    else:
        needed = {"list-unsubscribe", "list-unsubscribe-post"}
        rep.dkim_covers_headers = any(needed <= set(dkim_signed_headers(s)) for s in signatures)
        if not rep.dkim_covers_headers:
            add(
                "UNSUB_NOT_DKIM_SIGNED",
                "error",
                "no DKIM-Signature h= covers both List-Unsubscribe and List-Unsubscribe-Post",
                "RFC 8058 §4",
            )

    rep.one_click = not any(f.severity == "error" for f in rep.findings)
    return rep
