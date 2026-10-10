"""Score a CSV of accounts offline.

    python -m icp_scoring accounts.csv -o scored.csv [--config my_icp.json]

Input columns (all optional except domain): domain, name, industry,
employee_count, annual_revenue_usd, country, technologies (separated by ';').
Engagement is not read from CSV; use the API for event-aware scoring.
"""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import UTC, datetime

from .config import load_config
from .models import Company, ScoreRequest
from .scoring import score

OUTPUT_FIELDS = [
    "domain",
    "fit_score",
    "tier",
    "action",
    "disqualified",
    "needs_enrichment",
    "missing_fields",
    "top_reasons",
]


def _num(value: str | None, cast):
    value = (value or "").strip().replace(",", "")
    return cast(float(value)) if value else None


def row_to_company(row: dict[str, str]) -> Company:
    techs = row.get("technologies")
    return Company(
        domain=row["domain"],
        name=row.get("name") or None,
        industry=row.get("industry") or None,
        employee_count=_num(row.get("employee_count"), int),
        annual_revenue_usd=_num(row.get("annual_revenue_usd"), float),
        country=row.get("country") or None,
        technologies=None
        if techs is None or not techs.strip()
        else [t.strip() for t in techs.split(";") if t.strip()],
    )


def _write(out, rows: list[dict[str, str]], cfg, now: datetime) -> None:
    writer = csv.DictWriter(out, fieldnames=OUTPUT_FIELDS)
    writer.writeheader()
    for row in rows:
        result = score(ScoreRequest(company=row_to_company(row), as_of=now), cfg)
        lost = sorted(
            (c for c in result.contributions if c.max_points - c.points > 0.05),
            key=lambda c: c.max_points - c.points,
            reverse=True,
        )
        reasons = result.disqualifiers or [
            f"{c.signal} lost {c.max_points - c.points:.1f}pt" for c in lost[:2]
        ]
        writer.writerow(
            {
                "domain": result.domain,
                "fit_score": result.fit_score,
                "tier": result.tier,
                "action": result.action,
                "disqualified": result.disqualified,
                "needs_enrichment": result.needs_enrichment,
                "missing_fields": ";".join(result.missing_fields),
                "top_reasons": " | ".join(reasons),
            }
        )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="icp_scoring", description=__doc__.splitlines()[0])
    p.add_argument("input", help="CSV file with accounts")
    p.add_argument("-o", "--output", help="output CSV (default: stdout)")
    p.add_argument("--config", help="ICP config JSON (default: packaged example)")
    args = p.parse_args(argv)

    cfg = load_config(args.config)
    now = datetime.now(UTC)
    with open(args.input, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))

    if args.output:
        with open(args.output, "w", newline="", encoding="utf-8") as out:
            _write(out, rows, cfg, now)
    else:
        _write(sys.stdout, rows, cfg, now)
    return 0
