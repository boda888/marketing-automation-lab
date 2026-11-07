"""Real HTTP transport for ``execute_upserts`` on the standard library, no extra dependency.

Same rules as 06's ``sequencer.UrllibTransport``:

- every HTTP status comes back as an ``HttpResponse`` (``urllib`` raises ``HTTPError`` for
  4xx / 5xx; status, headers and body are kept, so 429 ``Retry-After`` and HubSpot's 207
  ``errors[]`` still reach ``parse_upsert_response``);
- **redirects are never followed.** CPython's ``HTTPRedirectHandler.redirect_request``
  re-sends a POST redirected with 301 / 302 / 303 as a body-less GET, so a batch upsert
  could come back "200" with nothing written. The 3xx is returned as-is; the parser marks
  the rows failed and not retryable, so the executor never re-sends them blindly;
- ``TransportError(reached_vendor=False)`` only when the request surely never left (DNS
  failure, connection refused, TLS certificate rejected). Anything else without a response
  (connect / read timeout, reset, disconnect) is ``reached_vendor=True``: HubSpot may have
  written the rows. Upserts are safe to re-send either way; a future non-idempotent writer
  (Pipedrive create) must not re-send when ``reached_vendor`` is true;
- error messages carry the method and the URL without its query string, never a header,
  so the private-app token in ``Authorization`` cannot leak into logs.
"""

from __future__ import annotations

import http.client
import json
import socket
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Protocol

from .executor import HttpCall, HttpResponse, TransportError

USER_AGENT = "crm-sync/0.0 (+https://github.com/boda888/marketing-automation-lab)"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # urllib then raises HTTPError with the 3xx, which send() returns


class Opener(Protocol):
    def open(self, request: urllib.request.Request, timeout: float) -> object: ...


def default_opener() -> urllib.request.OpenerDirector:
    """Opener with the environment's proxy settings and redirects switched off."""
    return urllib.request.build_opener(_NoRedirect())


_NEVER_LEFT = (ConnectionRefusedError, socket.gaierror, ssl.SSLCertVerificationError)


def _decode(raw: bytes, content_type: str) -> object:
    if not raw:
        return None
    text = raw.decode("utf-8", errors="replace")
    if "json" in content_type.lower() or text[:1] in "{[":
        try:
            return json.loads(text)
        except ValueError:
            pass
    return text


@dataclass
class UrllibTransport:
    """``Transport`` over ``urllib`` with one timeout for connect and each read."""

    timeout: float = 30.0
    max_body_bytes: int = 10_000_000
    opener: Opener = field(default_factory=default_opener)
    user_agent: str = USER_AGENT

    def _request(self, call: HttpCall) -> urllib.request.Request:
        headers = {"Accept": "application/json", "User-Agent": self.user_agent}
        headers.update(call.headers)
        data = None
        if call.json is not None:
            data = json.dumps(call.json).encode()
            headers.setdefault("Content-Type", "application/json")
        return urllib.request.Request(call.url, data=data, headers=headers, method=call.method)

    def _response(self, status: int, headers: object, body: object) -> HttpResponse:
        pairs = dict(headers.items()) if headers is not None else {}
        raw = body.read(self.max_body_bytes + 1) if body is not None else b""
        if len(raw) > self.max_body_bytes:
            raise TransportError(f"response body over {self.max_body_bytes} bytes")
        content_type = next((v for k, v in pairs.items() if k.lower() == "content-type"), "")
        return HttpResponse(status, _decode(raw, content_type), pairs)

    def send(self, call: HttpCall) -> HttpResponse:
        where = f"{call.method} {call.url.split('?', 1)[0]}"
        try:
            try:
                resp = self.opener.open(self._request(call), timeout=self.timeout)
                status = resp.status
            except urllib.error.HTTPError as exc:  # 3xx / 4xx / 5xx: still a response
                resp, status = exc, exc.code
            with resp:
                return self._response(status, resp.headers, resp)
        except urllib.error.URLError as exc:
            reached = not isinstance(exc.reason, _NEVER_LEFT)
            raise TransportError(f"{where}: {exc.reason}", reached_vendor=reached) from exc
        except (OSError, http.client.HTTPException) as exc:
            # Raised after the request was written (read timeout, reset, disconnect).
            raise TransportError(f"{where}: {exc!r}", reached_vendor=True) from exc
