"""LinkedIn invite ledger: rolling sent counts, limit-error cooldown, stale-invite sweep.

What the lab keeps itself, because no LinkedIn tool reports it (research R5, 2026-11-17):

- **Budget counts sends, not pending.** Vendor guides agree that withdrawing an invite does
  not give the week's quota back (search summaries, not a LinkedIn document), so every send
  in the last ``window_days`` counts — withdrawn ones included. ``SenderAccount.used_this_week``
  is computed from the ledger instead of a calendar-week counter that resets on Monday.
- **A limit error is about the sender, not the lead.** Over the limit, Unipile reportedly
  answers ``422 cannot_resend_yet`` (search summary); the ledger marks that *sender* full
  until ``limit_cooldown_days`` after the error, so its leads wait for it instead of being
  marked failed or re-sent by a retry.
- **Stale invites are withdrawn for hygiene, and re-invites wait.** Pending invites older than
  ``stale_after_days`` are planned for withdrawal (Unipile v2:
  ``POST /v2/{account_id}/users/me/relation-requests/{id}/cancel``; HeyReach / Aimfox expose
  no withdraw call). A withdrawn profile is blocked for ``reinvite_block_days`` (LinkedIn
  Help as quoted by a vendor: up to three weeks — not opened). No vendor sends an event for a
  withdrawn or ignored invite, so the send time comes from this ledger, not the vendor.

Pure functions over records the caller stores; nothing here calls LinkedIn or a vendor.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from enum import StrEnum

from .linkedin import SenderAccount
from .models import normalise_linkedin_url


class InviteStatus(StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"  # e.g. Unipile ``relation.new``
    WITHDRAWN = "withdrawn"


@dataclass(frozen=True)
class InviteRecord:
    sender_id: int
    profile_url: str
    sent_at: datetime
    status: InviteStatus = InviteStatus.PENDING
    invite_id: str | None = None  # vendor's request id, needed to cancel
    withdrawn_at: datetime | None = None

    def __post_init__(self) -> None:
        _aware(self.sent_at, "sent_at")
        if self.withdrawn_at is not None:
            _aware(self.withdrawn_at, "withdrawn_at")
        if (self.status is InviteStatus.WITHDRAWN) != (self.withdrawn_at is not None):
            raise ValueError("withdrawn_at is set exactly when status is withdrawn")


@dataclass(frozen=True)
class InvitePolicy:
    window_days: int = 7
    stale_after_days: int = 21
    reinvite_block_days: int = 21
    limit_cooldown_days: int = 7

    def __post_init__(self) -> None:
        if min(self.window_days, self.stale_after_days, self.limit_cooldown_days) < 1:
            raise ValueError("window, stale cut-off and cooldown must be >= 1 day")
        if self.reinvite_block_days < 0:
            raise ValueError("reinvite_block_days must be >= 0")


@dataclass(frozen=True)
class Withdrawal:
    record: InviteRecord
    age_days: int


@dataclass
class SweepPlan:
    withdraw: list[Withdrawal] = field(default_factory=list)
    no_invite_id: list[InviteRecord] = field(default_factory=list)  # stale, cannot cancel yet
    blocked_until: dict[str, datetime] = field(default_factory=dict)  # after the withdrawals


class InviteLedger:
    """Sends per sender and profile, plus the last invite-limit error per sender."""

    def __init__(
        self,
        records: Iterable[InviteRecord] = (),
        *,
        limit_errors: Mapping[int, datetime] | None = None,
        policy: InvitePolicy | None = None,
    ) -> None:
        self.policy = policy or InvitePolicy()
        self.records: list[InviteRecord] = []
        for r in records:
            self.add(r)
        self.limit_errors: dict[int, datetime] = {}
        for sender_id, at in (limit_errors or {}).items():
            self.record_limit_error(sender_id, at)

    def add(self, record: InviteRecord) -> None:
        url = normalise_linkedin_url(record.profile_url)
        if url is None:
            raise ValueError(f"not a LinkedIn profile URL: {record.profile_url!r}")
        self.records.append(replace(record, profile_url=url))

    def record_limit_error(self, sender_id: int, at: datetime) -> None:
        """An invite-limit answer (e.g. ``422 cannot_resend_yet``): the sender is full."""
        _aware(at, "limit error time")
        prev = self.limit_errors.get(sender_id)
        self.limit_errors[sender_id] = at if prev is None else max(prev, at)

    # --- budget ---------------------------------------------------------------------------

    def sent_in_window(self, sender_id: int, now: datetime) -> int:
        """Every send in the last ``window_days`` (withdrawn ones too: no refund)."""
        _aware(now, "now")
        since = now - timedelta(days=self.policy.window_days)
        return sum(1 for r in self.records if r.sender_id == sender_id and since < r.sent_at <= now)

    def full_until(self, sender_id: int, now: datetime) -> datetime | None:
        at = self.limit_errors.get(sender_id)
        if at is None:
            return None
        until = at + timedelta(days=self.policy.limit_cooldown_days)
        return until if until > now else None

    def apply(self, senders: Sequence[SenderAccount], now: datetime) -> list[SenderAccount]:
        """Senders with ``used_this_week`` from the ledger; a sender in limit cooldown is set
        to its full budget (remaining 0), so ``assign_senders`` holds its leads."""
        out = []
        for s in senders:
            used = self.sent_in_window(s.account_id, now)
            if self.full_until(s.account_id, now) is not None:
                used = max(used, s.weekly_invite_budget)
            out.append(replace(s, used_this_week=used))
        return out

    # --- re-invites and the sweep ---------------------------------------------------------

    def reinvite_blocked_until(self, profile_url: str, now: datetime) -> datetime | None:
        """Until when this profile must not be invited again, or None.

        A pending invite blocks indefinitely (returned as ``datetime.max`` in ``now``'s
        timezone); an accepted one too (already a connection); a withdrawal blocks for
        ``reinvite_block_days``."""
        _aware(now, "now")
        url = normalise_linkedin_url(profile_url)
        if url is None:
            return None
        latest: datetime | None = None
        for r in self.records:
            if r.profile_url != url:
                continue
            if r.status is not InviteStatus.WITHDRAWN:
                return datetime.max.replace(tzinfo=now.tzinfo)
            assert r.withdrawn_at is not None
            until = r.withdrawn_at + timedelta(days=self.policy.reinvite_block_days)
            if until > now and (latest is None or until > latest):
                latest = until
        return latest

    def blocked_profiles(self, now: datetime) -> set[str]:
        """Profile URLs to pass as ``in_campaign`` (held, never pushed) right now."""
        urls = {r.profile_url for r in self.records}
        return {u for u in urls if self.reinvite_blocked_until(u, now) is not None}

    def plan_sweep(self, now: datetime) -> SweepPlan:
        """Pending invites older than ``stale_after_days``, oldest first. Planning only:
        record the withdrawal with ``withdrawn()`` after the cancel call succeeded."""
        _aware(now, "now")
        cut = now - timedelta(days=self.policy.stale_after_days)
        plan = SweepPlan()
        stale = sorted(
            (r for r in self.records if r.status is InviteStatus.PENDING and r.sent_at <= cut),
            key=lambda r: (r.sent_at, r.sender_id, r.profile_url),
        )
        block = timedelta(days=self.policy.reinvite_block_days)
        for r in stale:
            if not r.invite_id:
                plan.no_invite_id.append(r)
                continue
            plan.withdraw.append(Withdrawal(r, (now - r.sent_at).days))
            plan.blocked_until[r.profile_url] = now + block
        return plan

    def withdrawn(self, invite_id: str, at: datetime) -> InviteRecord:
        """Mark a pending invite withdrawn (after the vendor confirmed the cancel)."""
        _aware(at, "withdrawn time")
        for i, r in enumerate(self.records):
            if r.invite_id == invite_id:
                if r.status is not InviteStatus.PENDING:
                    raise ValueError(f"invite {invite_id} is {r.status.value}, not pending")
                self.records[i] = replace(r, status=InviteStatus.WITHDRAWN, withdrawn_at=at)
                return self.records[i]
        raise KeyError(invite_id)


def _aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
