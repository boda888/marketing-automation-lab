"""Alerts from n8n's Error Trigger: one readable line per failure, repeats folded together.

n8n runs a workflow's error workflow only when an execution (or its trigger) failed, and
hands the Error Trigger one item (``packages/cli/src/execution-lifecycle/
execute-error-workflow.ts``):

    {"execution": {"id", "url", "error": {...}, "lastNodeExecuted", "mode", "retryOf"},
     "workflow": {"id", "name"}}                       # failed during the run
    {"trigger": {"error": {...}, "mode"}, "workflow": {"id", "name"}}   # failed in the trigger

The payload carries no items, so an alert can name the workflow, node and execution but
never the lead: per-lead failures live in the DLQ (``GET /v1/dlq``). Three things a raw
"send the payload to Slack" error workflow gets wrong, handled here:

- **Alert storms.** A schedule that fails every 5 minutes would post 288 messages a day.
  Alerts are fingerprinted (workflow + kind + node + the error message with ids, numbers,
  e-mails and quoted values masked); the first one in a window is sent, the rest are
  counted and reported on the next one that is sent ("+N similar since ...").
- **Lead data in a chat channel.** Vendor error messages often echo the payload; e-mail
  addresses are masked in the text and the message is cut to ``MAX_MESSAGE`` characters.
- **Dead links.** A trigger failure has no execution, and an execution URL opens only if
  the workflow saves failed executions; the text says which case it is.

``parse_error_event`` never raises on an odd payload: an error workflow that fails on its
own input alerts nobody, so an unrecognised payload becomes an ``unrecognised`` event that
is always sent.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

EventKind = Literal["execution", "trigger", "unrecognised"]

MAX_MESSAGE = 300
DEFAULT_WINDOW_SECONDS = 900.0

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_UUID = re.compile(r"\b[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\b")
_HEX = re.compile(r"\b[0-9a-fA-F]{16,}\b")
_QUOTED = re.compile(r"\"[^\"]*\"|'[^']*'")
_NUMBER = re.compile(r"\d+")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS alert_log (
    fingerprint TEXT PRIMARY KEY,
    workflow TEXT NOT NULL,
    first_seen_at REAL NOT NULL,
    last_seen_at REAL NOT NULL,
    last_sent_at REAL NOT NULL,
    suppressed INTEGER NOT NULL,
    total INTEGER NOT NULL
)
"""


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


@dataclass(frozen=True)
class ErrorEvent:
    """What the Error Trigger said, flattened; ``message`` is already masked and cut."""

    kind: EventKind
    workflow_id: str | None
    workflow_name: str
    execution_id: str | None = None
    url: str | None = None
    mode: str | None = None
    retry_of: str | None = None
    node: str | None = None
    message: str = ""
    http_code: str | None = None

    @property
    def test(self) -> bool:
        """Manual runs: the Error Trigger's sample data or someone testing the workflow."""
        return self.mode == "manual"

    def as_dict(self) -> dict[str, Any]:
        return {**asdict(self), "test": self.test}


def mask_message(message: str) -> str:
    """E-mail addresses masked, whitespace collapsed, cut to ``MAX_MESSAGE`` characters."""
    text = " ".join(_EMAIL.sub("<email>", message).split())
    return text if len(text) <= MAX_MESSAGE else text[: MAX_MESSAGE - 1] + "…"


def _error_message(error: Any) -> tuple[str, str | None, str | None]:
    if not isinstance(error, Mapping):
        return (mask_message(str(error)) if error else "(no error message)", None, None)
    parts = [_text(error.get("message")), _text(error.get("description"))]
    message = " — ".join(p for p in parts if p) or "(no error message)"
    node = error.get("node")
    node_name = _text(node.get("name")) if isinstance(node, Mapping) else _text(node)
    return mask_message(message), node_name, _text(error.get("httpCode"))


def parse_error_event(payload: Any) -> ErrorEvent:
    """One Error Trigger item -> ``ErrorEvent``; anything else -> kind ``unrecognised``."""
    if not isinstance(payload, Mapping):
        return ErrorEvent(kind="unrecognised", workflow_id=None, workflow_name="(unknown)")
    workflow = payload.get("workflow")
    workflow = workflow if isinstance(workflow, Mapping) else {}
    wf_id = _text(workflow.get("id"))
    wf_name = _text(workflow.get("name")) or (f"workflow {wf_id}" if wf_id else "(unknown)")
    execution, trigger = payload.get("execution"), payload.get("trigger")
    if isinstance(execution, Mapping):
        message, error_node, http_code = _error_message(execution.get("error"))
        return ErrorEvent(
            kind="execution",
            workflow_id=wf_id,
            workflow_name=wf_name,
            execution_id=_text(execution.get("id")),
            url=_text(execution.get("url")),
            mode=_text(execution.get("mode")),
            retry_of=_text(execution.get("retryOf")),
            node=error_node or _text(execution.get("lastNodeExecuted")),
            message=message,
            http_code=http_code,
        )
    if isinstance(trigger, Mapping):
        message, error_node, http_code = _error_message(trigger.get("error"))
        return ErrorEvent(
            kind="trigger",
            workflow_id=wf_id,
            workflow_name=wf_name,
            mode=_text(trigger.get("mode")),
            node=error_node,
            message=message,
            http_code=http_code,
        )
    return ErrorEvent(kind="unrecognised", workflow_id=wf_id, workflow_name=wf_name)


