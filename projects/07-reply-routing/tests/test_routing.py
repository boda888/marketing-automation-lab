from datetime import UTC, date, datetime

from replyrouting import (
    ActionKind,
    BounceKind,
    Channel,
    Classification,
    InboundReply,
    ReplyClass,
    RoutingPolicy,
    classify,
    route,
)

RECEIVED = datetime(2026, 11, 12, 9, 30, tzinfo=UTC)
EMAIL = "mara.olsen@northwind-freight.example"
PROFILE = "https://www.linkedin.com/in/mara-olsen-example"


def reply(body="", channel=Channel.EMAIL, subject="Re: hi", headers=None, email=EMAIL, url=PROFILE):
    return InboundReply(
        channel=channel,
        received_at=RECEIVED,
        body_text=body,
        subject=subject,
        headers=headers or {},
        from_address=email or "",
        person_email=email,
        person_linkedin_url=url,
    )


def keyed(decision, kind):
    return {(a.channel, a.key) for a in decision.actions if a.kind == kind}


def test_linkedin_reply_stops_the_email_campaign_too():
    r = reply("Happy to chat, what do you have in mind?", channel=Channel.LINKEDIN)
    d = route(r, classify(r))
    assert keyed(d, ActionKind.STOP_SEQUENCES) == {
        (Channel.EMAIL, EMAIL),
        (Channel.LINKEDIN, PROFILE),
    }
    assert ActionKind.CLASSIFY_WITH_LLM in d.kinds()
    assert d.warnings == ()


def test_missing_other_channel_key_is_a_warning_not_a_silent_skip():
    r = reply("Sounds interesting, tell me more", url=None)
    d = route(r, classify(r))
    assert keyed(d, ActionKind.STOP_SEQUENCES) == {(Channel.EMAIL, EMAIL)}
    assert any("linkedin" in w and "other channel" in w for w in d.warnings)


def test_unsubscribe_suppresses_every_channel_and_updates_crm():
    r = reply("Please remove me from your list")
    d = route(r, classify(r))
    assert keyed(d, ActionKind.SUPPRESS) == {(Channel.EMAIL, EMAIL), (Channel.LINKEDIN, PROFILE)}
    assert keyed(d, ActionKind.STOP_SEQUENCES) == {
        (Channel.EMAIL, EMAIL),
        (Channel.LINKEDIN, PROFILE),
    }
    assert ActionKind.CRM_UPDATE in d.kinds()


def test_hard_bounce_touches_email_only():
    c = Classification(
        ReplyClass.BOUNCE, rule="dsn", bounce_kind=BounceKind.HARD, bounce_status="5.1.1"
    )
    d = route(reply("x"), c)
    assert keyed(d, ActionKind.STOP_SEQUENCES) == {(Channel.EMAIL, EMAIL)}
    assert keyed(d, ActionKind.SUPPRESS) == {(Channel.EMAIL, EMAIL)}
    assert keyed(d, ActionKind.MARK_EMAIL_INVALID) == {(Channel.EMAIL, EMAIL)}
    assert not any(a.channel == Channel.LINKEDIN for a in d.actions)


def test_soft_bounce_only_records():
    c = Classification(
        ReplyClass.BOUNCE, rule="dsn", bounce_kind=BounceKind.SOFT, bounce_status="4.2.2"
    )
    d = route(reply("x"), c)
    assert d.kinds() == [ActionKind.RECORD_SOFT_BOUNCE]


def test_unknown_bounce_pauses_email_and_asks_a_human():
    c = Classification(ReplyClass.BOUNCE, rule="dsn", bounce_kind=BounceKind.UNKNOWN)
    d = route(reply("x"), c)
    assert ActionKind.STOP_SEQUENCES not in d.kinds()
    assert ActionKind.MARK_EMAIL_INVALID not in d.kinds()
    assert keyed(d, ActionKind.PAUSE_SEQUENCES) == {(Channel.EMAIL, EMAIL)}
    assert ActionKind.NOTIFY_HUMAN in d.kinds()


def test_ooo_pauses_both_channels_until_day_after_return():
    r = reply("I am away, back on 23 November.", headers={"Auto-Submitted": "auto-replied"})
    d = route(r, classify(r))
    pauses = [a for a in d.actions if a.kind == ActionKind.PAUSE_SEQUENCES]
    assert {a.channel for a in pauses} == {Channel.EMAIL, Channel.LINKEDIN}
    assert {a.until for a in pauses} == {date(2026, 11, 24)}
    assert ActionKind.STOP_SEQUENCES not in d.kinds()


def test_ooo_without_date_uses_default_pause():
    r = reply("Thanks for your email.", subject="Automatic reply: hi")
    d = route(r, classify(r), RoutingPolicy(ooo_default_pause_days=5))
    assert {a.until for a in d.actions if a.kind == ActionKind.PAUSE_SEQUENCES} == {
        date(2026, 11, 17)
    }


def test_ooo_far_out_is_capped_and_flagged():
    r = reply("On parental leave, returning 2027-06-01.", subject="Out of office")
    d = route(r, classify(r))
    assert {a.until for a in d.actions if a.kind == ActionKind.PAUSE_SEQUENCES} == {
        date(2027, 1, 11)
    }
    assert ActionKind.NOTIFY_HUMAN in d.kinds()


def test_ooo_return_date_in_the_past_falls_back_to_default():
    r = reply("Out of office until 2026-11-01.", subject="Out of office")
    d = route(r, classify(r))
    assert {a.until for a in d.actions if a.kind == ActionKind.PAUSE_SEQUENCES} == {
        date(2026, 11, 19)
    }


def test_referral_creates_a_task_and_never_enrols_the_new_person():
    r = reply("I'm not the right person. Please contact lena.koch@northwind-freight.example")
    d = route(r, classify(r))
    tasks = [a for a in d.actions if a.kind == ActionKind.REFERRAL_TASK]
    assert [t.key for t in tasks] == ["lena.koch@northwind-freight.example"]
    assert all(a.key != "lena.koch@northwind-freight.example" for a in d.actions if a.channel)
    assert keyed(d, ActionKind.STOP_SEQUENCES) == {
        (Channel.EMAIL, EMAIL),
        (Channel.LINKEDIN, PROFILE),
    }


def test_auto_generated_ack_lets_the_sequence_continue():
    r = reply("Ticket #1 created", headers={"Auto-Submitted": "auto-generated"})
    d = route(r, classify(r))
    assert d.kinds() == [ActionKind.NONE]


def test_human_thanks_stops_and_notifies():
    r = reply("Thanks!")
    d = route(r, classify(r))
    assert ActionKind.STOP_SEQUENCES in d.kinds()
    assert ActionKind.NOTIFY_HUMAN in d.kinds()


def test_interested_from_llm_stage_is_high_priority():
    c = Classification(ReplyClass.INTERESTED, rule="llm")
    d = route(reply("x"), c)
    notify = [a for a in d.actions if a.kind == ActionKind.NOTIFY_HUMAN]
    assert notify[0].priority == "high"


def test_vendor_label_disagreement_surfaces_as_warning():
    r = InboundReply(
        channel=Channel.EMAIL,
        received_at=RECEIVED,
        body_text="Remove me",
        person_email=EMAIL,
        person_linkedin_url=PROFILE,
        vendor_label="Interested",
    )
    d = route(r, classify(r))
    assert any("disagrees" in w for w in d.warnings)
