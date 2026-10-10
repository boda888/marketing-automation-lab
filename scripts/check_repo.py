"""Repo-level guardrails run in CI.

- every project has README.md, pyproject.toml and a non-empty tests/ dir
- .agent/state.json parses and has the fields the daily agent relies on
- every n8n workflow JSON parses, has unique node names and valid connections
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE_KEYS = {"current_project", "projects", "next_steps", "last_run", "run_log"}


def check_projects(errors: list[str]) -> None:
    projects = sorted(p for p in (ROOT / "projects").iterdir() if p.is_dir())
    if not projects:
        errors.append("no projects/ found")
    for p in projects:
        for required in ("README.md", "pyproject.toml"):
            if not (p / required).is_file():
                errors.append(f"{p.name}: missing {required}")
        tests = p / "tests"
        if not tests.is_dir() or not any(tests.glob("test_*.py")):
            errors.append(f"{p.name}: no tests/test_*.py")


def check_state(errors: list[str]) -> None:
    path = ROOT / ".agent" / "state.json"
    try:
        state = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f"state.json unreadable: {exc}")
        return
    missing = STATE_KEYS - state.keys()
    if missing:
        errors.append(f"state.json missing keys: {sorted(missing)}")
    known = {p["id"] for p in state.get("projects", [])}
    for p in state.get("projects", []):
        if p.get("status") in {"done", "in_progress"} and not (ROOT / p["path"]).is_dir():
            errors.append(f"state.json: project {p['id']} points to missing {p['path']}")
    cur = state.get("current_project")
    if cur and cur not in known:
        errors.append(f"state.json: current_project {cur!r} not in projects")


def check_n8n(errors: list[str]) -> None:
    for wf_path in ROOT.glob("projects/*/n8n/*.json"):
        rel = wf_path.relative_to(ROOT)
        try:
            wf = json.loads(wf_path.read_text())
        except json.JSONDecodeError as exc:
            errors.append(f"{rel}: invalid JSON ({exc})")
            continue
        names = [n.get("name") for n in wf.get("nodes", [])]
        if not names:
            errors.append(f"{rel}: no nodes")
        if len(names) != len(set(names)):
            errors.append(f"{rel}: duplicate node names")
        for src, outputs in wf.get("connections", {}).items():
            if src not in names:
                errors.append(f"{rel}: connection from unknown node {src!r}")
            for branch in outputs.get("main", []):
                for link in branch or []:
                    if link.get("node") not in names:
                        errors.append(f"{rel}: connection to unknown node {link.get('node')!r}")


def main() -> int:
    errors: list[str] = []
    check_projects(errors)
    check_state(errors)
    check_n8n(errors)
    for e in errors:
        print(f"ERROR: {e}")
    print("repo checks:", "FAILED" if errors else "OK")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
