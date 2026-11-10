import base64
import hashlib
import hmac

import pytest
from crmsync.webhook_auth import (
    AuthResult,
    WebhookSecrets,
    hubspot_legacy_signature,
    hubspot_v3_signature,
    verify_attio,
    verify_basic_auth,
    verify_hubspot_v3,
    verify_webhook,
)

# Vectors from @hubspot/api-client 14.1.3 lib/test/unit/signature.spec.js (vendor fixtures).
SDK_SECRET = "yyyyyyyy-yyyy-yyyy-yyyy-yyyyyyyyyyyy"
SDK_V1_BODY = (
    b'[{"eventId":1,"subscriptionId":12345,"portalId":62515,"occurredAt":1564113600000,'
    b'"subscriptionType":"contact.creation","attemptNumber":0,"objectId":123,'
    b'"changeSource":"CRM","changeFlag":"NEW","appId":54321}]'
)
SDK_URL = "https://www.example.com/webhook_uri"

NOW = 1_762_761_600_000  # fixed "now" in ms
URL = "https://hooks.acme.example/v1/webhooks/hubspot?portal=1"
BODY = b'[{"eventId":7,"subscriptionType":"contact.merge","primaryObjectId":11,"attemptNumber":0}]'
SECRET = "app-client-secret"


def test_hubspot_v1_matches_sdk_fixture():
    assert hubspot_legacy_signature(SDK_SECRET, SDK_V1_BODY) == (
        "232db2615f3d666fe21a8ec971ac7b5402d33b9a925784df3ca654d05f4817de"
    )


@pytest.mark.parametrize(
    ("method", "body", "expected"),
    [
        ("GET", b"", "eee2dddcc73c94d699f5e395f4b9d454a069a6855fbfa152e91e88823087200e"),
        (
            "POST",
            b'{"example_field":"example_value"}',
            "9569219f8ba981ffa6f6f16aa0f48637d35d728c7e4d93d0d52efaa512af7900",
        ),
    ],
)
def test_hubspot_v2_matches_sdk_fixtures(method, body, expected):
    sig = hubspot_legacy_signature(SDK_SECRET, body, version="v2", method=method, url=SDK_URL)
    assert sig == expected


def test_hubspot_legacy_rejects_unknown_version():
    with pytest.raises(ValueError):
        hubspot_legacy_signature(SDK_SECRET, b"", version="v3")


def test_hubspot_v3_is_base64_hmac_of_method_url_body_timestamp():
    ts = str(NOW)
    raw = hmac.new(SECRET.encode(), b"POST" + URL.encode() + BODY + ts.encode(), hashlib.sha256)
    assert (
        hubspot_v3_signature(SECRET, "post", URL, BODY, ts)
        == base64.b64encode(raw.digest()).decode()
    )


def _v3(**overrides):
    ts = overrides.pop("timestamp", str(NOW - 1_000))
    args = {
        "client_secret": SECRET,
        "method": "POST",
        "url": URL,
        "raw_body": BODY,
        "signature": hubspot_v3_signature(SECRET, "POST", URL, BODY, ts or ""),
        "timestamp": ts,
        "now_ms": NOW,
    }
    args.update(overrides)
    return verify_hubspot_v3(**args)


def test_hubspot_v3_accepts_genuine_delivery():
    assert _v3() == AuthResult(True, "ok")


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"client_secret": None}, "not_configured"),
        ({"client_secret": ""}, "not_configured"),
        ({"signature": None}, "missing_signature"),
        ({"timestamp": None, "signature": "x"}, "missing_timestamp"),
        ({"timestamp": "12.5", "signature": "x"}, "bad_timestamp"),
        ({"raw_body": BODY + b" "}, "bad_signature"),  # re-serialised / altered body
        ({"url": "http://hooks.acme.example/v1/webhooks/hubspot?portal=1"}, "bad_signature"),
        ({"method": "PUT"}, "bad_signature"),
        ({"client_secret": "other-app"}, "bad_signature"),
    ],
)
def test_hubspot_v3_refusals(overrides, reason):
    assert _v3(**overrides).reason == reason


def test_hubspot_v3_rejects_stale_and_future_timestamps():
    # The SDK only rejects old timestamps; a far-future one would replay for ever.
    assert _v3(timestamp=str(NOW - 300_001)).reason == "stale_timestamp"
    assert _v3(timestamp=str(NOW + 300_001)).reason == "future_timestamp"
    assert _v3(timestamp=str(NOW - 300_000)).ok
    assert _v3(timestamp=str(NOW + 299_000)).ok


