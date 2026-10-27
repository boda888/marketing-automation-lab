"""Apollo Bulk People Match (``POST /api/v1/people/bulk_match``): 10 people per call, answers
fed back into the waterfall one record at a time.

    records ──► prefetch(records)
                  skip: field filled / fixture / no complete input set   (never sent)
                  same inputs twice ─► sent once
                  chunks of ≤ 10 ─► POST {"details": [...], reveals off}
                     answer ─► one ProviderResult per detail (same rules as People Match)
                     timeout / unalignable answer ─► every record of the chunk: outcome unknown
                     429 / 5xx ─► every record of the chunk: error
                     401 / 403 ─► ApolloAuthError, the run stops
                  ─► BulkReport (per-chunk and per-record outcomes, credits)
    Waterfall("email", [..., bulk]).run(record)
        bulk.lookup(inputs) ─► the prefetched answer; not prefetched ─► one People Match call

Request shape: Apollo's own MCP plugin batches ``apollo_people_bulk_match`` "up to 10 per
call"; ``details`` is a list of People Match bodies. The answer is read as ``matches``, one
entry per detail in request order, ``null`` for a miss (from search summaries, not a docs page;
see the README). Because that order is not documented where the lab could open it, an answer is
used only when it can be aligned: as many entries as details sent, and every returned
``linkedin_url`` equal to the one sent for that slot. Otherwise the whole chunk is an unknown
outcome (it may have been billed), never a guess, so one person's email can never land on
another person's row.

Prefetch only the records that will actually reach Apollo: run the cheaper providers first and
bulk-match the leftovers, or credits are spent on records an earlier provider would answer.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from enrichment.apollo import (
    ApolloAuthError,
    ApolloHTTPError,
    ApolloPeopleMatch,
    build_match_body,
)
from enrichment.models import LeadRecord, ProviderError, ProviderResult, ProviderTimeout
from enrichment.waterfall import select_inputs

BULK_MATCH_PATH = "/people/bulk_match"
MAX_BATCH = 10

InputKey = tuple[tuple[str, str], ...]


def input_key(inputs: Mapping[str, str]) -> InputKey:
    return tuple(sorted(inputs.items()))


def build_bulk_body(batch: Sequence[Mapping[str, str]]) -> dict[str, Any]:
    """``details`` from lab inputs (same mapping as People Match); reveals stay off."""
    if not 1 <= len(batch) <= MAX_BATCH:
        raise ValueError(f"a bulk match takes 1..{MAX_BATCH} people, got {len(batch)}")
    details = []
    for inputs in batch:
        body = build_match_body(inputs)
        del body["reveal_personal_emails"], body["reveal_phone_number"]
        details.append(body)
    return {"details": details, "reveal_personal_emails": False, "reveal_phone_number": False}


def _linkedin(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip().lower().split("?", 1)[0].rstrip("/").removeprefix("https://")


def align_matches(details: Sequence[Mapping[str, Any]], payload: object) -> list[Any]:
    """``matches`` in request order, or ``ValueError`` when they cannot be trusted to line up."""
    matches = payload.get("matches") if isinstance(payload, Mapping) else None
    if not isinstance(matches, list):
        raise ValueError("no 'matches' list in the answer")
    if len(matches) != len(details):
        raise ValueError(f"{len(matches)} matches for {len(details)} details: cannot align")
    for slot, (sent, person) in enumerate(zip(details, matches, strict=True)):
        if person is not None and not isinstance(person, Mapping):
            raise ValueError(f"match {slot} is not an object")
        sent_li = _linkedin(sent.get("linkedin_url"))
        got_li = _linkedin(person.get("linkedin_url")) if person else None
        if sent_li and got_li and sent_li != got_li:
            raise ValueError(f"match {slot} is another LinkedIn profile: answers out of order")
    return matches


@dataclass
class BulkChunk:
    """One ``bulk_match`` request: how many people, and what came of it."""

    size: int
    status: str  # "ok" | "error" | "outcome_unknown"
    detail: str | None = None


@dataclass
class BulkReport:
    """What ``prefetch`` did, per record id. Records that were not sent say why."""

    chunks: list[BulkChunk] = field(default_factory=list)
    sent: dict[str, str] = field(default_factory=dict)  # record_id → provider_status / error
    skipped: dict[str, str] = field(default_factory=dict)  # record_id → reason
    duplicates: int = 0  # records whose inputs were already sent (this or an earlier prefetch)
    credits: float = 0.0  # booked for answers with an email (see ``ApolloPeopleMatch``)

    @property
    def requests(self) -> int:
        return len(self.chunks)


@dataclass
class ApolloBulkMatch:
    """Waterfall provider backed by bulk prefetches; same name, inputs and costs as ``single``.

    A prefetched answer is served once with its credits; a second record with the same inputs
    gets the value at 0 credits (Apollo billed one match). Inputs that were never prefetched
    fall back to one People Match call, unless ``fallback=False`` (then ``ProviderError``).
    """

    single: ApolloPeopleMatch
    batch_size: int = MAX_BATCH
    fallback: bool = True
    _answers: dict[InputKey, ProviderResult | ProviderError] = field(default_factory=dict)
    _served: set[InputKey] = field(default_factory=set)

    def __post_init__(self) -> None:
        if not 1 <= self.batch_size <= MAX_BATCH:
            raise ValueError(f"batch_size must be 1..{MAX_BATCH}, got {self.batch_size}")

    @property
    def name(self) -> str:
        return self.single.name

    @property
    def output_field(self) -> str:
        return self.single.output_field

    @property
    def input_sets(self) -> tuple[tuple[str, ...], ...]:
        return self.single.input_sets

    @property
    def paid(self) -> bool:
        return self.single.paid

    @property
    def max_cost(self) -> float:
        return self.single.max_cost

    def prefetch(
        self,
        records: Sequence[LeadRecord],
        *,
        run_if: Callable[[LeadRecord], bool] | None = None,
    ) -> BulkReport:
        """Bulk-match every record the waterfall would send here. Default ``run_if``: the
        output field is still empty. Auth errors raise; other faults are kept per record."""
        report = BulkReport()
        should_run = run_if or (lambda r: r.get(self.output_field) is None)
        pending: dict[InputKey, tuple[dict[str, str], list[str]]] = {}
        for record in records:
            if not should_run(record):
                report.skipped[record.record_id] = "run_if false"
                continue
            if self.paid and record.is_fixture:
                report.skipped[record.record_id] = "fixture"
                continue
            inputs = select_inputs(self, record)
            if inputs is None:
                report.skipped[record.record_id] = "missing inputs"
                continue
            key = input_key(inputs)
            if key in pending or key in self._answers:
                report.duplicates += 1
                pending.setdefault(key, (inputs, []))[1].append(record.record_id)
                continue
            pending[key] = (inputs, [record.record_id])

        todo = [(k, v) for k, v in pending.items() if k not in self._answers]
        for key, (_inputs, ids) in pending.items():
            if key in self._answers:
                for record_id in ids:
                    report.sent[record_id] = self._label(self._answers[key])
        for start in range(0, len(todo), self.batch_size):
            self._run_chunk(todo[start : start + self.batch_size], report)
        return report

    def _run_chunk(
        self,
        chunk: list[tuple[InputKey, tuple[dict[str, str], list[str]]]],
        report: BulkReport,
    ) -> None:
        body = build_bulk_body([inputs for _key, (inputs, _ids) in chunk])
        outcomes: list[ProviderResult | ProviderError]
        try:
            payload = self.single.transport.post(BULK_MATCH_PATH, body)
        except TimeoutError as exc:
            report.chunks.append(BulkChunk(len(chunk), "outcome_unknown", str(exc)))
            outcomes = [ProviderTimeout(f"{self.name} bulk: {exc}") for _ in chunk]
        except ApolloHTTPError as exc:
            if exc.status in (401, 403):
                raise ApolloAuthError(f"{self.name} bulk: {exc}") from exc
            report.chunks.append(BulkChunk(len(chunk), "error", str(exc)))
            outcomes = [ProviderError(f"{self.name} bulk: {exc}") for _ in chunk]
        else:
            try:
                matches = align_matches(body["details"], payload)
            except ValueError as exc:
                # Apollo answered, so the chunk may have been billed, but no answer is
                # attributable: an unknown outcome (budget booked at max_cost), never a miss.
                report.chunks.append(BulkChunk(len(chunk), "outcome_unknown", str(exc)))
                outcomes = [ProviderTimeout(f"{self.name} bulk: {exc}") for _ in chunk]
            else:
                report.chunks.append(BulkChunk(len(chunk), "ok"))
                outcomes = [
                    self.single._parse({"person": person}, sent_domain=detail.get("domain"))
                    for detail, person in zip(body["details"], matches, strict=True)
                ]
        for (key, (_inputs, ids)), outcome in zip(chunk, outcomes, strict=True):
            self._answers[key] = outcome
            if isinstance(outcome, ProviderResult):
                report.credits += outcome.cost
            for record_id in ids:
                report.sent[record_id] = self._label(outcome)

    @staticmethod
    def _label(outcome: ProviderResult | ProviderError) -> str:
        if isinstance(outcome, ProviderResult):
            return outcome.provider_status or "apollo:no_status"
        return "outcome_unknown" if isinstance(outcome, ProviderTimeout) else "error"

    def lookup(self, inputs: Mapping[str, str]) -> ProviderResult:
        key = input_key(inputs)
        outcome = self._answers.get(key)
        if outcome is None:
            if not self.fallback:
                raise ProviderError(f"{self.name}: not prefetched and fallback is off")
            return self.single.lookup(inputs)
        if isinstance(outcome, ProviderError):
            raise type(outcome)(str(outcome))
        if key in self._served:
            return outcome.model_copy(update={"cost": 0.0})
        self._served.add(key)
        return outcome
