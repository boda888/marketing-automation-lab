from datetime import UTC, date, datetime

import pytest
from replyrouting import BounceKind, Channel, InboundReply, ReplyClass, classify, own_text

RECEIVED = datetime(2026, 11, 12, 9, 30, tzinfo=UTC)


def email(body="", subject="Re: route planning", headers=None, **kw) -> InboundReply:
    return InboundReply(
        channel=Channel.EMAIL,
        received_at=RECEIVED,
        body_text=body,
        subject=subject,
        from_address=kw.pop("from_address", "mara.olsen@northwind-freight.example"),
        headers=headers or {},
        person_email=kw.pop("person_email", "mara.olsen@northwind-freight.example"),
        **kw,
    )


QUOTE = (
    "\n\nOn Tue, 10 Nov 2026 at 09:01, Sam <sam@lunaris-lab.example> wrote:\n"
    "> Hi Mara, worth a quick call about lane coverage?\n"
    "> Not relevant? Reply 'unsubscribe' and I will not write again.\n"
)


# --- quote stripping ---------------------------------------------------------------------


def test_own_text_drops_quoted_thread_and_signature():
    body = "Sounds good, Thursday works.\n-- \nMara\nHead of Ops" + QUOTE
    assert own_text(body) == "Sounds good, Thursday works."


def test_unsubscribe_word_in_our_own_quoted_footer_does_not_unsubscribe():
    c = classify(email("Yes, interested. What does it cost?" + QUOTE))
    assert c.reply_class == ReplyClass.UNCLASSIFIED
    assert c.needs_llm


def test_outlook_original_message_separator_is_stripped():
    body = (
        "Please remove me from your list.\n\n-----Original Message-----\nFrom: Sam\n"
        "Interested in a demo?"
    )
    assert own_text(body) == "Please remove me from your list."


# --- bounces -----------------------------------------------------------------------------


def test_dsn_with_permanent_status_is_hard_bounce():
    body = (
        "Delivery to the following recipient failed permanently.\n"
        "Final-Recipient: rfc822; mara.olsen@northwind-freight.example\n"
        "Action: failed\nStatus: 5.1.1\nDiagnostic-Code: smtp; 550 5.1.1 user unknown\n"
    )
    c = classify(
        email(
            body,
            subject="Delivery Status Notification (Failure)",
            from_address="mailer-daemon@mx.lunaris-lab.example",
            headers={"Content-Type": 'multipart/report; report-type="delivery-status"'},
        )
    )
    assert c.reply_class == ReplyClass.BOUNCE
    assert c.bounce_kind == BounceKind.HARD
    assert c.bounce_status == "5.1.1"
    assert "mara.olsen@northwind-freight.example" in c.evidence


def test_transient_status_is_soft_bounce():
    c = classify(
        email(
            "Status: 4.2.2\nMailbox full",
            subject="Delivery Status Notification (Delay)",
            from_address="postmaster@mx.lunaris-lab.example",
        )
    )
    assert c.bounce_kind == BounceKind.SOFT


def test_dsn_without_status_stays_unknown_never_hard():
    c = classify(email("Your message could not be delivered.", subject="Undeliverable: hello"))
    assert c.reply_class == ReplyClass.BOUNCE
    assert c.bounce_kind == BounceKind.UNKNOWN
    assert c.bounce_status is None


def test_human_reply_mentioning_delivery_is_not_a_bounce():
    c = classify(email("Delivery to our Hamburg depot failed last week, can you help?"))
    assert c.reply_class != ReplyClass.BOUNCE


# --- out of office -------------------------------------------------------------------------


def test_ooo_by_auto_submitted_header_with_return_date():
    c = classify(
        email(
            "I am currently away and will be back on Monday 23 November.",
            subject="Re: route planning",
            headers={"Auto-Submitted": "auto-replied"},
        )
    )
    assert c.reply_class == ReplyClass.OUT_OF_OFFICE
    assert c.auto_generated
    assert c.return_date == date(2026, 11, 23)
    assert c.evidence.startswith("Auto-Submitted")


def test_auto_submitted_no_is_not_an_auto_reply():
    c = classify(email("I am currently away this week", headers={"Auto-Submitted": "no"}))
    assert c.reply_class != ReplyClass.OUT_OF_OFFICE


def test_exchange_suppress_header_alone_proves_nothing():
    c = classify(
        email("Can we talk next week?", headers={"X-Auto-Response-Suppress": "All"}),
    )
    assert c.reply_class == ReplyClass.UNCLASSIFIED


def test_exchange_inbox_rule_auto_reply_is_ooo():
    c = classify(
        email(
            "I am on vacation until 30.11.2026.",
            subject="Re: route planning",
            headers={"X-Auto-Response-Suppress": "All", "X-MS-Exchange-Inbox-Rules-Loop": "x"},
        )
    )
    assert c.reply_class == ReplyClass.OUT_OF_OFFICE
    assert c.return_date == date(2026, 11, 30)


