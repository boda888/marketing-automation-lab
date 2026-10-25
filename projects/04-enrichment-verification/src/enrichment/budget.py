"""Credit budget per record and a result cache, so a waterfall never pays twice or overspends.

- ``CreditBudget``: one per record, shared by every waterfall run on it (email, phone, ...).
  A provider is called only when its worst-case cost (``max_cost``) still fits; otherwise
  the attempt is ``skipped_budget`` and the next, possibly cheaper, provider is tried.
- ``ResultCache``: definite answers (a value or a clean miss) keyed by provider, output field
  and the exact inputs sent. Replays cost nothing and are marked ``cached``. Errors and
  timeouts are never cached: a fault is not an answer, and a timed-out call's outcome is
  unknown.
"""

from __future__ import annotations

from collections.abc import Mapping

from enrichment.models import ProviderResult

_EPSILON = 1e-9

CacheKey = tuple[str, str, tuple[tuple[str, str], ...]]


class CreditBudget:
    """Credits one record may spend across all its waterfalls."""

    def __init__(self, limit: float) -> None:
        if limit < 0:
            raise ValueError(f"budget limit must be >= 0, got {limit}")
        self.limit = float(limit)
        self.spent = 0.0

    @property
    def remaining(self) -> float:
        return max(self.limit - self.spent, 0.0)

    def can_afford(self, cost: float) -> bool:
        return self.spent + cost <= self.limit + _EPSILON

    def charge(self, cost: float) -> None:
        """Book credits actually (or possibly) charged; may overshoot when a provider bills
        more than its declared ``max_cost``, which then blocks every further paid call."""
        if cost < 0:
            raise ValueError(f"cost must be >= 0, got {cost}")
        self.spent += cost


class ResultCache:
    """In-memory cache of provider answers; swap for a persistent store behind the same API."""

    def __init__(self) -> None:
        self._store: dict[CacheKey, ProviderResult] = {}
        self.hits = 0

    @staticmethod
    def key(provider: str, output_field: str, inputs: Mapping[str, str]) -> CacheKey:
        return provider, output_field, tuple(sorted(inputs.items()))

    def get(
        self, provider: str, output_field: str, inputs: Mapping[str, str]
    ) -> ProviderResult | None:
        result = self._store.get(self.key(provider, output_field, inputs))
        if result is not None:
            self.hits += 1
        return result

    def put(
        self, provider: str, output_field: str, inputs: Mapping[str, str], result: ProviderResult
    ) -> None:
        self._store[self.key(provider, output_field, inputs)] = result

    def __len__(self) -> int:
        return len(self._store)