def test_hubspot_v3_plus_turned_into_space_is_a_mismatch():
    ts = str(NOW)
    sig = hubspot_v3_signature(SECRET, "POST", URL, BODY, ts)
    mangled = sig.replace("+", " ") if "+" in sig else sig[:-2] + " ="
    assert _v3(timestamp=ts, signature=mangled).reason == "bad_signature"


def test_attio_hex_hmac_of_raw_body():
    body = b'{"events":[{"event_type":"record.merged"}]}'
    sig = hmac.new(b"attio-secret", body, hashlib.sha256).hexdigest()
    assert verify_attio(secret="attio-secret", raw_body=body, signature=sig).ok
    assert verify_attio(secret="attio-secret", raw_body=body, signature=sig.upper()).ok
    assert verify_attio(secret="attio-secret", raw_body=body + b"\n", signature=sig).reason == (
        "bad_signature"
    )
    assert verify_attio(secret=None, raw_body=body, signature=sig).reason == "not_configured"
    assert verify_attio(secret="attio-secret", raw_body=body, signature=None).reason == (
        "missing_signature"
    )


def _basic(user: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


def test_pipedrive_basic_auth():
    good = {"user": "hook", "password": "p:w"}  # ':' inside the password is legal
    assert verify_basic_auth(authorization=_basic("hook", "p:w"), **good).ok
    assert verify_basic_auth(authorization=_basic("hook", "nope"), **good).reason == (
        "bad_credentials"
    )
    assert verify_basic_auth(authorization=_basic("other", "p:w"), **good).reason == (
        "bad_credentials"
    )
    assert verify_basic_auth(authorization=None, **good).reason == "missing_credentials"
    assert verify_basic_auth(authorization="Bearer abc", **good).reason == "bad_credentials"
    assert verify_basic_auth(authorization="Basic !!!", **good).reason == "bad_credentials"
    no_colon = "Basic " + base64.b64encode(b"hookp:w").decode()
    assert verify_basic_auth(authorization=no_colon, **good).reason == "bad_credentials"
    assert (
        verify_basic_auth(authorization=_basic("hook", "p:w"), user="hook", password=None).reason
        == "not_configured"
    )


def test_status_codes():
    assert AuthResult(True, "ok").status == 200
    assert AuthResult(False, "bad_signature").status == 401
    assert AuthResult(False, "not_configured").status == 500


def test_verify_webhook_dispatch_reads_headers_case_insensitively():
    secrets = WebhookSecrets(
        hubspot_client_secret=SECRET,
        attio_secret="attio-secret",
        pipedrive_user="hook",
        pipedrive_password="pw",
    )
    ts = str(NOW)
    hs_headers = {
        "x-hubspot-signature-v3": hubspot_v3_signature(SECRET, "POST", URL, BODY, ts),
        "X-HUBSPOT-REQUEST-TIMESTAMP": ts,
    }
    common = {"raw_body": BODY, "method": "POST", "url": URL, "secrets": secrets, "now_ms": NOW}
    assert verify_webhook("hubspot", headers=hs_headers, **common).ok

    attio_sig = hmac.new(b"attio-secret", BODY, hashlib.sha256).hexdigest()
    assert verify_webhook("attio", headers={"X-Attio-Signature": attio_sig}, **common).ok
    assert verify_webhook("attio", headers={"attio-signature": attio_sig}, **common).ok

    assert verify_webhook("pipedrive", headers={"authorization": _basic("hook", "pw")}, **common).ok
    assert verify_webhook("pipedrive", headers={}, **common).reason == "missing_credentials"
    assert verify_webhook("salesforce", headers={}, **common).reason == "unknown_crm"


def test_verify_webhook_fails_closed_without_secrets():
    common = {
        "raw_body": BODY,
        "method": "POST",
        "url": URL,
        "secrets": WebhookSecrets(),
        "now_ms": NOW,
    }
    for crm in ("hubspot", "attio", "pipedrive"):
        result = verify_webhook(crm, headers={"Authorization": _basic("a", "b")}, **common)
        assert result.reason == "not_configured"
        assert result.status == 500
