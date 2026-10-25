"""Before and after a waterfall run: can each provider be fed, and what did it deliver?

- ``check_mappings()`` runs *before* any paid call: per provider, how many records have a
  complete input set, which fields are missing, and whether a required field exists in no
  record at all (a mapping typo or an empty column). PRACTICE: empty input mappings left a
  waterfall at ~45% email coverage until someone looked.
- ``coverage()`` summarises outcomes after a run.

Every ratio is a ``Ratio`` that names its denominator, so "coverage" can never silently switch
between input rows, rows the waterfall ran on and provider calls.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from enum import StrEnum

from pydantic import BaseModel, Field

from enrichment.models import (
    SKIPPED,
    AttemptStatus,
    LeadRecord,
    WaterfallOutcome,
    WaterfallStatus,
)
from enrichment.waterfall import Waterfall, select_inputs

RUN_RECORDS = "records the waterfall runs on"
INPUT_RECORDS = "input records"
PROVIDER_CALLS = "calls to this provider"
CACHED = "cached"  # counter key for cache replays, kept apart from attempt statuses


class Ratio(BaseModel):
    numerator: int = Field(ge=0)
    denominator: int = Field(ge=0)
    denominator_name: str

    @property
    def value(self) -> float | None:
        """``None`` when nothing was counted: 0 of 0 is unknown, not 0%."""
        return self.numerator / self.denominator if self.denominator else None

    def __str__(self) -> str:
        pct = "n/a" if self.value is None else f"{self.value:.0%}"
        return f"{self.numerator}/{self.denominator} {self.denominator_name} ({pct})"


class Severity(StrEnum):
    ERROR = "error"
    WARNING = "warning"


class MappingFinding(BaseModel):
    code: str
    severity: Severity
    provider: str | None = None
    message: str


class ProviderReadiness(BaseModel):
    provider: str
    paid: bool
    eligible: Ratio = Field(description="Records with a complete input set, of run records")
    fixture_blocked: int = Field(description="Run records a paid provider must not receive")
    missing_fields: dict[str, int] = Field(
        default_factory=dict,
        description="Per required field: run records lacking it among those not eligible",
    )


class MappingReport(BaseModel):
    field: str
    records_total: int
    records_to_run: int
    providers: list[ProviderReadiness]
    unreachable: Ratio = Field(description="Run records no provider can take")
    findings: list[MappingFinding] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(f.severity is Severity.ERROR for f in self.findings)


def check_mappings(
    waterfall: Waterfall, records: Sequence[LeadRecord], *, min_eligible: float = 0.5
) -> MappingReport:
    """Dry run of the waterfall's input gates: no provider is called."""
    to_run = [r for r in records if waterfall.should_run(r)]
    present = {name for r in records for name, value in r.fields.items() if value is not None}
    findings: list[MappingFinding] = []
    readiness: list[ProviderReadiness] = []
    reachable: set[str] = set()

    for p in waterfall.providers:
        eligible = blocked = 0
        missing: Counter[str] = Counter()
        for r in to_run:
            if p.paid and r.is_fixture:
                blocked += 1
                continue
            if select_inputs(p, r) is not None:
                eligible += 1
                reachable.add(r.record_id)
                continue
            missing.update({n for s in p.input_sets for n in s if r.get(n) is None})
        ratio = Ratio(numerator=eligible, denominator=len(to_run), denominator_name=RUN_RECORDS)
        readiness.append(
            ProviderReadiness(
                provider=p.name,
                paid=p.paid,
                eligible=ratio,
                fixture_blocked=blocked,
                missing_fields=dict(sorted(missing.items())),
            )
        )
        findings.extend(
            _provider_findings(
                p.name, p.input_sets, present if records else None, ratio, min_eligible
            )
        )

    unreachable = Ratio(
        numerator=len(to_run) - len(reachable),
        denominator=len(to_run),
        denominator_name=RUN_RECORDS,
    )
    if unreachable.numerator:
        findings.append(
            MappingFinding(
                code="RECORDS_WITHOUT_PROVIDER",
                severity=Severity.WARNING,
                message=f"{unreachable} cannot be sent to any provider",
            )
        )
    return MappingReport(
        field=waterfall.field,
        records_total=len(records),
        records_to_run=len(to_run),
        providers=readiness,
        unreachable=unreachable,
        findings=findings,
    )


