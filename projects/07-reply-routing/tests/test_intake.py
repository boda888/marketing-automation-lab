from datetime import UTC, datetime, timedelta

import pytest
from replyrouting import ActionKind, BounceKind, Channel, ReplyClass
from replyrouting.intake import (
    LEMLIST_UNSUBSCRIBE_TYPES,
    EventKind,
    IntakeConfig,
    IntakeOutcome,
    MemorySeenEvents,
    SqliteSeenEvents,
    classify_event,
    handle,
    intake,
    parse_timestamp,
)

NOW = datetime(2026, 11, 13, 17, 0, tzinfo=UTC)
TOKEN = "t0k3n-smartlead-0123456789abcdef"
SECRET = "lemlist-secret-0123456789abcdef"
CONFIG = IntakeConfig(
    lemlist_secret=SECRET,
    path_tokens={"smartlead": TOKEN, "instantly": "t0k3n-instantly-0123456789"},
)
EMAIL = "mara.olsen@northwind-freight.example"


def smartlead(event_type="EMAIL_REPLY", **extra):
    body = {
        "event_type": event_type,
        "campaign_id": 123456,
        "campaign_name": "Q4 freight",
        "sl_email_lead_id": 987654,
        "to_email": EMAIL,
        "to_name": "Mara Olsen",
        "from_email": "jon@lunaris-demo.example",
        "subject": "Re: lanes into Rotterdam",
        "event_timestamp": "2026-11-13T16:30:00.000Z",
        "reply_message": {"text": "Please remove me from your list."},
    }
    body.update(extra)
    return body


def lemlist(event_type="emailsReplied", **extra):
    body = {
        "_id": "act_7Hq2example",
        "type": event_type,
        "teamId": "tea_example",
        "createdAt": "2026-11-13T16:45:00.000Z",
        "campaignId": "cam_example",
        "leadEmail": EMAIL,
        "subject": "Re: lanes",
        "text": "I'm out of the office until 24 November.",
        "secret": SECRET,
    }
    body.update(extra)
    return body


def run(vendor, body, seen=None, token=TOKEN, now=NOW, config=CONFIG):
    return intake(
        vendor, body, now=now, config=config, seen=seen or MemorySeenEvents(), path_token=token
    )


# --- auth ---------------------------------------------------------------------------------


def test_smartlead_without_or_with_wrong_path_token_is_rejected():
    assert run("smartlead", smartlead(), token=None).outcome is IntakeOutcome.REJECTED
    assert run("smartlead", smartlead(), token="guess").outcome is IntakeOutcome.REJECTED


def test_vendor_without_configured_token_fails_closed():
    res = run("smartlead", smartlead(), config=IntakeConfig(lemlist_secret=SECRET))
    assert res.outcome is IntakeOutcome.REJECTED
    assert "not configured" in res.reason


def test_lemlist_secret_checked_then_stripped():
    res = run("lemlist", lemlist(), token=None)
    assert res.outcome is IntakeOutcome.ACCEPTED
    assert "secret" not in res.event.payload
    assert run("lemlist", lemlist(secret="nope")).outcome is IntakeOutcome.REJECTED
    body = lemlist()
    del body["secret"]
    assert run("lemlist", body).outcome is IntakeOutcome.REJECTED


def test_lemlist_without_configured_secret_fails_closed():
    res = run("lemlist", lemlist(), config=IntakeConfig())
    assert res.outcome is IntakeOutcome.REJECTED


def test_caller_body_is_not_mutated():
    body = lemlist()
    run("lemlist", body)
    assert body["secret"] == SECRET


def test_non_object_body_and_unknown_vendor_are_invalid():
    assert run("smartlead", ["x"]).outcome is IntakeOutcome.INVALID
    assert run("mailchimp", {}).outcome is IntakeOutcome.INVALID


