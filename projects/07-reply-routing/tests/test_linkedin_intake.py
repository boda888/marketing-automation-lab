from datetime import UTC, datetime, timedelta

import pytest
from replyrouting import ActionKind, Channel, ReplyClass
from replyrouting.intake import (
    EventKind,
    IntakeConfig,
    IntakeOutcome,
    MemorySeenEvents,
    handle,
    linkedin_intake,
    normalize_profile_url,
)

NOW = datetime(2026, 11, 13, 17, 0, tzinfo=UTC)
HR_TOKEN = "hr-header-token-0123456789abcdef"
AF_TOKEN = "af-path-token-0123456789abcdef"
CONFIG = IntakeConfig(path_tokens={"heyreach": HR_TOKEN, "aimfox": AF_TOKEN})
PROFILE = "https://www.linkedin.com/in/mara-olsen-example"


def envelope(event_type="MESSAGE_REPLY_RECEIVED", **extra):
    env = {
        "event_type": event_type,
        "profile_url": "http://linkedin.com/in/Mara-Olsen-Example/?miniProfileUrn=abc",
        "message_text": "Thanks, but please stop messaging me.",
        "message_at": "2026-11-13T16:40:12Z",
        "campaign_id": "hr-7781",
    }
    env.update(extra)
    return env


def run(body, vendor="heyreach", seen=None, token=HR_TOKEN, now=NOW, config=CONFIG):
    return linkedin_intake(
        vendor, body, now=now, config=config, seen=seen or MemorySeenEvents(), token=token
    )


def test_token_required_and_checked():
    assert run(envelope(), token=None).outcome is IntakeOutcome.REJECTED
    assert run(envelope(), token=AF_TOKEN).outcome is IntakeOutcome.REJECTED
    res = run(envelope(), config=IntakeConfig())
    assert res.outcome is IntakeOutcome.REJECTED


def test_heyreach_first_and_every_reply_for_one_message_are_processed_once():
    seen = MemorySeenEvents()
    first = run(envelope("MESSAGE_REPLY_RECEIVED"), seen=seen)
    every = run(envelope("EVERY_MESSAGE_REPLY_RECEIVED"), seen=seen)
    assert first.outcome is IntakeOutcome.ACCEPTED
    assert every.outcome is IntakeOutcome.DUPLICATE


def test_whitespace_and_case_in_text_do_not_split_the_key():
    seen = MemorySeenEvents()
    run(envelope(), seen=seen)
    again = envelope(
        "EVERY_MESSAGE_REPLY_RECEIVED", message_text="thanks,  but please STOP messaging me."
    )
    assert run(again, seen=seen).outcome is IntakeOutcome.DUPLICATE


def test_a_second_message_from_the_same_person_is_new():
    seen = MemorySeenEvents()
    run(envelope(), seen=seen)
    later = envelope(
        "EVERY_MESSAGE_REPLY_RECEIVED",
        message_text="Actually, send me the deck.",
        message_at="2026-11-13T16:52:00Z",
    )
    assert run(later, seen=seen).outcome is IntakeOutcome.ACCEPTED


@pytest.mark.parametrize("name", ["reply", "new_reply", "campaign_reply", "inmail_reply"])
def test_aimfox_reply_variants_collapse(name):
    seen = MemorySeenEvents()
    assert run(envelope("reply"), "aimfox", seen, AF_TOKEN).outcome is IntakeOutcome.ACCEPTED
    if name != "reply":
        res = run(envelope(name), "aimfox", seen, AF_TOKEN)
        assert res.outcome is IntakeOutcome.DUPLICATE


def test_aimfox_logged_out_sender_is_an_alert_once():
    seen = MemorySeenEvents()
    body = {"event_type": "account_logged_out", "sender_account": "acc-3", "message_at": NOW}
    first = run(body, "aimfox", seen, AF_TOKEN)
    assert first.outcome is IntakeOutcome.ALERT
    assert first.event.kind is EventKind.SENDER_ALERT
    replay = dict(body, message_at=NOW + timedelta(seconds=20))
    assert run(replay, "aimfox", seen, AF_TOKEN).outcome is IntakeOutcome.DUPLICATE


def test_unknown_event_ignored_events_and_labels():
    assert run(envelope("MESSAGE_REPLIED")).outcome is IntakeOutcome.ALERT
    assert run(envelope("VIEWED_PROFILE")).outcome is IntakeOutcome.IGNORED
    assert run(envelope("LEAD_TAG_UPDATED")).outcome is IntakeOutcome.LABEL_ONLY


def test_reply_without_profile_url_is_invalid():
    assert run(envelope(profile_url="")).outcome is IntakeOutcome.INVALID
    assert run(envelope(profile_url="https://www.linkedin.com/company/northwind")).outcome is (
        IntakeOutcome.INVALID
    )


def test_reply_without_text_needs_body():
    res = run(envelope(message_text=""))
    assert res.outcome is IntakeOutcome.NEEDS_BODY


def test_stale_linkedin_event():
    assert run(envelope(), now=NOW + timedelta(days=3)).outcome is IntakeOutcome.STALE


@pytest.mark.parametrize(
    "raw",
    [
        "https://www.linkedin.com/in/mara-olsen-example/",
        "http://linkedin.com/in/Mara-Olsen-Example?trk=x",
        "linkedin.com/in/mara-olsen-example#about",
        "https://de.linkedin.com/in/mara-olsen-example",
    ],
)
def test_profile_url_normalisation(raw):
    assert normalize_profile_url(raw) == PROFILE


def test_non_profile_urls_are_not_guessed():
    assert normalize_profile_url("https://www.linkedin.com/sales/lead/ACwAAB") is None
    assert normalize_profile_url("https://example.com/in/mara") is None


def test_linkedin_opt_out_stops_linkedin_and_warns_about_missing_email():
    res = run(envelope())
    assert res.event.channel is Channel.LINKEDIN
    decision = handle(res)
    assert decision.reply_class is ReplyClass.UNSUBSCRIBE
    suppressed = {(a.channel, a.key) for a in decision.actions if a.kind is ActionKind.SUPPRESS}
    assert (Channel.LINKEDIN, PROFILE) in suppressed
    assert any("email" in w for w in decision.warnings)
