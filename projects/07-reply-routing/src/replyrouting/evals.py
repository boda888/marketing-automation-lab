"""Labelled eval set + scorer for the whole classification path (rules, then the LLM stage).

One case = one reply with the class a person would give it (``expected``: a ``ReplyClass``
value, or ``"review"`` when the right outcome is a person deciding). Cases the rules leave
open carry a scripted model answer (``llm``) that ``FakeLLMClient`` replays, so the offline
run measures the rules plus the validation / fallback logic around the model — not a model.
Swap ``client_for`` for a live client to measure one (same cases, same scorer).

Errors do not cost the same: an unsubscribe or bounce that is missed keeps mailing someone
who asked to stop, and a "no" scored as interested sends a rep after the wrong person. So the
report counts those separately (``critical`` / ``false_hot``) and the default gate allows
none of them, whatever the accuracy.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .classify import classify
from .llm import FakeLLMClient, IntentResult, LLMClient, LLMError, LLMResponse, classify_intent
from .models import Channel, InboundReply, ReplyClass

REVIEW = "review"
LABELS = tuple(c.value for c in ReplyClass) + (REVIEW,)
RULE_CLASSES = frozenset(
    {
        ReplyClass.BOUNCE.value,
        ReplyClass.OUT_OF_OFFICE.value,
        ReplyClass.UNSUBSCRIBE.value,
        ReplyClass.WRONG_PERSON.value,
        ReplyClass.REFERRAL.value,
        ReplyClass.NO_MEANINGFUL_REPLY.value,
    }
)
# A miss here keeps contacting someone who must not be contacted (or the wrong address).
CRITICAL_CLASSES = frozenset(
    {ReplyClass.UNSUBSCRIBE.value, ReplyClass.BOUNCE.value, ReplyClass.WRONG_PERSON.value}
)
HOT = frozenset({ReplyClass.INTERESTED.value, ReplyClass.QUESTION.value})
DEFAULT_RECEIVED = datetime(2026, 11, 15, 9, 0, tzinfo=UTC)


@dataclass(frozen=True)
class EvalCase:
    id: str
    reply: InboundReply
    expected: str
    llm: tuple[LLMResponse, ...] = ()
    tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class CaseResult:
    id: str
    expected: str
    predicted: str
    rule: str
    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    review_reason: str = ""
    tags: tuple[str, ...] = ()

    @property
    def correct(self) -> bool:
        return self.expected == self.predicted


def _parse_case(raw: dict, line_no: int) -> EvalCase:
    where = f"line {line_no}"
    for key in ("id", "expected", "body"):
        if key not in raw:
            raise ValueError(f"{where}: missing {key!r}")
    if raw["expected"] not in LABELS:
        raise ValueError(f"{where}: expected {raw['expected']!r} not one of {', '.join(LABELS)}")
    if raw["expected"] == ReplyClass.UNCLASSIFIED.value:
        raise ValueError(f"{where}: 'unclassified' is not a label a person gives; use 'review'")
    received = raw.get("received_at")
    reply = InboundReply(
        channel=Channel(raw.get("channel", "email")),
        received_at=datetime.fromisoformat(received) if received else DEFAULT_RECEIVED,
        body_text=raw["body"],
        subject=raw.get("subject", ""),
        from_address=raw.get("from_address", ""),
        headers=dict(raw.get("headers", {})),
        vendor=raw.get("vendor", ""),
        vendor_label=raw.get("vendor_label"),
        person_email=raw.get("person_email"),
        person_linkedin_url=raw.get("person_linkedin_url"),
    )
    llm = []
    for item in raw.get("llm", []):
        text = item["text"] if isinstance(item["text"], str) else json.dumps(item["text"])
        llm.append(
            LLMResponse(
                text,
                item.get("stop_reason", "end_turn"),
                int(item.get("input_tokens", 0)),
                int(item.get("output_tokens", 0)),
            )
        )
    return EvalCase(raw["id"], reply, raw["expected"], tuple(llm), tuple(raw.get("tags", [])))


def load_cases(path: str | Path) -> list[EvalCase]:
    """Read a JSONL eval set; blank lines and ``#`` comments are skipped, ids must be unique."""
    cases: list[EvalCase] = []
    seen: set[str] = set()
    for n, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        case = _parse_case(json.loads(line), n)
        if case.id in seen:
            raise ValueError(f"line {n}: duplicate id {case.id!r}")
        seen.add(case.id)
        cases.append(case)
    return cases


class _NoReplay:
    """A case the rules left open but which has no scripted answer: fail like a provider."""

    def complete(self, system: str, user: str, schema: dict, max_tokens: int) -> LLMResponse:
        raise LLMError("no scripted answer for this case")


def replay_client(case: EvalCase) -> LLMClient:
    return FakeLLMClient(list(case.llm)) if case.llm else _NoReplay()


def _label(result_class: ReplyClass, needs_review: bool) -> str:
    return REVIEW if needs_review else result_class.value


def run_case(case: EvalCase, client_for: Callable[[EvalCase], LLMClient]) -> CaseResult:
    det = classify(case.reply)
    if not det.needs_llm:
        return CaseResult(
            case.id, case.expected, _label(det.reply_class, False), det.rule, tags=case.tags
        )
    res: IntentResult = classify_intent(case.reply, det, client_for(case))
    c = res.classification
    return CaseResult(
        case.id,
        case.expected,
        _label(c.reply_class, c.needs_human_review),
        c.rule,
        llm_calls=len(res.attempts),
        input_tokens=res.input_tokens,
        output_tokens=res.output_tokens,
        review_reason=c.review_reason,
        tags=case.tags,
    )


