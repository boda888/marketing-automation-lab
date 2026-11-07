"""UrllibTransport + execute_upserts against a local HTTP server (127.0.0.1, no HubSpot call)."""

from __future__ import annotations

import json
import socket
import ssl
import threading
import time
import urllib.error
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from crmsync import (
    CrmRow,
    HttpCall,
    Outcome,
    RateBudget,
    TransportError,
    UrllibTransport,
    execute_upserts,
    plan_upserts,
)

UPSERT = "/crm/v3/objects/contacts/batch/upsert"


class Handler(BaseHTTPRequestHandler):
    # Each item: (status, headers, body or callable(request_json) -> body, delay seconds).
    script: deque = deque()
    seen: list[tuple[str, str, dict[str, str], object]] = []

    def log_message(self, *args: object) -> None:
        pass

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        payload = json.loads(raw) if raw else None
        self.seen.append((self.command, self.path, dict(self.headers), payload))
        status, headers, body, delay = self.script.popleft() if self.script else (404, {}, b"", 0)
        if delay:
            time.sleep(delay)
        if callable(body):
            body = body(payload)
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(status)
        if not isinstance(body, bytes):
            self.send_header("Content-Type", "application/json")
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = do_POST = _handle


@pytest.fixture
def server():
    Handler.script, Handler.seen = deque(), []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def answer(status, body=b"", headers=None, delay=0.0):
    Handler.script.append((status, headers or {}, body, delay))


def all_created(payload):
    results = [
        {"id": f"7{i}", "_new": True, "objectWriteTraceId": x["objectWriteTraceId"]}
        for i, x in enumerate(payload["inputs"])
    ]
    return {"status": "COMPLETE", "results": results, "errors": []}


def first_only(payload):
    first = payload["inputs"][0]
    return {
        "status": "COMPLETE",
        "results": [{"id": "70", "_new": False, "objectWriteTraceId": first["objectWriteTraceId"]}],
        "errors": [],
    }


def rows(n):
    return [CrmRow(f"r{i}", f"p{i}@northwind.io", {"firstname": f"P{i}"}) for i in range(n)]


def run(base_url, n=2, timeout=5.0, **kw):
    sleeps: list[float] = []
    budget = RateBudget(100, 10, clock=lambda: 0.0, sleep=sleeps.append)
    report = execute_upserts(
        plan_upserts(rows(n)),
        UrllibTransport(timeout=timeout),
        token="pat-na1-secret",
        budget=budget,
        base_url=base_url,
        sleep=sleeps.append,
        **kw,
    )
    return report, sleeps


def test_upsert_over_real_http_sends_json_and_token_header(server):
    answer(200, all_created)
    report, _ = run(server)
    ((method, path, headers, payload),) = Handler.seen
    assert (method, path) == ("POST", UPSERT)
    assert headers["Authorization"] == "Bearer pat-na1-secret"
    assert headers["Content-Type"] == "application/json"
    assert payload["inputs"][0]["idProperty"] == "email"
    assert [o.outcome for o in report.outcomes] == [Outcome.CREATED, Outcome.CREATED]


def test_207_partial_then_only_missing_row_resent(server):
    answer(207, first_only)
    answer(200, all_created)
    report, sleeps = run(server)
    assert [len(p["inputs"]) for *_, p in Handler.seen] == [2, 1]
    assert Handler.seen[1][3]["inputs"][0]["id"] == "p1@northwind.io"
    assert [o.outcome for o in report.outcomes] == [Outcome.UPDATED, Outcome.CREATED]
    assert sleeps == [1.0]


def test_429_retry_after_header_is_read_from_real_response(server):
    answer(429, {"status": "error", "category": "RATE_LIMITS"}, {"Retry-After": "7"})
    answer(200, all_created)
    report, sleeps = run(server)
    assert sleeps == [7.0]
    assert report.summary()["created"] == 2 and report.calls == 2


def test_redirect_is_not_followed_and_rows_are_not_resent(server):
    # urllib would turn this POST into a body-less GET to /elsewhere and report its 200.
    answer(302, b"", {"Location": "/elsewhere"})
    answer(200, all_created)
    report, _ = run(server)
    assert [(m, p) for m, p, *_ in Handler.seen] == [("POST", UPSERT)]
    assert all(o.outcome is Outcome.FAILED and not o.retryable for o in report.outcomes)
    assert report.outcomes[0].reason.startswith("http_302")


def test_401_body_is_kept_and_not_retried(server):
    answer(401, {"status": "error", "message": "Authentication credentials not found."})
    report, _ = run(server)
    assert len(Handler.seen) == 1
    assert all(o.outcome is Outcome.FAILED and not o.retryable for o in report.outcomes)


def test_read_timeout_is_retried_because_upsert_is_idempotent(server):
    answer(200, all_created, delay=1.0)
    answer(200, all_created)
    report, _ = run(server, timeout=0.3)
    assert [o.outcome for o in report.outcomes] == [Outcome.CREATED, Outcome.CREATED]
    assert report.retried_rows == 2


def test_read_timeout_error_says_reached_vendor_and_hides_token(server):
    answer(200, all_created, delay=1.0)
    call = HttpCall(
        "POST",
        f"{server}{UPSERT}?hapikey=old-key",
        {"inputs": []},
        {"Authorization": "Bearer pat-na1-secret"},
    )
    with pytest.raises(TransportError) as err:
        UrllibTransport(timeout=0.2).send(call)
    assert err.value.reached_vendor is True
    assert "pat-na1-secret" not in str(err.value) and "old-key" not in str(err.value)


def test_connection_refused_is_not_sent_and_still_retryable():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()  # nothing listens here now
    report, _ = run(f"http://127.0.0.1:{port}", max_attempts=2)
    assert all(o.reason == "not_sent" and o.retryable for o in report.outcomes)
    assert report.calls == 2


class RaisingOpener:
    def __init__(self, exc: BaseException) -> None:
        self.exc = exc

    def open(self, request, timeout):
        raise self.exc


@pytest.mark.parametrize(
    ("exc", "reached"),
    [
        (urllib.error.URLError(socket.gaierror(-2, "Name or service not known")), False),
        (urllib.error.URLError(ssl.SSLCertVerificationError("bad cert")), False),
        (urllib.error.URLError(ConnectionRefusedError()), False),
        (urllib.error.URLError(TimeoutError("timed out")), True),
        (urllib.error.URLError(ConnectionResetError()), True),
        (ConnectionResetError(), True),
    ],
)
def test_error_mapping(exc, reached):
    with pytest.raises(TransportError) as err:
        UrllibTransport(opener=RaisingOpener(exc)).send(HttpCall("POST", "https://api.example/"))
    assert err.value.reached_vendor is reached


def test_body_size_cap(server):
    answer(200, b"x" * 2000)
    with pytest.raises(TransportError):
        UrllibTransport(timeout=5, max_body_bytes=1000).send(HttpCall("GET", f"{server}/big"))


def test_non_json_body_is_text_and_retried_as_unparseable(server):
    answer(200, b"<html>gateway</html>")
    answer(200, all_created)
    report, _ = run(server)
    assert report.calls == 2 and report.summary()["created"] == 2
