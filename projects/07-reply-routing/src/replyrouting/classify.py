"""Deterministic reply classification: the classes with suppression or legal consequences.

Order matters and is fixed:

1. bounce         delivery status notification (multipart/report; report-type=delivery-status,
                  mailer-daemon / postmaster sender, DSN subject); hard / soft from Status 5.x.x
                  / 4.x.x, unknown when no status is readable (never promoted to hard)
2. left company   an auto-reply or a human saying the person left -> wrong_person / referral,
                  checked before OOO because "X has left, contact Y" usually arrives as an
                  auto-reply
3. out of office  auto-reply headers (Auto-Submitted other than "no", X-Autoreply,
                  X-Autorespond, Precedence auto_reply, Exchange X-Auto-Response-Suppress
                  together with X-MS-Exchange-Inbox-Rules-Loop) or an OOO subject / phrase;
                  return date extracted when it follows a return cue
4. unsubscribe    explicit opt-out wording in the person's *own* text (quoted thread and our
                  own footer stripped first)
5. wrong person / referral   "not the right person", "please contact jane@..."
6. no meaningful reply       nothing left after stripping quote, signature and pleasantries
7. otherwise      unclassified + needs_llm: intent (interested / question / objection / not
                  interested) is left to the schema-validated LLM stage

Any other auto-generated message (header says so, no OOO wording) becomes
no_meaningful_reply with ``auto_generated=True``: a ticket-system acknowledgement is not a
human reply and must not stop a sequence as if someone answered.

The vendor's label is context only; it is recorded and flagged when it disagrees.
"""

from __future__ import annotations

import re

from .dates import extract_return_date
from .models import BounceKind, Classification, InboundReply, ReplyClass

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

# --- quoted thread / footer stripping -------------------------------------------------------

_QUOTE_HEADERS = [
    re.compile(r"^\s*On .{0,200}wrote:\s*$", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^\s*-{2,}\s*Original Message\s*-{2,}", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^\s*-{2,}\s*Forwarded message\s*-{2,}", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^\s*From:\s.+$\n^\s*(Sent|Date):\s", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^_{10,}\s*$", re.MULTILINE),  # Outlook separator line
]
_SIGNATURE = re.compile(r"^--\s*$", re.MULTILINE)


def own_text(body: str) -> str:
    """The sender's own words: quoted thread, '>' lines and '-- ' signature removed."""
    cut = len(body)
    for pattern in _QUOTE_HEADERS:
        m = pattern.search(body)
        if m is not None:
            cut = min(cut, m.start())
    m = _SIGNATURE.search(body)
    if m is not None:
        cut = min(cut, m.start())
    lines = [ln for ln in body[:cut].splitlines() if not ln.lstrip().startswith(">")]
    return "\n".join(lines).strip()


# --- bounce ---------------------------------------------------------------------------------

_DSN_SENDER = re.compile(r"(mailer[-_]?daemon|postmaster)@", re.IGNORECASE)
_DSN_SUBJECT = re.compile(
    r"^\s*(undeliverable|undelivered mail returned to sender|delivery status notification"
    r" \(failure\)|delivery status notification \(delay\)|mail delivery failed"
    r"|returned mail|failure notice|delivery has failed|mail delivery subsystem)",
    re.IGNORECASE,
)
_DSN_STATUS = re.compile(r"\bStatus:\s*([245])\.(\d{1,3})\.(\d{1,3})", re.IGNORECASE)
_SMTP_ENHANCED = re.compile(r"\b([45])\.(\d{1,3})\.(\d{1,3})\b")
_FINAL_RECIPIENT = re.compile(r"Final-Recipient:\s*rfc822;\s*(\S+@\S+)", re.IGNORECASE)


def _is_dsn(reply: InboundReply) -> bool:
    ctype = (reply.header("Content-Type") or "").lower().replace(" ", "")
    if "multipart/report" in ctype and "report-type=delivery-status" in ctype:
        return True
    if _DSN_SENDER.search(reply.from_address or ""):
        return True
    return bool(_DSN_SUBJECT.search(reply.subject or ""))


def _bounce(reply: InboundReply) -> Classification:
    text = reply.body_text or ""
    m = _DSN_STATUS.search(text) or _SMTP_ENHANCED.search(text)
    status = None
    kind = BounceKind.UNKNOWN
    if m is not None:
        status = ".".join(m.groups())
        if m.group(1) == "5":
            kind = BounceKind.HARD
        elif m.group(1) == "4":
            kind = BounceKind.SOFT
    recipient = _FINAL_RECIPIENT.search(text)
    evidence = f"status {status}" if status else "DSN without a readable status"
    if recipient:
        evidence += f"; final recipient {recipient.group(1).lower()}"
    return Classification(
        ReplyClass.BOUNCE,
        rule="dsn",
        evidence=evidence,
        auto_generated=True,
        bounce_kind=kind,
        bounce_status=status,
    )