def test_ooo_subject_without_date_gets_note_not_a_guess():
    c = classify(email("Thanks for your message.", subject="Automatic reply: route planning"))
    assert c.reply_class == ReplyClass.OUT_OF_OFFICE
    assert c.return_date is None
    assert c.return_date_note == "no date after a return cue"


def test_body_only_ooo_when_payload_has_no_headers():
    c = classify(email("Out of office. I return on 2026-12-01 with limited access to email."))
    assert c.reply_class == ReplyClass.OUT_OF_OFFICE
    assert not c.auto_generated
    assert c.return_date == date(2026, 12, 1)


def test_human_out_of_office_but_engaged_goes_to_llm():
    c = classify(email("I'm out of office until Monday but yes, interested - call me then?"))
    assert c.reply_class == ReplyClass.UNCLASSIFIED
    assert c.needs_llm


def test_weak_away_wording_without_auto_signal_is_not_ooo():
    c = classify(email("I am travelling next week, send me the deck and I'll read it."))
    assert c.reply_class == ReplyClass.UNCLASSIFIED


def test_ambiguous_numeric_return_date_is_reported():
    c = classify(email("Back on 03/04", subject="Out of office"))
    assert c.return_date is None
    assert c.return_date_note.startswith("ambiguous")


def test_auto_generated_ticket_ack_is_not_a_human_reply():
    c = classify(
        email(
            "We received your request. Ticket #4411 has been created.",
            headers={"Auto-Submitted": "auto-generated"},
        )
    )
    assert c.reply_class == ReplyClass.NO_MEANINGFUL_REPLY
    assert c.auto_generated


# --- left company / wrong person / referral --------------------------------------------------


def test_auto_reply_left_company_with_successor_is_referral_not_ooo():
    c = classify(
        email(
            "Mara is no longer with Northwind Freight. Please contact "
            "jon.berg@northwind-freight.example instead.",
            subject="Automatic reply: route planning",
            headers={"Auto-Submitted": "auto-replied"},
        )
    )
    assert c.reply_class == ReplyClass.REFERRAL
    assert c.referral_email == "jon.berg@northwind-freight.example"
    assert c.auto_generated


def test_left_company_without_successor_is_wrong_person():
    c = classify(email("This mailbox is no longer monitored."))
    assert c.reply_class == ReplyClass.WRONG_PERSON
    assert c.referral_email is None


def test_human_referral_to_colleague():
    c = classify(
        email("Not my area. Please reach out to lena.koch@northwind-freight.example, she runs it.")
    )
    assert c.reply_class == ReplyClass.REFERRAL
    assert c.referral_email == "lena.koch@northwind-freight.example"


def test_wrong_person_without_address():
    c = classify(email("I'm not the right person for this, sorry."))
    assert c.reply_class == ReplyClass.WRONG_PERSON


def test_customer_name_left_phrase_is_not_left_company():
    c = classify(email("We have left you a voicemail about our needs, call back?"))
    assert c.reply_class == ReplyClass.UNCLASSIFIED


# --- unsubscribe ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Please remove me from your list",
        "unsubscribe",
        "STOP",
        "Don't email me again.",
        "Take me off this list please",
        "Stop emailing me",
    ],
)
def test_unsubscribe_wording(text):
    assert classify(email(text)).reply_class == ReplyClass.UNSUBSCRIBE


def test_unsubscribe_on_linkedin():
    reply = InboundReply(
        channel=Channel.LINKEDIN,
        received_at=RECEIVED,
        body_text="Please stop messaging me.",
        person_linkedin_url="https://www.linkedin.com/in/mara-olsen-example",
    )
    assert classify(reply).reply_class == ReplyClass.UNSUBSCRIBE


def test_stop_inside_a_sentence_is_not_an_opt_out():
    c = classify(email("We had to stop the pilot in May, what changed since?"))
    assert c.reply_class == ReplyClass.UNCLASSIFIED


# --- no meaningful reply / vendor label -------------------------------------------------------


def test_thanks_only_is_no_meaningful_reply():
    c = classify(email("Thanks!\nSent from my iPhone" + QUOTE))
    assert c.reply_class == ReplyClass.NO_MEANINGFUL_REPLY
    assert not c.auto_generated


def test_vendor_label_is_context_and_disagreement_is_flagged():
    # Instantly interest code 0 = Out of Office, but the person actually opted out.
    c = classify(email("Remove me from your list.", vendor="instantly", vendor_label="0"))
    assert c.reply_class == ReplyClass.UNSUBSCRIBE
    assert c.vendor_label == "0"
    assert c.vendor_label_disagrees


def test_vendor_label_agreeing_is_not_flagged():
    c = classify(email("x", subject="Out of office", vendor_label="Out of Office"))
    assert c.reply_class == ReplyClass.OUT_OF_OFFICE
    assert not c.vendor_label_disagrees


def test_unknown_vendor_label_is_kept_raw():
    c = classify(email("Remove me", vendor_label="Custom: Hot lead (A)"))
    assert c.vendor_label == "Custom: Hot lead (A)"
    assert not c.vendor_label_disagrees
