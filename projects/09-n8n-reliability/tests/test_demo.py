"""The offline end-to-end demo: every part of the kit on one clock, checked by its report."""

import json

import pytest
from reliability.demo import DEMO_FILE, main, render, run_demo

DEMO = json.loads(DEMO_FILE.read_text())


@pytest.fixture(scope="module")
def report():
    return run_demo()


def test_first_run_matches_the_scripted_expectations(report):
    assert report["first_run_matches_expect"] is True
    keys = {r["email"]: r["key_after"] for r in report["first_run"]}
    # sent -> committed, may have landed -> kept reserved, certainly not landed -> released
    assert keys["ana@acme.example"] == "committed"
    assert keys["cara@cobalt.example"] == keys["eli@elm.example"] == "reserved"
    assert keys["gus@gale.example"] == keys["fay@fjord.example"] == "released"


def test_a_double_trigger_sends_nothing(report):
    rerun = report["rerun"]
    assert rerun["pushes"] == 0
    claims = {r["email"]: (r["end"], r["claim"]) for r in rerun["leads"]}
    assert claims["ana@acme.example"] == ("skipped", "committed")
    assert claims["cara@cobalt.example"] == ("skipped", "in_flight")
    # released keys are reserved again but the workspace is blocked by Fay's Retry-After
    for email in ("gus@gale.example", "hal@heath.example", "fay@fjord.example"):
        lead = next(r for r in rerun["leads"] if r["email"] == email)
        assert lead["end"] == "dlq" and lead["outcome"] == "deferred" and lead["pushes"] == 0
        assert lead["key_after"] == "released"
    gus = next(r for r in rerun["leads"] if r["email"] == "gus@gale.example")
    assert gus["dlq_outcome"] == "failed" and gus["safe_to_resend"] is False


def test_replay_sends_only_safe_rows_and_refuses_the_rest(report):
    sent = {r["email"]: r for r in report["replay"]["sent_again"]}
    assert set(sent) == {"hal@heath.example", "fay@fjord.example"}
    assert all(r["end"] == "done" and r["key_after"] == "committed" for r in sent.values())
    refused = {r["key"]: r["status"] for r in report["replay"]["refused"]}
    assert refused == {
        "cara@cobalt.example": "unsafe_to_resend",
        "eli@elm.example": "unsafe_to_resend",
        "gus@gale.example": "unsafe_to_resend",
    }


def test_watchdog_reports_stale_keys_then_a_silent_schedule(report):
    after = report["watchdog_after_replay"]
    assert after["counts"]["up"] == 1 and after["counts"]["down"] == 0
    assert after["stale_keys"]["by_scope"] == {"sequencer:campaign-demo": 2}
    assert len(after["alerts"]) == 1 and "key reservation" in after["alerts"][0]
    silent = report["watchdog_after_silence"]
    assert silent["counts"]["down"] == 1
    assert any("'dlq-replay' is down" in a for a in silent["alerts"])
    assert not any("key reservation" in a for a in silent["alerts"])  # resolved by read-back


def test_read_back_resolves_rows_and_keys_from_vendor_state(report):
    got = {r["key"]: (r["landed"], r["key_state"]) for r in report["read_back"]}
    assert got == {
        "cara@cobalt.example": (True, "committed"),  # 502 after the write
        "eli@elm.example": (False, "released"),  # the request never arrived
    }


def test_error_alert_once_then_folded_and_masked(report):
    first, repeat = report["error_alerts"]
    assert first["send"] is True and repeat["send"] is False and repeat["total"] == 2
    assert "ana@acme.example" not in first["text"] and "<email>" in first["text"]


def test_vendor_never_received_a_duplicate_of_a_landed_lead(report):
    pushes = report["vendor"]["pushes_per_lead"]
    # one push each for leads that landed first time; retries only before anything landed
    assert pushes["ana@acme.example"] == 1 and pushes["cara@cobalt.example"] == 1
    assert pushes["gus@gale.example"] == 1  # rejected once, never re-sent automatically
    assert set(report["vendor"]["in_campaign"]) == {
        "ana@acme.example",
        "ben@birch.example",
        "cara@cobalt.example",
        "dev@dune.example",
        "fay@fjord.example",
        "hal@heath.example",
    }


def test_cli_renders_and_exits_zero(capsys):
    assert main([]) == 0
    out = capsys.readouterr().out
    assert "re-run of the same list: 0 pushes" in out
    assert main(["--json"]) == 0
    assert json.loads(capsys.readouterr().out)["rerun"]["pushes"] == 0


def test_render_is_plain_text(report):
    text = render(report)
    assert text.startswith("1. first run") and "7. two hours without a replay run" in text