# --- event types --------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["EMAIL_REPLY", "EMAIL_REPLIED"])
def test_both_smartlead_reply_spellings_are_replies(name):
    res = run("smartlead", smartlead(name))
    assert res.outcome is IntakeOutcome.ACCEPTED
    assert res.event.kind is EventKind.REPLY
    assert res.event.body_text == "Please remove me from your list."


def test_replay_under_the_other_smartlead_spelling_is_a_duplicate():
    seen = MemorySeenEvents()
    assert run("smartlead", smartlead("EMAIL_REPLY"), seen).outcome is IntakeOutcome.ACCEPTED
    assert run("smartlead", smartlead("EMAIL_REPLIED"), seen).outcome is IntakeOutcome.DUPLICATE


@pytest.mark.parametrize("name", LEMLIST_UNSUBSCRIBE_TYPES)
def test_all_five_lemlist_unsubscribe_types_unsubscribe(name):
    res = run("lemlist", lemlist(name))
    assert res.outcome is IntakeOutcome.ACCEPTED
    assert classify_event(res.event).reply_class is ReplyClass.UNSUBSCRIBE


def test_unknown_event_type_is_an_alert_not_a_fallback():
    res = run("smartlead", smartlead("EMAIL_REPLY_V2"))
    assert res.outcome is IntakeOutcome.ALERT
    assert "EMAIL_REPLY_V2" in res.reason


def test_opens_are_ignored_and_labels_are_context_only():
    assert run("smartlead", smartlead("EMAIL_OPEN")).outcome is IntakeOutcome.IGNORED
    res = run("smartlead", smartlead("LEAD_CATEGORY_UPDATED"))
    assert res.outcome is IntakeOutcome.LABEL_ONLY


def test_lemlist_sender_health_event_is_an_alert():
    res = run("lemlist", lemlist("sendLimitReached", leadEmail=None))
    assert res.outcome is IntakeOutcome.ALERT
    assert "sender health" in res.reason


def test_lemlist_linkedin_reply_has_linkedin_channel_and_whatsapp_is_routed_by_hand():
    res = run("lemlist", lemlist("linkedinReplied"))
    assert res.event.channel is Channel.LINKEDIN
    res = run("lemlist", lemlist("whatsappReplied", _id="act_wa"))
    assert res.outcome is IntakeOutcome.ALERT


# --- keys, age, dedupe --------------------------------------------------------------------


def test_lemlist_key_is_activity_id():
    seen = MemorySeenEvents()
    first = run("lemlist", lemlist(), seen)
    assert first.event.event_key == "lemlist:act_7Hq2example"
    assert run("lemlist", lemlist(), seen).outcome is IntakeOutcome.DUPLICATE
    assert run("lemlist", lemlist(_id="act_other"), seen).outcome is IntakeOutcome.ACCEPTED


def test_smartlead_key_is_derived_and_timestamp_format_does_not_matter():
    seen = MemorySeenEvents()
    first = run("smartlead", smartlead(), seen)
    assert first.event.event_key == "smartlead:123456:987654:reply:2026-11-13T16:30:00.000+00:00"
    replay = smartlead(event_timestamp="2026-11-13T17:30:00+01:00")
    assert run("smartlead", replay, seen).outcome is IntakeOutcome.DUPLICATE


def test_instantly_key_uses_lowercased_lead_email():
    seen = MemorySeenEvents()
    body = {
        "event_type": "reply_received",
        "campaign_id": "c-1",
        "lead_email": "Mara.Olsen@Northwind-Freight.example",
        "timestamp": "2026-11-13T16:00:00Z",
        "reply_text": "Sure, send details.",
    }
    tok = "t0k3n-instantly-0123456789"
    first = run("instantly", body, seen, token=tok)
    assert first.outcome is IntakeOutcome.ACCEPTED
    assert first.event.person_email == EMAIL
    assert run("instantly", dict(body, lead_email=EMAIL), seen, token=tok).outcome is (
        IntakeOutcome.DUPLICATE
    )


