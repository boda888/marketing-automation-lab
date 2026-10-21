"""Offline providers for tests and demos: deterministic, no network, no keys."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from enrichment.models import ProviderError, ProviderResult


@dataclass
class FakeProvider:
    """Answers from a lookup table keyed by any input value; records every call.

    ``answers`` maps an input value (e.g. a LinkedIn URL or a domain) to the value returned.
    Keys in ``errors`` raise ``ProviderError`` instead. ``charge_on_miss=False`` models
    providers that bill only when they return data.
    """

    name: str
    output_field: str
    input_sets: tuple[tuple[str, ...], ...]
    answers: Mapping[str, str | None] = field(default_factory=dict)
    errors: frozenset[str] = frozenset()
    cost_per_call: float = 1.0
    charge_on_miss: bool = True
    paid: bool = False
    provider_status: str | None = None
    calls: list[dict[str, str]] = field(default_factory=list)

    def lookup(self, inputs: Mapping[str, str]) -> ProviderResult:
        self.calls.append(dict(inputs))
        for value in inputs.values():
            if value in self.errors:
                raise ProviderError(f"{self.name}: simulated 503", cost=0.0)
        answer = next((self.answers[v] for v in inputs.values() if v in self.answers), None)
        result = ProviderResult(value=answer, provider_status=self.provider_status)
        charged = self.cost_per_call if (result.value or self.charge_on_miss) else 0.0
        return result.model_copy(update={"cost": charged})