def _provider_findings(
    name: str,
    input_sets: tuple[tuple[str, ...], ...],
    present: set[str] | None,
    ratio: Ratio,
    min_eligible: float,
) -> list[MappingFinding]:
    out: list[MappingFinding] = []
    if present is not None:  # None: no records, nothing to judge a mapping by
        absent_by_set = [[n for n in s if n not in present] for s in input_sets]
        # One dead alternative is a warning while another set can still be fed; all dead = error.
        severity = Severity.ERROR if all(absent_by_set) else Severity.WARNING
        for s, absent in zip(input_sets, absent_by_set, strict=True):
            if absent:
                out.append(
                    MappingFinding(
                        code="INPUT_FIELD_EMPTY_EVERYWHERE",
                        severity=severity,
                        provider=name,
                        message=f"input set {'+'.join(s)}: {', '.join(absent)} is empty in "
                        "every record (unmapped column or typo)",
                    )
                )
    if ratio.denominator and ratio.numerator == 0:
        out.append(
            MappingFinding(
                code="NO_ELIGIBLE_RECORDS",
                severity=Severity.ERROR,
                provider=name,
                message=f"{ratio}: this provider would never be called",
            )
        )
    elif ratio.value is not None and ratio.value < min_eligible:
        out.append(
            MappingFinding(
                code="LOW_ELIGIBILITY",
                severity=Severity.WARNING,
                provider=name,
                message=f"only {ratio} have a complete input set",
            )
        )
    return out


class ProviderCoverage(BaseModel):
    provider: str
    calls: int
    answered: Ratio = Field(description="Accepted answers, of calls to this provider")
    rejected: int
    errors: int
    unknown: int = Field(description="Timed-out calls whose outcome (and charge) is unknown")
    skipped: int
    cached: int = Field(description="Answers replayed from the cache: no call, no credits")
    cost: float


class CoverageReport(BaseModel):
    field: str
    found_of_run: Ratio
    found_of_input: Ratio
    status_counts: dict[str, int]
    providers: list[ProviderCoverage]
    cost: float
    cost_per_found: float | None = Field(
        description="Credits per record with a value found; None when nothing was found"
    )


def coverage(outcomes: Sequence[WaterfallOutcome]) -> CoverageReport:
    """Coverage and cost from waterfall outcomes, each ratio with its denominator."""
    fields = {o.field for o in outcomes}
    if len(fields) > 1:
        raise ValueError(f"outcomes mix fields {sorted(fields)}: report one waterfall at a time")
    statuses = Counter(o.status for o in outcomes)
    found = statuses[WaterfallStatus.FOUND]
    ran = len(outcomes) - statuses[WaterfallStatus.NOT_RUN]

    per: dict[str, Counter[str]] = {}
    cost: Counter[str] = Counter()
    for o in outcomes:
        for a in o.attempts:
            per.setdefault(a.provider, Counter())[CACHED if a.cached else a.status] += 1
            cost[a.provider] += a.cost
    providers = []
    for name, c in per.items():
        skipped = sum(c[s] for s in SKIPPED)
        calls = c.total() - skipped - c[CACHED]
        providers.append(
            ProviderCoverage(
                provider=name,
                calls=calls,
                answered=Ratio(
                    numerator=c[AttemptStatus.FOUND],
                    denominator=calls,
                    denominator_name=PROVIDER_CALLS,
                ),
                rejected=c[AttemptStatus.REJECTED],
                errors=c[AttemptStatus.ERROR],
                unknown=c[AttemptStatus.OUTCOME_UNKNOWN],
                skipped=skipped,
                cached=c[CACHED],
                cost=cost[name],
            )
        )
    total_cost = sum(cost.values())
    return CoverageReport(
        field=fields.pop() if fields else "",
        found_of_run=Ratio(numerator=found, denominator=ran, denominator_name=RUN_RECORDS),
        found_of_input=Ratio(
            numerator=found, denominator=len(outcomes), denominator_name=INPUT_RECORDS
        ),
        status_counts={s.value: statuses[s] for s in WaterfallStatus},
        providers=providers,
        cost=total_cost,
        cost_per_found=total_cost / found if found else None,
    )
