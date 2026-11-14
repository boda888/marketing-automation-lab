"""Classification -> actions for one person, across channels.

A human reply on either channel stops the same person's campaigns on *both* channels (email
by address, LinkedIn by profile URL): native HeyReach <-> Smartlead / Instantly sync does not
stop on replies (research R5, 2026-11-06). When a key for the other channel is missing the
decision says so in ``warnings`` instead of pretending the other channel was stopped.

Actions are plans; executing them (06 sequencer adapters, 08 CRM writer) happens elsewhere.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import StrEnum

from .models import BounceKind, Channel, Classification, InboundReply, ReplyClass

ALL_CHANNELS = (Channel.EMAIL, Channel.LINKEDIN)


class ActionKind(StrEnum):
    STOP_SEQUENCES = "stop_sequences"
    PAUSE_SEQUENCES = "pause_sequences"
    SUPPRESS = "suppress"  # global suppression list (06), not just this campaign
    MARK_EMAIL_INVALID = "mark_email_invalid"  # hard bounce -> 04 status ladder / CRM
    RECORD_SOFT_BOUNCE = "record_soft_bounce"  # feeds 02's per-mailbox bounce guard
    CRM_UPDATE = "crm_update"
    REFERRAL_TASK = "referral_task"  # a human / enrichment queue, never an auto-enrolment
    NOTIFY_HUMAN = "notify_human"
    CLASSIFY_WITH_LLM = "classify_with_llm"
    NONE = "none"


@dataclass(frozen=True)
class Action:
    kind: ActionKind
    channel: Channel | None = None
    key: str | None = None  # email address or LinkedIn profile URL the action applies to
    until: date | None = None
    detail: str = ""
    priority: str = "normal"  # "high" for leads that went hot


@dataclass(frozen=True)
class RoutingPolicy:
    ooo_default_pause_days: int = 7  # OOO without a readable return date
    ooo_buffer_days: int = 1  # resume the day after they are back, not on it
    ooo_max_pause_days: int = 60  # a return date further out -> pause to the cap + review


@dataclass(frozen=True)
class RoutingDecision:
    reply_class: ReplyClass
    actions: tuple[Action, ...]
    warnings: tuple[str, ...] = ()

    def kinds(self) -> list[ActionKind]:
        return [a.kind for a in self.actions]


@dataclass
class _Builder:
    reply: InboundReply
    actions: list[Action] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def _key(self, channel: Channel) -> str | None:
        if channel == Channel.EMAIL:
            return (self.reply.person_email or "").strip().lower() or None
        return (self.reply.person_linkedin_url or "").strip() or None

    def per_channel(
        self,
        kind: ActionKind,
        channels=ALL_CHANNELS,
        *,
        until: date | None = None,
        detail: str = "",
    ) -> None:
        for channel in channels:
            key = self._key(channel)
            if key is None:
                origin = "this" if channel == self.reply.channel else "the other"
                self.warnings.append(
                    f"{kind.value} on {channel.value} not planned: no {channel.value} key for the "
                    f"person ({origin} channel) - resolve the person before relying on it"
                )
                continue
            self.actions.append(Action(kind, channel, key, until=until, detail=detail))

    def add(self, kind: ActionKind, **kw) -> None:
        self.actions.append(Action(kind, **kw))


def route(
    reply: InboundReply, c: Classification, policy: RoutingPolicy | None = None
) -> RoutingDecision:
    policy = policy or RoutingPolicy()
    b = _Builder(reply)
    rc = c.reply_class

    if rc == ReplyClass.BOUNCE:
        if c.bounce_kind == BounceKind.HARD:
            b.per_channel(ActionKind.STOP_SEQUENCES, (Channel.EMAIL,), detail=c.evidence)
            b.per_channel(ActionKind.SUPPRESS, (Channel.EMAIL,), detail="hard bounce")
            b.per_channel(
                ActionKind.MARK_EMAIL_INVALID, (Channel.EMAIL,), detail=c.bounce_status or ""
            )
        elif c.bounce_kind == BounceKind.SOFT:
            b.per_channel(
                ActionKind.RECORD_SOFT_BOUNCE, (Channel.EMAIL,), detail=c.bounce_status or ""
            )
        else:
            # Not provably permanent: hold the email channel and let a person look.
            until = reply.received_at.date() + timedelta(days=policy.ooo_default_pause_days)
            b.per_channel(
                ActionKind.PAUSE_SEQUENCES, (Channel.EMAIL,), until=until, detail=c.evidence
            )
            b.add(ActionKind.NOTIFY_HUMAN, detail="bounce without a readable status")
        # LinkedIn is untouched: a dead mailbox says nothing about the person.

    elif rc == ReplyClass.UNSUBSCRIBE:
        b.per_channel(ActionKind.STOP_SEQUENCES, detail="opt-out")
        b.per_channel(ActionKind.SUPPRESS, detail="opt-out applies to the person, every channel")
        b.add(ActionKind.CRM_UPDATE, detail="opted_out")

    elif rc == ReplyClass.OUT_OF_OFFICE:
        received = reply.received_at.date()
        cap = received + timedelta(days=policy.ooo_max_pause_days)
        if c.return_date is None:
            until = received + timedelta(days=policy.ooo_default_pause_days)
            detail = f"no return date ({c.return_date_note}); default pause"
        elif c.return_date < received:
            until = received + timedelta(days=policy.ooo_default_pause_days)
            detail = f"return date {c.return_date} already passed; default pause"
        else:
            until = c.return_date + timedelta(days=policy.ooo_buffer_days)
            detail = f"back {c.return_date}"
        if until > cap:
            until = cap
            b.add(ActionKind.NOTIFY_HUMAN, detail=f"return {c.return_date} beyond the pause cap")
        b.per_channel(ActionKind.PAUSE_SEQUENCES, until=until, detail=detail)

    elif rc in {ReplyClass.WRONG_PERSON, ReplyClass.REFERRAL}:
        b.per_channel(ActionKind.STOP_SEQUENCES, detail=rc.value)
        b.add(ActionKind.CRM_UPDATE, detail=rc.value)
        if rc == ReplyClass.REFERRAL:
            b.add(
                ActionKind.REFERRAL_TASK,
                key=c.referral_email,
                detail="enrich + decide; not enrolled",
            )

    elif rc == ReplyClass.NO_MEANINGFUL_REPLY:
        if c.auto_generated:
            b.add(ActionKind.NONE, detail="auto-generated, not a human answer; sequence continues")
        else:
            b.per_channel(ActionKind.STOP_SEQUENCES, detail="a person replied")
            b.add(ActionKind.NOTIFY_HUMAN, detail="short human reply")

    elif rc == ReplyClass.UNCLASSIFIED:
        b.per_channel(ActionKind.STOP_SEQUENCES, detail="a person replied")
        if c.needs_human_review:
            # The LLM stage already ran and could not decide: a person does, not another call.
            b.add(
                ActionKind.NOTIFY_HUMAN,
                detail=f"intent review: {c.review_reason}",
                priority=c.review_priority,
            )
        else:
            b.add(ActionKind.CLASSIFY_WITH_LLM, detail="intent not decided by rules")

    elif rc in {ReplyClass.INTERESTED, ReplyClass.QUESTION}:
        b.per_channel(ActionKind.STOP_SEQUENCES, detail=rc.value)
        b.add(ActionKind.NOTIFY_HUMAN, detail=rc.value, priority="high")
        b.add(ActionKind.CRM_UPDATE, detail=rc.value)

    elif rc in {ReplyClass.OBJECTION, ReplyClass.NOT_INTERESTED}:
        b.per_channel(ActionKind.STOP_SEQUENCES, detail=rc.value)
        b.add(ActionKind.CRM_UPDATE, detail=rc.value)

    if c.vendor_label_disagrees:
        b.warnings.append(
            f"vendor label {c.vendor_label!r} disagrees with {rc.value}; label ignored"
        )

    return RoutingDecision(rc, tuple(b.actions), tuple(b.warnings))