def test_missing_key_fields_or_timestamp_are_invalid():
    assert run("smartlead", smartlead(sl_email_lead_id=None)).outcome is IntakeOutcome.INVALID
    assert run("smartlead", smartlead(event_timestamp="soon")).outcome is IntakeOutcome.INVALID


def test_reply_without_person_key_is_invalid_and_does_not_consume_the_key():
    seen = MemorySeenEvents()
    assert run("lemlist", lemlist(leadEmail=None), seen).outcome is IntakeOutcome.INVALID
    assert run("lemlist", lemlist(), seen).outcome is IntakeOutcome.ACCEPTED


def test_old_event_is_stale_for_review_and_not_recorded():
    seen = MemorySeenEvents()
    later = NOW + timedelta(days=2)
    res = run("smartlead", smartlead(), seen, now=later)
    assert res.outcome is IntakeOutcome.STALE
    assert res.event is not None  # kept for the review queue
    assert run("smartlead", smartlead(), seen).outcome is IntakeOutcome.ACCEPTED


def test_future_timestamp_beyond_skew_is_stale():
    res = run("smartlead", smartlead(), now=NOW - timedelta(hours=1))
    assert res.outcome is IntakeOutcome.STALE


def test_reply_without_text_needs_body_not_classification():
    body = smartlead()
    del body["reply_message"]
    res = run("smartlead", body)
    assert res.outcome is IntakeOutcome.NEEDS_BODY
    assert handle(res) is None
    decision = handle(res, body_text="Not interested, remove me.")
    assert decision.reply_class is ReplyClass.UNSUBSCRIBE


def test_memory_store_release_and_bound():
    seen = MemorySeenEvents(max_keys=2)
    assert seen.add("a") and seen.add("b") and not seen.add("a")
    seen.release("a")
    assert seen.add("a")
    assert seen.add("c")  # evicts the oldest ("b")
    assert seen.add("b")


def test_sqlite_store_is_shared_between_instances(tmp_path):
    path = str(tmp_path / "seen.db")
    one, two = SqliteSeenEvents(path), SqliteSeenEvents(path)
    assert one.add("smartlead:1:2:reply:x")
    assert not two.add("smartlead:1:2:reply:x")
    two.release("smartlead:1:2:reply:x")
    assert one.add("smartlead:1:2:reply:x")


def test_parse_timestamp_forms():
    assert parse_timestamp("2026-11-13T16:30:00Z") == datetime(2026, 11, 13, 16, 30, tzinfo=UTC)
    assert parse_timestamp(1_763_051_400) == parse_timestamp(1_763_051_400_000)
    assert parse_timestamp("2026-11-13T16:30:00").tzinfo is UTC
    assert parse_timestamp(True) is None
    assert parse_timestamp("") is None


# --- intake -> classify -> route ----------------------------------------------------------


def test_smartlead_unsubscribe_reply_routes_to_suppression():
    decision = handle(run("smartlead", smartlead()))
    assert ActionKind.SUPPRESS in decision.kinds()


def test_lemlist_ooo_reply_pauses_until_return():
    decision = handle(run("lemlist", lemlist()))
    assert decision.reply_class is ReplyClass.OUT_OF_OFFICE
    assert ActionKind.PAUSE_SEQUENCES in decision.kinds()


def test_vendor_bounce_is_unknown_never_hard():
    res = run("lemlist", lemlist("emailsBounced", text=""))
    c = classify_event(res.event)
    assert c.bounce_kind is BounceKind.UNKNOWN
    assert ActionKind.MARK_EMAIL_INVALID not in handle(res).kinds()


def test_duplicate_and_rejected_events_are_not_routed():
    seen = MemorySeenEvents()
    handle(run("lemlist", lemlist(), seen))
    assert handle(run("lemlist", lemlist(), seen)) is None
    assert handle(run("lemlist", lemlist(secret="x"))) is None