# --- auto-reply / out of office -------------------------------------------------------------


def _auto_reply_header(reply: InboundReply) -> str | None:
    suppress = reply.header("X-Auto-Response-Suppress")
    if suppress is not None and reply.header("X-MS-Exchange-Inbox-Rules-Loop") is not None:
        # Exchange inbox-rule auto-reply (Zammad's check); the suppress header alone is set on
        # many ordinary Outlook messages and proves nothing.
        return "X-MS-Exchange-Inbox-Rules-Loop"
    auto_submitted = (reply.header("Auto-Submitted") or "").strip().lower()
    if auto_submitted and not auto_submitted.startswith("no"):
        return f"Auto-Submitted: {auto_submitted}"
    for name in ("X-Autoreply", "X-Autorespond", "X-Autoresponder"):
        if reply.header(name) is not None:
            return name
    precedence = (reply.header("Precedence") or "").strip().lower()
    if precedence == "auto_reply":
        return "Precedence: auto_reply"
    return None


_OOO_SUBJECT = re.compile(
    r"^\s*(automatic reply|auto(?:matic)?[- ]?reply|auto:|out of (?:the )?office|ooo\b"
    r"|away from (?:the )?office|on vacation|on holiday|abwesenheitsnotiz|autosvar"
    r"|automatisch antwoord|réponse automatique)",
    re.IGNORECASE,
)
# Strong wording: enough on its own, even when the webhook carries no headers (most
# sequencer payloads carry only the body).
_OOO_STRONG = re.compile(
    r"\b(out of (?:the )?office|on (?:annual|parental|maternity|paternity|sick|medical) leave"
    r"|limited access to (?:my )?e-?mail|(?:automatic|auto)[- ]?reply"
    r"|will (?:respond|reply|get back to you)(?: to your (?:e-?mail|message))? (?:upon|on|after)"
    r" my return)",
    re.IGNORECASE,
)
# Weak wording: counts only together with an auto-reply header or subject.
_OOO_WEAK = re.compile(
    r"\b((?:i am|i'm) (?:currently )?(?:away|on leave|on vacation|on holiday|travell?ing)"
    r"|away from (?:the )?office|back (?:in the office )?on)",
    re.IGNORECASE,
)
# A human who writes "out of office until Monday, but yes, call me" is a lead, not an OOO.
_ENGAGEMENT = re.compile(
    r"\?|\b(interested|call me|let'?s|sounds good|tell me more|send (?:me|over)|book|schedule"
    r"|pricing|demo)\b",
    re.IGNORECASE,
)

# --- left company / wrong person / referral -------------------------------------------------

_LEFT_COMPANY = re.compile(
    r"\b((?:am|is|are) no longer (?:with|at|employed|part of|working (?:at|for|with))"
    r"|(?:i|he|she|they) no longer works? (?:at|for)|(?:has|have) left (?:the company|the business"
    r"|the organi[sz]ation)|this (?:e-?mail|mailbox|address|inbox) is no longer (?:monitored|active"
    r"|in use))",
    re.IGNORECASE,
)
_WRONG_PERSON = re.compile(
    r"\b(not the right (?:person|contact)|wrong (?:person|contact)|not (?:the person|someone)"
    r" (?:responsible|who handles|in charge)|(?:i'm|i am) not (?:responsible|involved|the "
    r"(?:decision[- ]maker|right person))|(?:don't|do not) (?:handle|deal with|manage) (?:this|that"
    r"|these))",
    re.IGNORECASE,
)
_REFERRAL_CUE = re.compile(
    r"\b(please (?:contact|reach out to|email|write to|get in touch with)|reach out to|contact"
    r" (?:my colleague|him|her|them|instead)|(?:better|right) person (?:is|would be)|cc'?(?:ing|ed)"
    r"|forward(?:ed|ing)? (?:this|your (?:e-?mail|message)) to|you (?:should|could|can|may want"
    r" to) (?:speak|talk|contact|reach))",
    re.IGNORECASE,
)

# --- unsubscribe ----------------------------------------------------------------------------

_UNSUBSCRIBE = re.compile(
    r"\b(unsubscribe|opt[- ]?out|remove (?:me|us|my (?:e-?mail|address|name))|take (?:me|us) off"
    r"|stop (?:e-?mailing|emailing|contacting|messaging|sending)|(?:do not|don't|never) (?:contact|"
    r"e-?mail|email|message|write to) (?:me|us)(?: again)?|not interested[,.!]? (?:please )?stop"
    r"|no more (?:e-?mails|emails|messages)|delete my (?:data|details|e-?mail)"
    r"|take me off your list|^\s*stop\s*[.!]*\s*$)",
    re.IGNORECASE | re.MULTILINE,
)

# --- no meaningful reply --------------------------------------------------------------------