def fingerprint(event: ErrorEvent) -> str:
    """Same workflow, kind, node and message shape -> same fingerprint.

    Ids, numbers and quoted values are masked so "lead 123 failed" and "lead 456 failed"
    fold together; a different node or status code is a different alert. Test events
    (manual mode) never share a fingerprint with real ones.
    """
    shape = event.message
    for pattern, mask in ((_UUID, "<id>"), (_HEX, "<id>"), (_QUOTED, "<v>"), (_NUMBER, "<n>")):
        shape = pattern.sub(mask, shape)
    parts = [
        "test" if event.test else "live",
        event.workflow_id or event.workflow_name,
        event.kind,
        event.node or "",
        event.http_code or "",
        shape.lower(),
    ]
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()[:16]


@dataclass(frozen=True)
class AlertDecision:
    send: bool
    fingerprint: str
    suppressed_since_last: int  # repeats folded into this alert (send) or so far (no send)
    total: int  # every event with this fingerprint ever recorded
    first_seen_at: float
    last_sent_at: float  # this event's time when sent, else the earlier sent alert's
    previous_sent_at: float | None = None  # the alert sent before the repeats were folded

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class AlertLog:
    """Per-fingerprint counters on one SQLite file, shared by every n8n worker."""

    def __init__(
        self,
        path: str | Path = ":memory:",
        *,
        window_seconds: float = DEFAULT_WINDOW_SECONDS,
        busy_timeout: float = 10.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if window_seconds < 0:
            raise ValueError("window_seconds must be >= 0")
        self.path = str(path)
        self.window_seconds = window_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._db = sqlite3.connect(
            self.path, timeout=busy_timeout, isolation_level=None, check_same_thread=False
        )
        if self.path != ":memory:":
            self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute(_SCHEMA)

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> AlertLog:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def record(self, event: ErrorEvent, *, window_seconds: float | None = None) -> AlertDecision:
        """Count the event; ``send`` for the first one per fingerprint and window.

        Unrecognised payloads are always sent: their fingerprint says nothing about
        whether two of them are the same failure.
        """
        return self.record_fingerprint(
            fingerprint(event),
            label=event.workflow_name,
            window_seconds=window_seconds,
            always=event.kind == "unrecognised",
        )

    def record_fingerprint(
        self,
        fp: str,
        *,
        label: str,
        window_seconds: float | None = None,
        always: bool = False,
    ) -> AlertDecision:
        """The same counting for any alert source that names its own fingerprint."""
        window = self.window_seconds if window_seconds is None else window_seconds
        now = self._clock()
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                row = self._db.execute(
                    "SELECT first_seen_at, last_sent_at, suppressed, total FROM alert_log "
                    "WHERE fingerprint = ?",
                    (fp,),
                ).fetchone()
                previous = None
                if row is None:
                    first, sent_at, folded, total, send = now, now, 0, 1, True
                    self._db.execute(
                        "INSERT INTO alert_log VALUES (?, ?, ?, ?, ?, 0, 1)",
                        (fp, label, now, now, now),
                    )
                else:
                    first, last_sent, suppressed, total = row
                    total += 1
                    send = always or now - last_sent >= window
                    folded = suppressed if send else suppressed + 1
                    sent_at = now if send else last_sent
                    previous = last_sent
                    self._db.execute(
                        "UPDATE alert_log SET last_seen_at = ?, last_sent_at = ?, "
                        "suppressed = ?, total = ? WHERE fingerprint = ?",
                        (now, sent_at, 0 if send else folded, total, fp),
                    )
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
        return AlertDecision(
            send=send,
            fingerprint=fp,
            suppressed_since_last=folded,
            total=total,
            first_seen_at=first,
            last_sent_at=sent_at,
            previous_sent_at=previous,
        )


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d %H:%M UTC")


def format_alert(event: ErrorEvent, decision: AlertDecision | None = None) -> str:
    """Plain text for Telegram / Slack / a generic webhook; no markup to escape."""
    tag = "[test] " if event.test else ""
    if event.kind == "unrecognised":
        return (
            f"{tag}n8n error workflow got a payload it does not recognise "
            f"(workflow: {event.workflow_name}). Open the error workflow's executions."
        )
    where = f" at node '{event.node}'" if event.node else ""
    status = f" (HTTP {event.http_code})" if event.http_code else ""
    lines = [f"{tag}n8n: '{event.workflow_name}' failed{where}{status}: {event.message}"]
    if event.kind == "trigger":
        lines.append(
            "Failed in its trigger: no execution was started, so there is nothing to open; "
            "check the trigger's credentials and schedule."
        )
    else:
        run = f"execution {event.execution_id}" if event.execution_id else "execution"
        mode = f", mode {event.mode}" if event.mode else ""
        retry = f", retry of {event.retry_of}" if event.retry_of else ""
        lines.append(f"{run}{mode}{retry}: {event.url or '(no url)'}")
        lines.append(
            "The link opens only if the workflow saves failed executions. "
            "This alert names no lead: per-lead failures are in GET /v1/dlq."
        )
    if decision is not None and decision.suppressed_since_last:
        since = decision.previous_sent_at or decision.first_seen_at
        lines.append(
            f"+{decision.suppressed_since_last} similar not sent since the alert at "
            f"{_iso(since)} ({decision.total} in total since {_iso(decision.first_seen_at)})."
        )
    return "\n".join(lines)
