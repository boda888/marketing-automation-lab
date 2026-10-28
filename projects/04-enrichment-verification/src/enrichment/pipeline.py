"""One batch, one pass: dedupe -> email waterfall -> precheck -> ladder -> next action.

    candidates (several sources per person)
        │ dedupe_contacts            one person record, provenance, identity findings
        ▼
    email waterfall per person     only where the email is still empty (default run_if),
        │                            one credit budget per person, one result cache per batch
        ▼
    precheck (no SMTP)             syntax, DNS, disposable, role, catch-all; one DNS cache
        │                            per batch; catch-all domains from earlier verifier checks
        ▼
    ladder                         found / checked / verified / invalid from that address's
        │                            own checks + the precheck
        ▼
    next action                    suppress > review > no_email > send > verify

PRACTICE: every step used to read its own copy of the row, so QA fixed the employer and the
email step still used the old one; a found email was reported as valid; two people could leave
with the same address. Here every step reads the same merged person record, the email is
re-checked against the company domain *after* the waterfall, an address shared by two people
sends both to review, and only a ``verified`` address with no findings is ``send``.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum

from pydantic import BaseModel, Field

from enrichment.budget import ResultCache
from enrichment.coverage import CoverageReport, coverage
from enrichment.dedupe import ContactCandidate, PersonRecord, dedupe_contacts, email_matches_company
from enrichment.ladder import (
    DEFAULT_MAX_AGE,
    EmailAssessment,
    EmailCheck,
    EmailStatus,
    LadderSummary,
    assess_email,
    ladder_summary,
    normalise_email,
)
from enrichment.models import LeadRecord, WaterfallOutcome, WaterfallStatus
from enrichment.precheck import (
    DISPOSABLE_SAMPLE,
    ROLE_LOCAL_PARTS,
    EmailPrecheck,
    PrecheckReport,
    Resolver,
    catch_all_domains,
)
from enrichment.waterfall import Waterfall

EMAIL_FIELD = "email"

# Findings the pipeline adds on top of dedupe's own.
EMAIL_DOMAIN_MISMATCH = "email_domain_mismatch"
EMAIL_SHARED = "email_shared_with_other_person"
OUTCOME_UNKNOWN = "waterfall_outcome_unknown"


class NextAction(StrEnum):
    SEND = "send"  # verified address, no findings
    VERIFY = "verify"  # an address, not verified yet: run an independent verifier first
    REVIEW = "review"  # a person must look: identity findings, shared address, unknown paid call
    SUPPRESS = "suppress"  # the address is invalid or must not be mailed
    NO_EMAIL = "no_email"  # nothing to send to


class PersonResult(BaseModel):
    person: PersonRecord
    email: str | None = Field(description="The address every later step must use")
    email_source: str | None = Field(
        default=None, description="Waterfall provider that found it, or the source it came from"
    )
    waterfall: WaterfallOutcome
    precheck: PrecheckReport | None = None
    assessment: EmailAssessment | None = None
    findings: list[str] = Field(default_factory=list)
    action: NextAction


class BatchResult(BaseModel):
    candidates_in: int
    people: int
    results: list[PersonResult]
    actions: dict[NextAction, int]
    ladder: LadderSummary = Field(description="Over distinct addresses: a shared one counts once")
    coverage: CoverageReport
    cost: float = Field(description="Credits charged by the email waterfall in this batch")


def _action(email: str | None, assessment: EmailAssessment | None, findings: list[str]):
    if assessment is not None and assessment.status is EmailStatus.INVALID:
        return NextAction.SUPPRESS
    if findings:
        return NextAction.REVIEW
    if email is None:
        return NextAction.NO_EMAIL
    if assessment is not None and assessment.sendable:
        return NextAction.SEND
    return NextAction.VERIFY


@dataclass
class EnrichmentPipeline:
    """Wiring for one batch. A fresh precheck (DNS cache) and result cache per ``run``,
    unless ``cache`` is passed to share answers across batches on purpose."""

    waterfall: Waterfall
    resolver: Resolver
    source_priority: Sequence[str] = ()
    budget_per_record: float | None = None
    cache: ResultCache | None = None
    disposable_domains: Iterable[str] = DISPOSABLE_SAMPLE
    role_local_parts: Iterable[str] = ROLE_LOCAL_PARTS
    known_catch_all: Iterable[str] = field(default_factory=tuple)
    max_age: timedelta = DEFAULT_MAX_AGE
    require_independent: bool = True

    def __post_init__(self) -> None:
        if self.waterfall.field != EMAIL_FIELD:
            raise ValueError(
                f"pipeline needs an {EMAIL_FIELD!r} waterfall, got {self.waterfall.field!r}"
            )

    def run(
        self,
        candidates: Sequence[ContactCandidate],
        checks: Sequence[EmailCheck] = (),
        *,
        now: datetime,
    ) -> BatchResult:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now needs a timezone")
        people = dedupe_contacts(candidates, source_priority=self.source_priority)
        records = [LeadRecord(record_id=p.person_id, fields=p.fields) for p in people]
        outcomes = self.waterfall.run_many(
            records,
            budget_per_record=self.budget_per_record,
            cache=self.cache if self.cache is not None else ResultCache(),
        )
        precheck = EmailPrecheck(
            self.resolver,
            disposable_domains=self.disposable_domains,
            role_local_parts=self.role_local_parts,
            catch_all_domains=[*self.known_catch_all, *catch_all_domains(checks)],
        )
        by_address: dict[str, list[EmailCheck]] = {}
        for check in checks:
            by_address.setdefault(normalise_email(check.email), []).append(check)

        owners = Counter(
            normalise_email(email)
            for person, outcome in zip(people, outcomes, strict=True)
            if (email := self._email(person, outcome)[0])
        )
        results: list[PersonResult] = []
        for person, outcome in zip(people, outcomes, strict=True):
            email, source = self._email(person, outcome)
            findings = [f for f in person.findings if f != EMAIL_DOMAIN_MISMATCH]
            if outcome.status is WaterfallStatus.OUTCOME_UNKNOWN:
                findings.append(OUTCOME_UNKNOWN)
            report = assessment = None
            if email is not None:
                if email_matches_company(email, person.fields.get("company_domain")) is False:
                    findings.append(EMAIL_DOMAIN_MISMATCH)
                if owners[normalise_email(email)] > 1:
                    findings.append(EMAIL_SHARED)
                report = precheck.check(email)
                own = [*by_address.get(normalise_email(email), []), report.to_check(checked_at=now)]
                assessment = assess_email(
                    email,
                    source or "unknown-source",
                    own,
                    now=now,
                    max_age=self.max_age,
                    require_independent=self.require_independent,
                )
            results.append(
                PersonResult(
                    person=person,
                    email=email,
                    email_source=source,
                    waterfall=outcome,
                    precheck=report,
                    assessment=assessment,
                    findings=findings,
                    action=_action(email, assessment, findings),
                )
            )

        distinct: dict[str, EmailAssessment] = {}
        for r in results:
            if r.assessment is not None:
                distinct.setdefault(normalise_email(r.assessment.email), r.assessment)
        actions = Counter(r.action for r in results)
        return BatchResult(
            candidates_in=len(candidates),
            people=len(people),
            results=results,
            actions={a: actions[a] for a in NextAction},
            ladder=ladder_summary(list(distinct.values())),
            coverage=coverage(outcomes),
            cost=sum(o.cost for o in outcomes),
        )

    @staticmethod
    def _email(person: PersonRecord, outcome: WaterfallOutcome) -> tuple[str | None, str | None]:
        if outcome.status is WaterfallStatus.FOUND and outcome.value:
            return outcome.value, outcome.answered_by
        existing = person.provenance.get(EMAIL_FIELD)
        return (existing.value, existing.source) if existing else (None, None)
