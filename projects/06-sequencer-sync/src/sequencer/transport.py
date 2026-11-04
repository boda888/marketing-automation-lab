"""Real HTTP transport on the standard library (``urllib.request``), no extra dependency.

What it guarantees to ``execute_plan`` / ``read_members``:

- every HTTP status comes back as an ``HttpResponse`` (``urllib`` raises ``HTTPError`` for
  4xx / 5xx; its code, headers and body are kept, so 429 ``Retry-After`` still works);
- **redirects are never followed.** ``urllib``'s default handler turns a redirected POST
  (301 / 302 / 303) into a body-less GET, so a lead push could come back "200" with nothing
  created (CPython ``Lib/urllib/request.py`` ``redirect_request``). The 3xx is returned
  as-is and ``execute_plan`` treats it as ``outcome_unknown`` (a 303 may follow a POST
  that was processed);
- ``TransportError(reached_vendor=False)`` only when the request surely never left: DNS
  failure, connection refused, TLS certificate rejected. Anything else without a response
  (connect / read timeout, reset, remote disconnect) is ``reached_vendor=True``: the vendor
  may have processed the batch, so its idempotency keys stay reserved for ``reconcile``;
- error messages carry ``HttpRequest.redacted_url()`` only, never a query-string key.
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
from urllib.parse import urlencode

from .adapters import HttpRequest
from .push import HttpResponse, TransportError

USER_AGENT = "sequencer-sync/0.1 (+https://github.com/boda888/marketing-automation-lab)"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # urllib then raises HTTPError with the 3xx, which send() returns


class Opener(Protocol):
    def open(self, request: urllib.request.Request, timeout: float) -> object: ...


def default_opener() -> urllib.request.OpenerDirector:
    """Opener with the environment's proxy settings and redirects switched off."""
    return urllib.request.build_opener(_NoRedirect())


_NEVER_LEFT = (ConnectionRefusedError, socket.gaierror, ssl.SSLCertVerificationError)


def _never_left(reason: object) -> bool:
    return isinstance(reason, _NEVER_LEFT)


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

    def _request(self, request: HttpRequest) -> urllib.request.Request:
        url = request.url
        if request.params:
            url = f"{url}{'&' if '?' in url else '?'}{urlencode(request.params)}"
        headers = {"Accept": "application/json", "User-Agent": self.user_agent}
        headers.update(request.headers)
        data = None
        if request.json is not None:
            data = json.dumps(request.json).encode()
            headers.setdefault("Content-Type", "application/json")
        return urllib.request.Request(url, data=data, headers=headers, method=request.method)

    def _response(self, status: int, headers: object, body: object) -> HttpResponse:
        pairs = dict(headers.items()) if headers is not None else {}
        raw = body.read(self.max_body_bytes + 1) if body is not None else b""
        if len(raw) > self.max_body_bytes:
            raise TransportError(f"response body over {self.max_body_bytes} bytes")
        content_type = next((v for k, v in pairs.items() if k.lower() == "content-type"), "")
        return HttpResponse(status, _decode(raw, content_type), pairs)

    def send(self, request: HttpRequest) -> HttpResponse:
        where = f"{request.method} {request.redacted_url()}"
        try:
            try:
                resp = self.opener.open(self._request(request), timeout=self.timeout)
                status = resp.status
            except urllib.error.HTTPError as exc:  # 3xx / 4xx / 5xx: still a response
                resp, status = exc, exc.code
            with resp:
                return self._response(status, resp.headers, resp)
        except urllib.error.URLError as exc:
            reached = not _never_left(exc.reason)
            raise TransportError(f"{where}: {exc.reason}", reached_vendor=reached) from exc
        except (OSError, http.client.HTTPException) as exc:
            # Raised after the request was written (read timeout, reset, disconnect).
            raise TransportError(f"{where}: {exc!r}", reached_vendor=True) from exc