_PLEASANTRY = re.compile(
    r"^(thanks?|thank you|thx|ok|okay|noted|received|got it|cheers|best|regards|kind regards"
    r"|best regards|sent from my \w+|👍|\+1)[\s.!,]*$",
    re.IGNORECASE,
)

# Vendor labels we can read with certainty. Instantly lt_interest_status: 0 = Out of Office,
# 1 = Interested (Instantly API v2 schema, R4 notes); other codes are kept raw.
_VENDOR_LABEL_CLASS = {
    "0": ReplyClass.OUT_OF_OFFICE,
    "out of office": ReplyClass.OUT_OF_OFFICE,
    "1": ReplyClass.INTERESTED,
    "interested": ReplyClass.INTERESTED,
    "not interested": ReplyClass.NOT_INTERESTED,
    "unsubscribed": ReplyClass.UNSUBSCRIBE,
    "wrong person": ReplyClass.WRONG_PERSON,
    "bounced": ReplyClass.BOUNCE,
}


def _with_label(c: Classification, reply: InboundReply) -> Classification:
    label = reply.vendor_label
    if label is None:
        return c
    mapped = _VENDOR_LABEL_CLASS.get(str(label).strip().lower())
    disagrees = mapped is not None and mapped != c.reply_class and not c.needs_llm
    return Classification(
        **{**c.__dict__, "vendor_label": str(label), "vendor_label_disagrees": disagrees}
    )


def _other_email(text: str, reply: InboundReply) -> str | None:
    own = {a.lower() for a in (reply.person_email, reply.from_address) if a}
    for m in EMAIL_RE.finditer(text):
        address = m.group(0).rstrip(".").lower()
        if address not in own:
            return address
    return None


def _left_or_referral(text: str, reply: InboundReply, rule: str, auto: bool) -> Classification:
    other = _other_email(text, reply)
    if other is not None:
        return Classification(
            ReplyClass.REFERRAL,
            rule=rule,
            evidence=text[:200],
            auto_generated=auto,
            referral_email=other,
        )
    return Classification(
        ReplyClass.WRONG_PERSON, rule=rule, evidence=text[:200], auto_generated=auto
    )


def classify(reply: InboundReply) -> Classification:
    """Deterministic classification; ``needs_llm`` is set when no rule decides."""
    if reply.channel == "email" and _is_dsn(reply):
        return _with_label(_bounce(reply), reply)

    text = own_text(reply.body_text or "")
    auto_header = _auto_reply_header(reply)
    ooo_subject = bool(_OOO_SUBJECT.search(reply.subject or ""))
    auto = auto_header is not None or ooo_subject

    left = _LEFT_COMPANY.search(text)
    if left is not None:
        return _with_label(_left_or_referral(text, reply, "left_company", auto), reply)

    strong = _OOO_STRONG.search(text)
    weak = _OOO_WEAK.search(text)
    body_only = (
        strong is not None
        and not auto
        and not _ENGAGEMENT.search(text)
        and not _UNSUBSCRIBE.search(text)
    )
    if ooo_subject or (auto_header and (strong or weak)) or body_only:
        found = extract_return_date(f"{reply.subject}\n{text}", reply.received_at.date())
        if auto_header:
            evidence = auto_header
        elif ooo_subject:
            evidence = f"subject: {reply.subject[:80]}"
        else:
            evidence = strong.group(0)
        return _with_label(
            Classification(
                ReplyClass.OUT_OF_OFFICE,
                rule="out_of_office",
                evidence=evidence,
                auto_generated=auto,
                return_date=found.value,
                return_date_note=found.note if found.value is None else f"from '{found.matched}'",
            ),
            reply,
        )

    if auto:
        # Ticket acknowledgements, "we received your message" ... : not a human answer.
        return _with_label(
            Classification(
                ReplyClass.NO_MEANINGFUL_REPLY,
                rule="auto_generated",
                evidence=auto_header or f"subject: {reply.subject[:80]}",
                auto_generated=True,
            ),
            reply,
        )

    unsub = _UNSUBSCRIBE.search(text)
    if unsub is not None:
        return _with_label(
            Classification(
                ReplyClass.UNSUBSCRIBE, rule="unsubscribe_wording", evidence=unsub.group(0)
            ),
            reply,
        )

    if _WRONG_PERSON.search(text) or (_REFERRAL_CUE.search(text) and _other_email(text, reply)):
        return _with_label(_left_or_referral(text, reply, "wrong_person", False), reply)

    meaningful = [
        ln for ln in text.splitlines() if ln.strip() and not _PLEASANTRY.match(ln.strip())
    ]
    if not meaningful:
        return _with_label(
            Classification(
                ReplyClass.NO_MEANINGFUL_REPLY, rule="empty_after_strip", evidence=text[:80]
            ),
            reply,
        )

    return _with_label(
        Classification(
            ReplyClass.UNCLASSIFIED, rule="no_rule", evidence=text[:200], needs_llm=True
        ),
        reply,
    )
