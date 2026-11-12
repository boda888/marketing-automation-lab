"""Return-date extraction for out-of-office replies (English wording only).

Only a date that follows a return cue ("back on", "returning", "until", "from", ...) counts.
Ambiguous numeric dates (03/04/2026: March or April?) are reported as ambiguous, never
guessed: the caller then pauses for a default window instead.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

MONTHS = {
    name: i
    for i, names in enumerate(
        [
            ("january", "jan"),
            ("february", "feb"),
            ("march", "mar"),
            ("april", "apr"),
            ("may",),
            ("june", "jun"),
            ("july", "jul"),
            ("august", "aug"),
            ("september", "sep", "sept"),
            ("october", "oct"),
            ("november", "nov"),
            ("december", "dec"),
        ],
        start=1,
    )
    for name in names
}
_MONTH = r"(?P<month>" + "|".join(sorted(MONTHS, key=len, reverse=True)) + r")\.?"
_DAY = r"(?P<day>\d{1,2})(?:st|nd|rd|th)?"
_YEAR = r"(?P<year>\d{4})"
_WEEKDAY = r"(?:(?:mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)[a-z]*,?\s+)?"

# Cues in priority order: "back on 24 Nov" beats "away until 23 Nov" in the same message.
# "from" is deliberately not a cue: in "away from 10 to 24 Nov" it names the start.
_CUES = [
    re.compile(r"\bback\b|\breturn(?:ing|s)?\b|\bin the office\b|\bavailable\b"),
    re.compile(r"\buntil\b|\btill\b|\bthrough\b"),
]
_MAX_GAP = 40

_PATTERNS = [
    ("iso", re.compile(r"(?P<year>\d{4})-(?P<m>\d{2})-(?P<d>\d{2})")),
    (
        "day_month",
        re.compile(_WEEKDAY + _DAY + r"(?:\s+of)?\s+" + _MONTH + r"(?:,?\s+" + _YEAR + ")?"),
    ),
    ("month_day", re.compile(_WEEKDAY + _MONTH + r"\s+" + _DAY + r"(?:,?\s+" + _YEAR + ")?")),
    # Dotted numbers need a year ("9.30" is a time); slashed ones may omit it ("24/11").
    (
        "numeric",
        re.compile(r"(?<![\d.])(?P<a>\d{1,2})\.(?P<b>\d{1,2})\.(?P<y>\d{4}|\d{2})(?![\d])"),
    ),
    (
        "numeric",
        re.compile(r"(?<![\d/])(?P<a>\d{1,2})/(?P<b>\d{1,2})(?:/(?P<y>\d{4}|\d{2}))?(?![\d])"),
    ),
]


@dataclass(frozen=True)
class ReturnDate:
    value: date | None
    note: str  # "found" | "ambiguous: ..." | "no date after a return cue" | "invalid date ..."
    matched: str = ""


def _year_for(month: int, day: int, explicit: int | None, received: date) -> int:
    if explicit is not None:
        return explicit
    # No year given: the next occurrence on or after the day the reply arrived.
    year = received.year
    try:
        if date(year, month, day) < received:
            year += 1
    except ValueError:
        pass
    return year


def _from_match(kind: str, m: re.Match[str], received: date) -> ReturnDate:
    text = m.group(0).strip()
    try:
        if kind == "iso":
            return ReturnDate(date(int(m["year"]), int(m["m"]), int(m["d"])), "found", text)
        if kind in {"day_month", "month_day"}:
            month = MONTHS[m["month"].lower()]
            day = int(m["day"])
            explicit = int(m["year"]) if m["year"] else None
            return ReturnDate(
                date(_year_for(month, day, explicit, received), month, day), "found", text
            )
        a, b = int(m["a"]), int(m["b"])
        y = m["y"]
        explicit = None if y is None else (int(y) + 2000 if len(y) == 2 else int(y))
        if a > 12 and b <= 12:
            day, month = a, b
        elif b > 12 and a <= 12:
            day, month = b, a
        elif a == b:
            day = month = a
        else:
            return ReturnDate(None, f"ambiguous: {text} (day/month order unknown)", text)
        return ReturnDate(
            date(_year_for(month, day, explicit, received), month, day), "found", text
        )
    except ValueError:
        return ReturnDate(None, f"invalid date: {text}", text)


def extract_return_date(text: str, received: date) -> ReturnDate:
    """Date that follows a return cue (at most 40 characters after it, same sentence).

    The highest-priority cue group that yields a date wins; within a group, the earliest.
    """
    lowered = text.lower()
    for cues in _CUES:
        best: tuple[int, ReturnDate] | None = None
        for cue in cues.finditer(lowered):
            start = cue.end()
            window = lowered[start : start + _MAX_GAP + 40]
            # Stop at a sentence end, but not at the dot inside "24.11.2026" or "Nov.".
            window = re.split(r"\.(?=\s|$)|\n", window, maxsplit=1)[0]
            found: tuple[int, str, re.Match[str]] | None = None
            for kind, pattern in _PATTERNS:
                m = pattern.search(window)
                if (
                    m is not None
                    and m.start() <= _MAX_GAP
                    and (found is None or m.start() < found[0])
                ):
                    found = (m.start(), kind, m)
            if found is None:
                continue
            pos = start + found[0]
            if best is None or pos < best[0]:
                best = (pos, _from_match(found[1], found[2], received))
        if best is not None:
            return best[1]
    return ReturnDate(None, "no date after a return cue")


__all__ = ["ReturnDate", "extract_return_date"]
