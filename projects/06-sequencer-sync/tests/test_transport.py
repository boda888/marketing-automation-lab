"""UrllibTransport against a local HTTP server (127.0.0.1 only, no vendor is called)."""

from __future__ import annotations

import json
import socket
import ssl
import threading
import time
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from sequencer import (
    BatchStatus,
    Campaign,
    DedupeScope,
    InMemoryPushLog,
    PushState,
    SequencerLead,
    SmartleadAdapter,
    SmartleadMembers,
    TransportError,
    UrllibTransport,
    execute_plan,
    plan_push,
    read_members,
)
from sequencer.adapters import HttpRequest
from sequencer.readback import IncompleteRead


class Handler(BaseHTTPRequestHandler):
    routes: dict[str, tuple[int, dict[str, str], bytes, float]] = {}
    seen: list[tuple[str, str, dict[str, str], bytes]] = []

    def log_message(self, *args: object) -> None:  # keep pytest output clean
        pass

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        self.seen.append((self.command, self.path, dict(self.headers), body))
        path = self.path.split("?")[0]
        status, headers, payload, delay = self.routes.get(path, (404, {}, b"", 0.0))
        if delay:
            time.sleep(delay)
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = do_POST = _handle


@pytest.fixture
def server():
    Handler.routes, Handler.seen = {}, []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def route(path: str, status: int, body: object = b"", headers=None, delay: float = 0.0):
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    hdrs = {"Content-Type": "application/json"} if not isinstance(body, bytes) else {}
    Handler.routes[path] = (status, {**hdrs, **(headers or {})}, raw, delay)


def test_post_sends_json_params_and_headers_and_parses_body(server):
    route("/leads", 200, {"ok": True})
    req = HttpRequest(
        "POST",
        f"{server}/leads",
        params={"api_key": "sk-secret"},
        headers={"Authorization": "Bearer t"},
        json={"lead_list": [{"email": "ana@northwind.example"}]},
        secret_params=frozenset({"api_key"}),
    )
    resp = UrllibTransport(timeout=5).send(req)
    assert resp.status == 200 and resp.body == {"ok": True}
    method, path, headers, body = Handler.seen[0]
    assert method == "POST" and path == "/leads?api_key=sk-secret"
    assert headers["Content-Type"] == "application/json"
    assert headers["Authorization"] == "Bearer t"
    assert json.loads(body) == {"lead_list": [{"email": "ana@northwind.example"}]}


def test_error_status_is_a_response_with_headers_and_body(server):
    route("/limited", 429, {"message": "slow down"}, headers={"Retry-After": "7"})
    route("/text", 400, b"bad request")
    t = UrllibTransport(timeout=5)
    limited = t.send(HttpRequest("GET", f"{server}/limited"))
    assert limited.status == 429 and limited.body == {"message": "slow down"}
    assert limited.headers["Retry-After"] == "7"
    text = t.send(HttpRequest("GET", f"{server}/text"))
    assert text.status == 400 and text.body == "bad request"


def test_redirect_is_returned_not_followed(server):
    route("/old", 302, b"", headers={"Location": "/new"})
    route("/new", 200, {"ok": True})
    resp = UrllibTransport(timeout=5).send(
        HttpRequest("POST", f"{server}/old", json={"leads": [1]})
    )
    assert resp.status == 302
    assert [p for _, p, _, _ in Handler.seen] == ["/old"]  # no body-less GET to /new


def test_push_redirect_keeps_keys_reserved(server):
    route("/api/v1/campaigns/101/leads", 303, b"", headers={"Location": "/elsewhere"})
    campaign = Campaign(campaign_id="101", name="Q4 Logistics DE")
    plan = plan_push(
        "smartlead",
        campaign,
        [SequencerLead(lead_id="l1", email="ana@northwind.example")],
        dedupe_scope=DedupeScope.CAMPAIGN,
        allow_fixtures=True,
    )
    log = InMemoryPushLog()
    adapter = SmartleadAdapter(api_key="sk-secret", base_url=f"{server}/api/v1")
    report = execute_plan(plan, adapter, UrllibTransport(timeout=5), log)
    assert report.batches[0].status is BatchStatus.OUTCOME_UNKNOWN
    assert log.state(plan.to_send[0].key) is PushState.RESERVED
    assert "sk-secret" not in json.dumps(report.summary())


def test_read_timeout_reached_vendor(server):
    route("/slow", 200, {"ok": True}, delay=1.0)
    with pytest.raises(TransportError) as err:
        UrllibTransport(timeout=0.2).send(
            HttpRequest(
                "POST",
                f"{server}/slow",
                params={"api_key": "sk-secret"},
                secret_params=frozenset({"api_key"}),
            )
        )
    assert err.value.reached_vendor is True
    assert "sk-secret" not in str(err.value)


def test_connection_refused_never_left():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()  # nothing listens on this port now
    with pytest.raises(TransportError) as err:
        UrllibTransport(timeout=2).send(HttpRequest("POST", f"http://127.0.0.1:{port}/x"))
    assert err.value.reached_vendor is False


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
        (urllib.error.URLError(TimeoutError("timed out")), True),  # connect or send
        (urllib.error.URLError(ConnectionResetError()), True),
        (ConnectionResetError(), True),
    ],
)
def test_error_mapping(exc, reached):
    with pytest.raises(TransportError) as err:
        UrllibTransport(opener=RaisingOpener(exc)).send(HttpRequest("POST", "https://x.example/"))
    assert err.value.reached_vendor is reached


def test_body_size_cap(server):
    route("/big", 200, b"x" * 2000)
    with pytest.raises(TransportError):
        UrllibTransport(timeout=5, max_body_bytes=1000).send(HttpRequest("GET", f"{server}/big"))


def test_member_read_over_real_http(server):
    route(
        "/api/v1/campaigns/101/leads",
        200,
        {"total_leads": 1, "data": [{"lead": {"email": "Ana@Northwind.example"}}]},
    )
    reader = SmartleadMembers(SmartleadAdapter("sk-secret", base_url=f"{server}/api/v1"))
    read = read_members(reader, Campaign(campaign_id="101", name="Q4"), UrllibTransport(timeout=5))
    assert read.members == {"ana@northwind.example"}


def test_member_read_redirect_is_incomplete(server):
    route("/api/v1/campaigns/101/leads", 301, b"", headers={"Location": "/login"})
    reader = SmartleadMembers(SmartleadAdapter("sk-secret", base_url=f"{server}/api/v1"))
    with pytest.raises(IncompleteRead):
        read_members(reader, Campaign(campaign_id="101", name="Q4"), UrllibTransport(timeout=5))
