"""Prove a CRM webhook delivery is genuine before it touches the IdMap.

A merge webhook re-points stored ids, so a forged one can redirect every later write to a
record of the attacker's choice. Each CRM proves origin differently (research R7 note
2026-11-10):

- HubSpot v3: ``X-HubSpot-Signature-v3`` = base64 HMAC-SHA256 keyed with the **app client
  secret** over ``method + url + raw body + timestamp``; ``X-HubSpot-Request-Timestamp`` in
  ms, older than 5 minutes rejected. Concatenation and encoding from the official
  ``@hubspot/api-client`` 14.1.3 ``Signature`` util; header names from search summaries.
  The SDK compares with ``===`` and lets a *future* timestamp pass; here both directions
  are bounded and the compare is constant-time. v1 / v2 (sha256 hex of secret + …) are kept
  for old apps and tested against the SDK's own fixture vectors.
- Attio: hex HMAC-SHA256 of the raw body keyed with the webhook ``secret`` in
  ``Attio-Signature`` (or ``X-Attio-Signature``) — search summaries only, sources conflict
  on a timestamp, so confirm on a live delivery.
- Pipedrive: no signature; optional HTTP Basic credentials set on the webhook.

Every check fails closed: no configured secret means the request is refused, never waved
through. All functions take the **raw body bytes**; re-serialised JSON (what n8n's HubSpot
Trigger hashes) changes bytes and breaks genuine signatures.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
from collections.abc import Mapping
from dataclasses import dataclass

HUBSPOT_MAX_AGE_MS = 300_000


@dataclass(frozen=True)
class AuthResult:
    ok: bool
    reason: str  # "ok" or why the request was refused

    @property
    def status(self) -> int:
        """HTTP status to answer: 401 for a refused delivery, 500 when we are misconfigured.

        HubSpot does not retry 401 (forum answer, search summary), so a forged request is
        not re-sent; a missing secret is our fault, and a 5xx lets the CRM retry later.
        """
        if self.ok:
            return 200
        return 500 if self.reason == "not_configured" else 401


OK = AuthResult(True, "ok")


def _refuse(reason: str) -> AuthResult:
    return AuthResult(False, reason)


def _header(headers: Mapping[str, str], *names: str) -> str | None:
    lowered = {k.lower(): v for k, v in headers.items()}
    for name in names:
        value = lowered.get(name.lower())
        if value is not None and value.strip():
            return value.strip()
    return None


def hubspot_v3_signature(
    client_secret: str, method: str, url: str, raw_body: bytes, timestamp: str
) -> str:
    source = method.upper().encode() + url.encode() + raw_body + timestamp.encode()
    digest = hmac.new(client_secret.encode(), source, hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


def verify_hubspot_v3(
    *,
    client_secret: str | None,
    method: str,
    url: str,
    raw_body: bytes,
    signature: str | None,
    timestamp: str | None,
    now_ms: int,
    max_age_ms: int = HUBSPOT_MAX_AGE_MS,
) -> AuthResult:
    """``url`` must be the exact public URL HubSpot called (scheme, host, path, query).

    Behind a proxy, rebuild it from the configured public base URL, not from the Host header
    the app sees. A ``+`` turned into a space in the header (seen behind some proxies) is a
    mismatch here, not silently repaired.
    """
    if not client_secret:
        return _refuse("not_configured")
    if not signature:
        return _refuse("missing_signature")
    if not timestamp:
        return _refuse("missing_timestamp")
    if not timestamp.isdigit():
        return _refuse("bad_timestamp")
    age = now_ms - int(timestamp)
    if age > max_age_ms:
        return _refuse("stale_timestamp")
    if age < -max_age_ms:
        return _refuse("future_timestamp")
    expected = hubspot_v3_signature(client_secret, method, url, raw_body, timestamp)
    if not hmac.compare_digest(expected.encode(), signature.encode()):
        return _refuse("bad_signature")
    return OK


def hubspot_legacy_signature(
    client_secret: str, raw_body: bytes, *, version: str = "v1", method: str = "", url: str = ""
) -> str:
    """v1 = sha256(secret + body); v2 = sha256(secret + method + url + body); hex."""
    if version == "v1":
        source = client_secret.encode() + raw_body
    elif version == "v2":
        source = client_secret.encode() + method.upper().encode() + url.encode() + raw_body
    else:
        raise ValueError(f"unknown HubSpot signature version: {version}")
    return hashlib.sha256(source).hexdigest()


def verify_attio(*, secret: str | None, raw_body: bytes, signature: str | None) -> AuthResult:
    if not secret:
        return _refuse("not_configured")
    if not signature:
        return _refuse("missing_signature")
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected.encode(), signature.lower().encode()):
        return _refuse("bad_signature")
    return OK


def verify_basic_auth(
    *, authorization: str | None, user: str | None, password: str | None
) -> AuthResult:
    if not user or not password:
        return _refuse("not_configured")
    if not authorization:
        return _refuse("missing_credentials")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "basic" or not token:
        return _refuse("bad_credentials")
    try:
        decoded = base64.b64decode(token.strip(), validate=True).decode()
    except (binascii.Error, UnicodeDecodeError):
        return _refuse("bad_credentials")
    got_user, sep, got_password = decoded.partition(":")
    # Compare both halves even when the first differs: no early exit to time.
    user_ok = hmac.compare_digest(got_user.encode(), user.encode())
    password_ok = hmac.compare_digest(got_password.encode(), password.encode())
    if not (sep and user_ok and password_ok):
        return _refuse("bad_credentials")
    return OK


@dataclass(frozen=True)
class WebhookSecrets:
    """What each CRM's deliveries are checked against; None = that CRM is refused."""

    hubspot_client_secret: str | None = None
    attio_secret: str | None = None
    pipedrive_user: str | None = None
    pipedrive_password: str | None = None


def verify_webhook(
    crm: str,
    *,
    headers: Mapping[str, str],
    raw_body: bytes,
    method: str,
    url: str,
    secrets: WebhookSecrets,
    now_ms: int,
) -> AuthResult:
    """One entry point for ``POST /v1/webhooks/{crm}``; unknown CRMs are refused."""
    if crm == "hubspot":
        return verify_hubspot_v3(
            client_secret=secrets.hubspot_client_secret,
            method=method,
            url=url,
            raw_body=raw_body,
            signature=_header(headers, "X-HubSpot-Signature-v3"),
            timestamp=_header(headers, "X-HubSpot-Request-Timestamp"),
            now_ms=now_ms,
        )
    if crm == "attio":
        return verify_attio(
            secret=secrets.attio_secret,
            raw_body=raw_body,
            signature=_header(headers, "Attio-Signature", "X-Attio-Signature"),
        )
    if crm == "pipedrive":
        return verify_basic_auth(
            authorization=_header(headers, "Authorization"),
            user=secrets.pipedrive_user,
            password=secrets.pipedrive_password,
        )
    return _refuse("unknown_crm")
