"""Offline end-to-end demo: every part of the kit on one simulated clock, no n8n, no vendor.

    python -m reliability.demo          # readable story
    python -m reliability.demo --json   # the same report as JSON

The service is the real FastAPI app (``create_app``) behind ``TestClient``; the n8n
workflows are stood in for by small Python loops that make the same HTTP calls in the same
order as ``n8n/retry-loop.json`` (claim → acquire → push → decide → commit / DLQ + key),
``n8n/dlq-replay.json`` (replay safe rows through the same loop, then ping the watchdog),
``n8n/watchdog.json`` (``POST /v1/watchdog/check``) and ``n8n/error-alert.json`` (``POST
/v1/alerts/n8n-error``). The offline replay of the workflow JSON itself lives in
``tests/test_n8n.py``; this module shows the parts working *together*.

The vendor is scripted by ``examples/demo-loop.json`` (synthetic ``.example`` leads). Its
one extra rule, for the read-back step: an HTTP answer ≥ 500 means the write happened but
the response failed (the lead is in the campaign), no HTTP answer means it never arrived.
Once a lead's scripted answers are used up, the vendor accepts it (201).

Steps:

1. **first run** — each lead goes through the loop once; results must match ``expect``;
2. **re-run of the same list** at once (a double trigger): nothing is pushed — sent leads are
   ``committed``, maybe-landed ones are ``in_flight``, released ones meet the workspace
   blocked by Fay's ``Retry-After`` and are deferred by the budget, never sent;
3. **scheduled replay** after the block: only rows the DLQ calls safe go back through the
   loop; ``unknown`` and ``failed`` rows are refused; the run pings the watchdog;
4. **watchdog** after ``stale_after``: the replay schedule is up, the two maybe-landed
   reservations are reported as stale keys (one alert per scope);
5. **read-back**: each ``unknown`` row is resolved from the vendor's own state, which also
   commits or releases its key;
6. **error alerts**: the same n8n failure twice → one alert, the repeat folded, e-mail masked;
7. **silence**: no replay run for two hours → the watchdog reports the schedule ``down``.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from .api import create_app

ROOT = Path(__file__).resolve().parents[2]
DEMO_FILE = ROOT / "examples" / "demo-loop.json"
TOKEN = "demo-reliability-token-0123456789"
AUTH = {"Authorization": f"Bearer {TOKEN}"}

# the values of retry-loop.json's Config node
KEY_SCOPE = "sequencer:campaign-demo"
POLICY = {"max_attempts": 4, "max_wait_seconds": 300, "max_backoff_seconds": 60}
RATE_MAX_WAIT_MS = 60_000
REPLAY_WATCH = {"name": "dlq-replay", "period_seconds": 3600, "grace_seconds": 600}

# one synthetic Error Trigger item (shape of n8n's execute-error-workflow.ts)
ERROR_ITEM = {
    "execution": {
        "id": "901",
        "url": "https://n8n.ops.example/workflow/12/executions/901",
        "mode": "trigger",
        "lastNodeExecuted": "Push lead (one item)",
        "error": {"message": "Lead ana@acme.example rejected: campaign is paused"},
    },
    "workflow": {"id": "12", "name": "push-demo"},
}


class Clock:
    def __init__(self, start: float) -> None:
        self.t = float(start)

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@dataclass
class ScriptedVendor:
    """Answers from demo-loop.json per e-mail, then 201; remembers what was written."""

    answers: dict[str, list[dict[str, Any]]]
    stored: set[str] = field(default_factory=set)
    pushes: dict[str, int] = field(default_factory=dict)

    def push(self, email: str) -> dict[str, Any]:
        self.pushes[email] = self.pushes.get(email, 0) + 1
        queue = self.answers.get(email) or []
        answer = queue.pop(0) if queue else {"status": 201}
        if "status" in answer and (200 <= answer["status"] < 300 or answer["status"] >= 500):
            self.stored.add(email)
        return answer

    def has(self, email: str) -> bool:
        return email in self.stored


@dataclass
class LeadRun:
    email: str
    end: str  # done | dlq | skipped
    outcome: str | None = None
    claim: str | None = None
    pushes: int = 0
    waits_s: list[float] = field(default_factory=list)
    safe_to_resend: bool | None = None  # of the DLQ row after this stop
    dlq_outcome: str | None = None  # the row's outcome: an earlier failure can outrank this stop
    key_after: str | None = None  # committed | released | reserved

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


class Demo:
    def __init__(self, demo: dict[str, Any]) -> None:
        self.demo = demo
        self.clock = Clock(demo["now"])
        self.client = TestClient(create_app(api_token=TOKEN, now=self.clock, rand=lambda: 0.5))
        self.vendor = ScriptedVendor(
            {lead["lead"]["email"]: list(lead["answers"]) for lead in demo["leads"]}
        )
        self.rate_scope = demo["rate"]["scope"]

    def call(self, method: str, path: str, body: Any = None, **params: Any) -> dict[str, Any]:
        r = self.client.request(method, path, json=body, params=params or None, headers=AUTH)
        if r.status_code != 200:
            raise RuntimeError(f"{method} {path} -> {r.status_code}: {r.text}")
        return r.json()

    def setup(self) -> None:
        self.call(
            "POST",
            "/v1/rate/scopes",
            {"scope": self.rate_scope, "windows": self.demo["rate"]["windows"]},
        )
        name = REPLAY_WATCH["name"]
        watch = {k: v for k, v in REPLAY_WATCH.items() if k != "name"}
        self.call("PUT", f"/v1/watchdog/checks/{name}", watch)

    # --- retry-loop.json for one lead ---------------------------------------------------

    def push_lead(self, lead: dict[str, Any], source: str) -> LeadRun:
        email = lead["email"]
        claim = self.call("POST", "/v1/keys/claim", {"scope": KEY_SCOPE, "key": email})
        if claim["status"] != "reserved":
            return LeadRun(email, "skipped", claim=claim["status"])
        run = LeadRun(email, "done", claim="reserved")
        attempt = 1
        while True:
            grant = self.call(
                "POST",
                "/v1/rate/acquire",
                {
                    "scope": self.rate_scope,
                    "cost": 1,
                    "max_wait_ms": RATE_MAX_WAIT_MS,
                    "key": email,
                    "attempt": attempt,
                },
            )
            if grant["action"] == "wait":
                self.clock.advance(grant["wait_ms"] / 1000)
                continue
            if grant["action"] == "stop":
                return self._stop(run, grant, lead, claim["token"], source)
            answer = self.vendor.push(email)
            run.pushes += 1
            body: dict[str, Any] = {
                "attempt": attempt,
                "method": "POST",
                "idempotent": False,
                "key": email,
                "policy": POLICY,
            }
            if "status" in answer:
                body |= {
                    "status": answer["status"],
                    "headers": answer.get("headers", {}),
                    "rate_scope": self.rate_scope,
                }
            else:
                body["error"] = answer["error"]
            decision = self.call("POST", "/v1/decide", body)
            if decision["action"] == "done":
                self.call(
                    "POST",
                    "/v1/keys/commit",
                    {"scope": KEY_SCOPE, "key": email, "token": claim["token"]},
                )
                run.key_after = "committed"
                return run
            if decision["action"] == "retry":
                run.waits_s.append(decision["wait_ms"] / 1000)
                self.clock.advance(decision["wait_ms"] / 1000)
                attempt = decision["next_attempt"]
                continue
            return self._stop(run, decision, lead, claim["token"], source)

    def _stop(
        self, run: LeadRun, decision: dict[str, Any], lead: dict[str, Any], token: str, src: str
    ) -> LeadRun:
        row = self.call(
            "POST",
            "/v1/dlq",
            {
                "scope": KEY_SCOPE,
                "row": decision["dlq_row"],
                "payload": lead,
                "source": {"workflow": src, "node": "Push lead (one item)"},
            },
        )
        run.end, run.outcome = "dlq", decision["outcome"]
        run.dlq_outcome, run.safe_to_resend = row["outcome"], row["safe_to_resend"]
        if decision["outcome"] == "unknown":
            run.key_after = "reserved"  # may have landed: read back before anything else
        else:
            body = {"scope": KEY_SCOPE, "key": run.email, "token": token}
            self.call("POST", "/v1/keys/release", body)
            run.key_after = "released"
        return run

    # --- the story ----------------------------------------------------------------------

    def run(self) -> dict[str, Any]:
        self.setup()
        report: dict[str, Any] = {"started_at": self.clock()}

        first = [self.push_lead(lead["lead"], "push-demo") for lead in self.demo["leads"]]
        report["first_run"] = [r.as_dict() for r in first]
        report["first_run_matches_expect"] = all(
            _matches(r, lead["expect"]) for r, lead in zip(first, self.demo["leads"], strict=True)
        )

        before = sum(self.vendor.pushes.values())
        rerun = [self.push_lead(lead["lead"], "push-demo") for lead in self.demo["leads"]]
        report["rerun"] = {
            "pushes": sum(self.vendor.pushes.values()) - before,
            "leads": [r.as_dict() for r in rerun],
        }

        blocked = self.call("GET", "/v1/rate/usage", scope=self.rate_scope)
        until = blocked.get("blocked_until") or self.clock()
        self.clock.advance(max(until - self.clock(), 0) + 1)
        replay = self.call("POST", "/v1/dlq/replay", {"scope": KEY_SCOPE})
        replayed = [self.push_lead(item["payload"], "dlq-replay") for item in replay["replay"]]
        self.call("POST", f"/v1/watchdog/checks/{REPLAY_WATCH['name']}/ping", {"signal": "ok"})
        report["replay"] = {
            "sent_again": [r.as_dict() for r in replayed],
            "refused": [
                {"key": i["key"], "status": i["status"], "reason": i["reason"]}
                for i in replay["refused"]
            ],
        }

        self.clock.advance(901)  # past KeyStore.stale_after (900 s)
        check = self.call("POST", "/v1/watchdog/check")
        report["watchdog_after_replay"] = {
            "counts": check["counts"],
            "stale_keys": check["stale_keys"],
            "alerts": [a["text"] for a in check["alerts"]],
        }

        open_unknown = self.call("GET", "/v1/dlq", state="open", outcome="unknown")["rows"]
        resolved = []
        for row in open_unknown:
            landed = self.vendor.has(row["key"])
            out = self.call("POST", f"/v1/dlq/{row['id']}/resolve", {"landed": landed})
            resolved.append({"key": row["key"], "landed": landed, "key_state": out["key_state"]})
        report["read_back"] = resolved

        alerts = [self.call("POST", "/v1/alerts/n8n-error", ERROR_ITEM) for _ in range(2)]
        report["error_alerts"] = [
            {"send": a["send"], "total": a["total"], "text": a["text"]} for a in alerts
        ]

        self.clock.advance(2 * 3600)
        silent = self.call("POST", "/v1/watchdog/check")
        report["watchdog_after_silence"] = {
            "counts": silent["counts"],
            "alerts": [a["text"] for a in silent["alerts"]],
        }
        report["dlq_summary"] = self.call("GET", "/v1/dlq/summary", scope=KEY_SCOPE)
        report["vendor"] = {
            "pushes_per_lead": dict(sorted(self.vendor.pushes.items())),
            "in_campaign": sorted(self.vendor.stored),
        }
        report["finished_at"] = self.clock()
        return report


def _matches(run: LeadRun, expect: dict[str, Any]) -> bool:
    if run.end != expect["end"] or run.pushes != expect["pushes"]:
        return False
    if run.waits_s != [float(w) for w in expect["waits_s"]]:
        return False
    if "outcome" in expect and run.outcome != expect["outcome"]:
        return False
    return "safe_to_resend" not in expect or run.safe_to_resend == expect["safe_to_resend"]


def run_demo(demo: dict[str, Any] | None = None) -> dict[str, Any]:
    data = demo if demo is not None else json.loads(DEMO_FILE.read_text())
    return Demo(json.loads(json.dumps(data))).run()  # deep copy: answers are consumed


def render(report: dict[str, Any]) -> str:
    lines = ["1. first run (one lead per pass, claim -> budget -> push -> decide)"]
    for r in report["first_run"]:
        tail = f"{r['outcome']}, safe_to_resend={r['safe_to_resend']}" if r["outcome"] else ""
        waits = f" waits {r['waits_s']}" if r["waits_s"] else ""
        lines.append(
            f"   {r['email']:<22} {r['end']:<5} pushes={r['pushes']}{waits} "
            f"key={r['key_after']} {tail}".rstrip()
        )
    lines.append(f"   matches examples/demo-loop.json: {report['first_run_matches_expect']}")
    rerun = report["rerun"]
    lines.append(f"2. re-run of the same list: {rerun['pushes']} pushes")
    for r in rerun["leads"]:
        why = f"claim {r['claim']}"
        if r["end"] != "skipped":
            why = f"{r['outcome']} by the budget, never sent; DLQ row: {r['dlq_outcome']}"
        lines.append(f"   {r['email']:<22} {r['end']:<7} ({why})")
    lines.append("3. scheduled replay after the workspace block")
    for r in report["replay"]["sent_again"]:
        lines.append(f"   sent again  {r['email']:<22} {r['end']} pushes={r['pushes']}")
    for r in report["replay"]["refused"]:
        lines.append(f"   refused     {r['key']:<22} {r['status']}")
    w = report["watchdog_after_replay"]
    lines.append(f"4. watchdog after stale_after: {w['counts']}")
    lines.extend(f"   alert: {a}" for a in w["alerts"])
    lines.append("5. read-back of maybe-landed rows")
    for r in report["read_back"]:
        lines.append(f"   {r['key']:<22} landed={r['landed']} -> key {r['key_state']}")
    lines.append("6. error workflow: same failure twice")
    for a in report["error_alerts"]:
        first = a["text"].splitlines()[0] if a["text"] else ""
        lines.append(f"   send={a['send']} total={a['total']} {first}")
    s = report["watchdog_after_silence"]
    lines.append(f"7. two hours without a replay run: {s['counts']}")
    lines.extend(f"   alert: {a}" for a in s["alerts"])
    lines.append(f"vendor pushes per lead: {report['vendor']['pushes_per_lead']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    args = parser.parse_args(argv)
    report = run_demo()
    print(json.dumps(report, indent=2) if args.json else render(report))
    return 0 if report["first_run_matches_expect"] and report["rerun"]["pushes"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