@dataclass(frozen=True)
class ClassScore:
    support: int  # cases with this expected label
    predicted: int  # cases given this label
    correct: int

    @property
    def precision(self) -> float | None:
        return self.correct / self.predicted if self.predicted else None

    @property
    def recall(self) -> float | None:
        return self.correct / self.support if self.support else None


@dataclass(frozen=True)
class EvalReport:
    results: tuple[CaseResult, ...]
    confusion: dict[str, dict[str, int]]  # expected -> predicted -> count
    per_class: dict[str, ClassScore]
    critical: tuple[str, ...]  # ids: unsubscribe / bounce / wrong person not caught
    false_hot: tuple[str, ...]  # ids: scored interested / question, a person would not
    wrong_confident: tuple[str, ...]  # ids: a wrong class, not sent to review

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def accuracy(self) -> float:
        return sum(r.correct for r in self.results) / self.total if self.total else 0.0

    @property
    def review_rate(self) -> float:
        reviewed = sum(r.predicted == REVIEW for r in self.results)
        return reviewed / self.total if self.total else 0.0

    @property
    def rule_accuracy(self) -> float | None:
        """Accuracy over cases a rule should decide (denominator: those cases only)."""
        rule = [r for r in self.results if r.expected in RULE_CLASSES]
        return sum(r.correct for r in rule) / len(rule) if rule else None

    @property
    def llm_calls(self) -> int:
        return sum(r.llm_calls for r in self.results)

    @property
    def input_tokens(self) -> int:
        return sum(r.input_tokens for r in self.results)

    @property
    def output_tokens(self) -> int:
        return sum(r.output_tokens for r in self.results)

    def to_dict(self) -> dict:
        def rnd(x: float | None) -> float | None:
            return None if x is None else round(x, 4)

        return {
            "total": self.total,
            "accuracy": rnd(self.accuracy),
            "rule_accuracy": rnd(self.rule_accuracy),
            "review_rate": rnd(self.review_rate),
            "llm_calls": self.llm_calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "critical": list(self.critical),
            "false_hot": list(self.false_hot),
            "wrong_confident": list(self.wrong_confident),
            "per_class": {
                k: {
                    "support": v.support,
                    "predicted": v.predicted,
                    "correct": v.correct,
                    "precision": rnd(v.precision),
                    "recall": rnd(v.recall),
                }
                for k, v in self.per_class.items()
            },
            "confusion": self.confusion,
            "errors": [
                {"id": r.id, "expected": r.expected, "predicted": r.predicted, "rule": r.rule}
                for r in self.results
                if not r.correct
            ],
        }


def score(results: Iterable[CaseResult]) -> EvalReport:
    results = tuple(results)
    confusion: dict[str, dict[str, int]] = {}
    for r in results:
        row = confusion.setdefault(r.expected, {})
        row[r.predicted] = row.get(r.predicted, 0) + 1
    per_class = {}
    for label in LABELS:
        support = sum(r.expected == label for r in results)
        predicted = sum(r.predicted == label for r in results)
        if support or predicted:
            correct = sum(r.expected == label and r.correct for r in results)
            per_class[label] = ClassScore(support, predicted, correct)
    critical = tuple(r.id for r in results if r.expected in CRITICAL_CLASSES and not r.correct)
    false_hot = tuple(
        r.id
        for r in results
        if r.predicted in HOT and r.expected not in HOT and r.expected != REVIEW
    )
    wrong_confident = tuple(r.id for r in results if not r.correct and r.predicted != REVIEW)
    return EvalReport(results, confusion, per_class, critical, false_hot, wrong_confident)


def run_eval(
    cases: Iterable[EvalCase], client_for: Callable[[EvalCase], LLMClient] = replay_client
) -> EvalReport:
    return score(run_case(c, client_for) for c in cases)


@dataclass(frozen=True)
class EvalGate:
    """Pass / fail thresholds for a run; defaults: no critical miss, no false hot lead."""

    min_accuracy: float = 0.9
    min_rule_accuracy: float = 1.0
    max_review_rate: float = 0.25
    max_critical: int = 0
    max_false_hot: int = 0
    tags_must_pass: tuple[str, ...] = field(default_factory=tuple)

    def check(self, report: EvalReport) -> list[str]:
        failures = []
        if report.accuracy < self.min_accuracy:
            failures.append(f"accuracy {report.accuracy:.3f} < {self.min_accuracy}")
        rule = report.rule_accuracy
        if rule is not None and rule < self.min_rule_accuracy:
            failures.append(f"rule_accuracy {rule:.3f} < {self.min_rule_accuracy}")
        if report.review_rate > self.max_review_rate:
            failures.append(f"review_rate {report.review_rate:.3f} > {self.max_review_rate}")
        if len(report.critical) > self.max_critical:
            failures.append(f"critical misses: {', '.join(report.critical)}")
        if len(report.false_hot) > self.max_false_hot:
            failures.append(f"false hot leads: {', '.join(report.false_hot)}")
        for tag in self.tags_must_pass:
            bad = [r.id for r in report.results if tag in r.tags and not r.correct]
            if bad:
                failures.append(f"tag {tag!r} failed: {', '.join(bad)}")
        return failures


def main(argv: list[str] | None = None) -> int:
    """``python -m replyrouting.evals CASES.jsonl``: JSON report on stdout, exit 1 on a fail."""
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: python -m replyrouting.evals CASES.jsonl", file=sys.stderr)
        return 2
    report = run_eval(load_cases(args[0]))
    failures = EvalGate().check(report)
    out = report.to_dict() | {"gate": {"passed": not failures, "failures": failures}}
    print(json.dumps(out, indent=2, sort_keys=True))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
