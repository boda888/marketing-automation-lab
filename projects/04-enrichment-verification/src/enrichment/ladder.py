"""Email status ladder: found -> checked (checker + date) -> verified, never by arithmetic.

PRACTICE: a "found" email was reported as "valid", and the rows left after removing the
invalid ones were called "verified". Here a status is earned per address, from explicit
checks of *that* address:

    found     an address and the provider that supplied it; no usable check yet
    checked   a named checker looked at it on a known date, but the verdict is not a clean
              "valid" from an independent checker (catch-all, unknown, or the finder's own label)
    verified  the deciding check says valid, it is fresh, and the checker is not the finder
    invalid   the deciding check says invalid or do-not-mail (spam trap, abuse, ...)

Rules:
- Checks of another address (an old employer's email, a typo variant) are ignored.
- Checks older than ``max_age`` do not count: a stale "valid" falls back to ``found``.
- The deciding check is the newest fresh check with a conclusive result; ``unknown`` decides
  only when nothing else is fresh. Same timestamp: the most cautious result wins.
- The finder's own status (``ProviderResult.provider_status``) is context, not truth: by default
  a check by the provider that found the address cannot verify it.
- A check dated after ``now`` is a data error and raises: dates are evidence.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from datetime import datetime, timedelta
from enum import StrEnum

from pydantic import BaseModel, Field, field_validator

from enrichment.coverage import Ratio
from enrichment.models import WaterfallOutcome, WaterfallStatus, clean

EMAILS_FOUND = "emails found"
DEFAULT_MAX_AGE = timedelta(days=90)


class CheckResult(StrEnum):
    VALID = "valid"
    INVALID = "invalid"
    CATCH_ALL = "catch_all"  # the domain accepts every address: deliverability unknowable
    UNKNOWN = "unknown"  # the checker could not decide (timeout, greylisting, ...)
    DO_NOT_MAIL = "do_not_mail"  # deliverable but must not be mailed (trap, abuse, ...)


# Higher = more cautious; breaks ties between checks with the same timestamp.
_CAUTION = {
    CheckResult.VALID: 0,
    CheckResult.CATCH_ALL: 1,
    CheckResult.UNKNOWN: 2,
    CheckResult.INVALID: 3,
    CheckResult.DO_NOT_MAIL: 4,
}

# ZeroBounce ``status`` values (zerobouncesdk 2.2.4, ZBValidateStatus).
ZEROBOUNCE_STATUS = {
    "valid": CheckResult.VALID,
    "invalid": CheckResult.INVALID,
    "catch-all": CheckResult.CATCH_ALL,
    "unknown": CheckResult.UNKNOWN,
    "": CheckResult.UNKNOWN,
    "spamtrap": CheckResult.DO_NOT_MAIL,
    "abuse": CheckResult.DO_NOT_MAIL,
    "do_not_mail": CheckResult.DO_NOT_MAIL,
}


class EmailStatus(StrEnum):
    FOUND = "found"
    CHECKED = "checked"
    VERIFIED = "verified"
    INVALID = "invalid"


def normalise_email(email: str) -> str:
    """Compare addresses case-insensitively and without surrounding blanks."""
    return email.strip().lower()


class EmailCheck(BaseModel):
    """One verification of one address by one named checker at one time."""

    email: str = Field(min_length=3)
    checker: str = Field(min_length=1, description="Provider that ran the check")
    checked_at: datetime = Field(description="When the check ran; timezone required")
    result: CheckResult
    provider_status: str | None = Field(
        default=None, description="The checker's own code (e.g. 'catch-all/role_based')"
    )

    @field_validator("checker")
    @classmethod
    def _checker_named(cls, v: str) -> str:
        cleaned = clean(v)
        if cleaned is None:
            raise ValueError(f"checker must name a provider, not {v!r}")
        return cleaned

    @field_validator("checked_at")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None or v.utcoffset() is None:
            raise ValueError("checked_at needs a timezone: a check date must be unambiguous")
        return v

    @classmethod
    def from_zerobounce(
        cls, email: str, status: str, sub_status: str = "", *, checked_at: datetime
    ) -> EmailCheck:
        """Map a ZeroBounce validate result; unmapped statuses raise instead of guessing."""
        key = status.strip().lower()
        if key not in ZEROBOUNCE_STATUS:
            raise ValueError(f"unknown ZeroBounce status {status!r}")
        code = f"{key}/{sub_status}" if sub_status else key
        return cls(
            email=email,
            checker="zerobounce",
            checked_at=checked_at,
            result=ZEROBOUNCE_STATUS[key],
            provider_status=code,
        )


class EmailAssessment(BaseModel):
    email: str
    found_by: str
    status: EmailStatus
    reason: str = Field(description="Short code: why this rung and not the next")
    deciding_check: EmailCheck | None = None
    checks_used: int = Field(0, description="Fresh checks of this address")
    checks_ignored: dict[str, int] = Field(
        default_factory=dict, description="Checks not used, by reason (other_address, stale)"
    )

    @property
    def sendable(self) -> bool:
        return self.status is EmailStatus.VERIFIED


def _deciding(checks: Sequence[EmailCheck]) -> EmailCheck:
    conclusive = [c for c in checks if c.result is not CheckResult.UNKNOWN] or list(checks)
    return max(conclusive, key=lambda c: (c.checked_at, _CAUTION[c.result]))


def assess_email(
    email: str,
    found_by: str,
    checks: Sequence[EmailCheck],
    *,
    now: datetime,
    max_age: timedelta = DEFAULT_MAX_AGE,
    require_independent: bool = True,
) -> EmailAssessment:
    """Place one found address on the ladder from the checks given for it."""
    address = clean(email)
    finder = clean(found_by)
    if address is None or finder is None:
        raise ValueError("a found email needs both the address and the provider that found it")
    if now.tzinfo is None:
        raise ValueError("now needs a timezone")

    target = normalise_email(address)
    ignored: Counter[str] = Counter()
    fresh: list[EmailCheck] = []
    for check in checks:
        if check.checked_at > now:
            raise ValueError(f"check by {check.checker!r} is dated after now: {check.checked_at}")
        if normalise_email(check.email) != target:
            ignored["other_address"] += 1
        elif now - check.checked_at > max_age:
            ignored["stale"] += 1
        else:
            fresh.append(check)

    def result(status: EmailStatus, reason: str, check: EmailCheck | None = None):
        return EmailAssessment(
            email=address,
            found_by=finder,
            status=status,
            reason=reason,
            deciding_check=check,
            checks_used=len(fresh),
            checks_ignored=dict(ignored),
        )

    if not fresh:
        return result(EmailStatus.FOUND, "only_stale_checks" if ignored["stale"] else "no_check")

    check = _deciding(fresh)
    if check.result in (CheckResult.INVALID, CheckResult.DO_NOT_MAIL):
        return result(EmailStatus.INVALID, check.result.value, check)
    if check.result is not CheckResult.VALID:
        return result(EmailStatus.CHECKED, check.result.value, check)
    if require_independent and check.checker.lower() == finder.lower():
        return result(EmailStatus.CHECKED, "checked_by_finder_only", check)
    return result(EmailStatus.VERIFIED, "valid", check)


def assess_outcome(
    outcome: WaterfallOutcome,
    checks: Sequence[EmailCheck],
    *,
    now: datetime,
    max_age: timedelta = DEFAULT_MAX_AGE,
    require_independent: bool = True,
) -> EmailAssessment | None:
    """Ladder for a waterfall's email; ``None`` when the waterfall found nothing."""
    if outcome.status is not WaterfallStatus.FOUND or outcome.value is None:
        return None
    return assess_email(
        outcome.value,
        outcome.answered_by or "",
        checks,
        now=now,
        max_age=max_age,
        require_independent=require_independent,
    )


class LadderSummary(BaseModel):
    by_status: dict[EmailStatus, Ratio]
    by_reason: dict[str, int]

    @property
    def sendable(self) -> Ratio:
        return self.by_status[EmailStatus.VERIFIED]


def ladder_summary(assessments: Sequence[EmailAssessment]) -> LadderSummary:
    """Counts per rung over the emails found: unchecked addresses are never counted as valid."""
    seen: set[str] = set()
    for a in assessments:
        key = normalise_email(a.email)
        if key in seen:
            raise ValueError(f"{a.email!r} assessed twice: the denominator would double-count")
        seen.add(key)
    counts = Counter(a.status for a in assessments)
    total = len(assessments)
    return LadderSummary(
        by_status={
            s: Ratio(numerator=counts[s], denominator=total, denominator_name=EMAILS_FOUND)
            for s in EmailStatus
        },
        by_reason=dict(Counter(a.reason for a in assessments)),
    )
