"""Today's send batch: cap a lead list at today's capacity and spread it over mailboxes.

``plan_capacity`` says how many campaign emails each mailbox may send today;
``assign_batch`` turns that into which mailbox sends which lead. Leads go
round-robin over the mailboxes that have capacity left, so no single mailbox
fills up while others idle, and everything past ``capacity_today`` is
deferred (kept in input order) instead of being pushed anyway.

Leads are keyed by ``id``; the same ``id`` twice, or the same e-mail address
under two ids, is skipped and listed in ``duplicates`` — a duplicate is a
logged skip, never a reason to fail the whole batch.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from .capacity import CapacityPlan
from .findings import Finding

_REF = "capacity plan (plan_capacity)"


class Lead(BaseModel):
    id: str = Field(min_length=1)
    email: str | None = None


class Assignment(BaseModel):
    lead_id: str
    email: str | None = None
    mailbox: str


class BatchPlan(BaseModel):
    capacity_today: int
    assignments: list[Assignment]
    deferred: list[str]  # lead ids, input order
    duplicates: list[str]  # lead ids skipped as duplicates
    by_mailbox: dict[str, int]
    findings: list[Finding] = []


def unique_leads(leads: list[Lead]) -> tuple[list[Lead], list[str]]:
    """Leads with repeated ids or e-mail addresses removed; returns (unique, duplicate ids)."""
    ids: set[str] = set()
    emails: set[str] = set()
    unique: list[Lead] = []
    dupes: list[str] = []
    for lead in leads:
        email = lead.email.strip().lower() if lead.email else None
        if lead.id in ids or (email and email in emails):
            dupes.append(lead.id)
            continue
        ids.add(lead.id)
        if email:
            emails.add(email)
        unique.append(lead)
    return unique, dupes


def assign_batch(plan: CapacityPlan, leads: list[Lead]) -> BatchPlan:
    """Assign up to ``plan.capacity_today`` leads round-robin to mailboxes with capacity."""
    unique, dupes = unique_leads(leads)
    left = {mc.address: mc.capacity for mc in plan.mailboxes if mc.capacity > 0}
    slots: list[str] = []
    while left:
        for address in list(left):
            slots.append(address)
            left[address] -= 1
            if not left[address]:
                del left[address]

    assignments = [
        Assignment(lead_id=lead.id, email=lead.email, mailbox=mailbox)
        for lead, mailbox in zip(unique, slots, strict=False)
    ]
    deferred = [lead.id for lead in unique[len(assignments) :]]
    by_mailbox: dict[str, int] = {}
    for a in assignments:
        by_mailbox[a.mailbox] = by_mailbox.get(a.mailbox, 0) + 1

    findings: list[Finding] = []
    if dupes:
        findings.append(
            Finding(
                code="BATCH_DUPLICATE_LEAD",
                severity="info",
                message=f"{len(dupes)} duplicate lead(s) skipped (same id or e-mail)",
                ref=_REF,
            )
        )
    if unique and not assignments:
        findings.append(
            Finding(
                code="BATCH_NO_CAPACITY_TODAY",
                severity="warning",
                message=f"no mailbox has campaign capacity today; all {len(unique)} lead(s) "
                "deferred",
                ref=_REF,
            )
        )
    elif deferred:
        findings.append(
            Finding(
                code="BATCH_DEFERRED",
                severity="info",
                message=f"{len(assignments)} lead(s) today, {len(deferred)} deferred "
                f"(capacity today {plan.capacity_today})",
                ref=_REF,
            )
        )
    return BatchPlan(
        capacity_today=plan.capacity_today,
        assignments=assignments,
        deferred=deferred,
        duplicates=dupes,
        by_mailbox=by_mailbox,
        findings=findings,
    )
