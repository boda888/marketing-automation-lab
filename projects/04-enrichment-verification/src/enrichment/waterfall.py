"""Waterfall enrichment: ordered providers, explicit run condition, first acceptable answer wins.

    record ──► run_if? ──no──► NOT_RUN (no calls, no credits)
                 │yes
                 ▼
    for provider in order (cheap first):
        fixture + paid provider ─► skipped_fixture
        no complete input set   ─► skipped_missing_inputs
        max_cost > budget left  ─► skipped_budget
        same inputs answered before (cache) ─► replay, no call, no credits
        call ─► value accepted  ─► FOUND, stop, answered_by = provider
              ─► value rejected / none / ProviderError ─► next provider
              ─► ProviderTimeout ─► outcome_unknown (budget booked at max_cost) ─► next provider

Only the fields of the provider's matched input set are sent, and only after placeholders
were cleaned to ``None`` (see ``LeadRecord``), so ``"Unknown"`` never reaches a paid call.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from enrichment.budget import CreditBudget, ResultCache
from enrichment.models import (
    SKIPPED,
    Attempt,
    AttemptStatus,
    LeadRecord,
    ProviderError,
    ProviderResult,
    ProviderTimeout,
    WaterfallOutcome,
    WaterfallStatus,
)


@runtime_checkable
class Provider(Protocol):
    """One enrichment source for one output field.

    ``input_sets`` lists alternative sets of required input fields, in preference order
    (e.g. ``(("linkedin_url",), ("first_name", "last_name", "company_domain"))``); the
    provider runs only when one set is complete. ``max_cost`` is the most one call can be
    billed (charged-per-lookup and charged-only-on-hit alike); budgets are checked against it
    before the call.
    """

    name: str
    output_field: str
    input_sets: tuple[tuple[str, ...], ...]
    paid: bool
    max_cost: float

    def lookup(self, inputs: Mapping[str, str]) -> ProviderResult: ...


def select_inputs(provider: Provider, record: LeadRecord) -> dict[str, str] | None:
    """The first complete input set for ``provider`` on ``record``, or ``None``."""
    for names in provider.input_sets:
        values = {n: record.get(n) for n in names}
        if all(values.values()):
            return values  # type: ignore[return-value]
    return None


def _field_missing(target: str) -> Callable[[LeadRecord], bool]:
    return lambda record: record.get(target) is None


@dataclass(frozen=True)
class Waterfall:
    """Ordered providers for one field. Default condition: run only while the field is empty."""

    field: str
    providers: Sequence[Provider]
    run_if: Callable[[LeadRecord], bool] | None = None
    accept: Callable[[str], bool] | None = None

    def __post_init__(self) -> None:
        if not self.providers:
            raise ValueError(f"waterfall {self.field!r}: no providers")
        names = tuple(p.name for p in self.providers)
        if len(set(names)) != len(names):
            raise ValueError(f"waterfall {self.field!r}: duplicate provider names {names}")
        for p in self.providers:
            if p.output_field != self.field:
                raise ValueError(
                    f"provider {p.name!r} returns {p.output_field!r}, "
                    f"waterfall needs {self.field!r}"
                )
            if not p.input_sets or not all(p.input_sets):
                raise ValueError(f"provider {p.name!r}: every input set needs at least one field")

    def should_run(self, record: LeadRecord) -> bool:
        return (self.run_if or _field_missing(self.field))(record)

    def run(
        self,
        record: LeadRecord,
        *,
        budget: CreditBudget | None = None,
        cache: ResultCache | None = None,
    ) -> WaterfallOutcome:
        """Run on one record. Pass the record's ``budget`` to every waterfall run on it."""
        outcome = WaterfallOutcome(
            record_id=record.record_id, field=self.field, status=WaterfallStatus.NOT_RUN
        )
        if not self.should_run(record):
            return outcome

        accept = self.accept or (lambda _value: True)
        called = False
        for provider in self.providers:
            attempt, value = self._attempt(provider, record, accept, budget, cache)
            outcome.attempts.append(attempt)
            if attempt.status is AttemptStatus.FOUND:
                outcome.status = WaterfallStatus.FOUND
                outcome.answered_by = provider.name
                outcome.value = value
                return outcome
            called = called or attempt.status not in SKIPPED
        statuses = {a.status for a in outcome.attempts}
        if AttemptStatus.OUTCOME_UNKNOWN in statuses:
            outcome.status = WaterfallStatus.OUTCOME_UNKNOWN
        elif AttemptStatus.SKIPPED_BUDGET in statuses:
            outcome.status = WaterfallStatus.BUDGET_SPENT
        elif called:
            outcome.status = WaterfallStatus.NOT_FOUND
        else:
            outcome.status = WaterfallStatus.NO_ELIGIBLE_PROVIDER
        return outcome

    def run_many(
        self,
        records: Sequence[LeadRecord],
        *,
        budget_per_record: float | None = None,
        cache: ResultCache | None = None,
    ) -> list[WaterfallOutcome]:
        seen: set[str] = set()
        for r in records:
            if r.record_id in seen:
                raise ValueError(f"duplicate record_id {r.record_id!r}: ids must be stable keys")
            seen.add(r.record_id)
        return [
            self.run(
                r,
                budget=None if budget_per_record is None else CreditBudget(budget_per_record),
                cache=cache,
            )
            for r in records
        ]

    def _attempt(
        self,
        provider: Provider,
        record: LeadRecord,
        accept: Callable[[str], bool],
        budget: CreditBudget | None,
        cache: ResultCache | None,
    ) -> tuple[Attempt, str | None]:
        if provider.paid and record.is_fixture:
            return Attempt(provider=provider.name, status=AttemptStatus.SKIPPED_FIXTURE), None
        inputs = select_inputs(provider, record)
        if inputs is None:
            needed = " | ".join("+".join(s) for s in provider.input_sets)
            return Attempt(
                provider=provider.name,
                status=AttemptStatus.SKIPPED_MISSING_INPUTS,
                detail=f"needs {needed}",
            ), None
        used = list(inputs)
        cached = cache.get(provider.name, self.field, inputs) if cache else None
        if cached is None and budget is not None and not budget.can_afford(provider.max_cost):
            return Attempt(
                provider=provider.name,
                status=AttemptStatus.SKIPPED_BUDGET,
                detail=f"max_cost {provider.max_cost:g} > remaining {budget.remaining:g}",
            ), None
        if cached is not None:
            return self._judge(provider, cached, used, accept, cached=True)
        try:
            result = provider.lookup(inputs)
        except ProviderTimeout as exc:
            if budget is not None:
                budget.charge(max(exc.cost, provider.max_cost))
            return Attempt(
                provider=provider.name,
                status=AttemptStatus.OUTCOME_UNKNOWN,
                inputs_used=used,
                cost=exc.cost,
                detail=f"{exc}; up to {provider.max_cost:g} credits may have been charged",
            ), None
        except ProviderError as exc:
            if budget is not None:
                budget.charge(exc.cost)
            return Attempt(
                provider=provider.name,
                status=AttemptStatus.ERROR,
                inputs_used=used,
                cost=exc.cost,
                detail=str(exc),
            ), None
        if budget is not None:
            budget.charge(result.cost)
        if cache is not None:
            cache.put(provider.name, self.field, inputs, result)
        return self._judge(provider, result, used, accept, cached=False)

    @staticmethod
    def _judge(
        provider: Provider,
        result: ProviderResult,
        used: list[str],
        accept: Callable[[str], bool],
        *,
        cached: bool,
    ) -> tuple[Attempt, str | None]:
        if result.value is None:
            status = AttemptStatus.NOT_FOUND
        elif accept(result.value):
            status = AttemptStatus.FOUND
        else:
            status = AttemptStatus.REJECTED
        attempt = Attempt(
            provider=provider.name,
            status=status,
            inputs_used=used,
            cost=0.0 if cached else result.cost,
            detail=result.provider_status,
            cached=cached,
        )
        return attempt, result.value if status is AttemptStatus.FOUND else None
