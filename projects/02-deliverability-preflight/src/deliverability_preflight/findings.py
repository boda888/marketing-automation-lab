"""Explainable findings shared by every check."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

Severity = Literal["error", "warning", "info"]


class Finding(BaseModel):
    """One observation about a sending setup or message.

    ``code`` is stable and machine-readable (for n8n routing / dashboards);
    ``message`` explains it to a human; ``ref`` names the rule it comes from.
    """

    code: str
    severity: Severity
    message: str
    ref: str | None = None


def worst(findings: list[Finding]) -> Severity | None:
    """Highest severity present, or ``None`` when there are no findings."""
    for level in ("error", "warning", "info"):
        if any(f.severity == level for f in findings):
            return level  # type: ignore[return-value]
    return None
