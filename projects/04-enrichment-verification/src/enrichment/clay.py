"""Clay as an asynchronous waterfall ``Provider`` for ``email``: push a row, take the callback.

Clay is a table, not a request/response lookup. The usual code-side round trip:

    lab ──POST row──► Clay table webhook (inbound source)
                         │ Clay runs its own enrichments / waterfall on the row
                         ▼
    lab ◄──POST row── HTTP API column on the same table (body built from columns)

Shapes the adapter relies on (Clay's docs hosts were unreachable from the run environment,
see the README): a webhook source takes one JSON object per row; a webhook accepts at most
50,000 submissions in its lifetime and deleting rows does not give them back (then a new
webhook is needed); an inbound token may be sent as ``x-clay-webhook-auth``. The HTTP API
column POSTs whatever JSON body you configure, so the callback shape here is the lab's own
contract: ``lab_ref`` plus the result columns you map into it.

Rules the adapter adds:

- Every pushed row carries ``lab_ref``, a hash of exactly the inputs sent. A callback is
  accepted only for a ``lab_ref`` the lab pushed and is still waiting on; an unknown ref is
  refused, so a stray or replayed POST can never write an email onto a record.
- A lookup that pushes, or finds its row still waiting, raises ``ClayPending`` (a
  ``ProviderTimeout``): the waterfall records ``outcome_unknown`` and caches nothing, so the
  answer is picked up by the next run after the callback. The same inputs are pushed once;
  waiting never re-submits (a submission is spent quota and Clay credits).
- The lifetime cap is enforced before the POST: ``ClayQuotaError`` stops the run rather than
  failing rows silently at the cap. A push that timed out counts as submitted and stays
  pending (the row may have landed), so it is never pushed twice.
- A row with no callback after ``callback_timeout`` is ``ClayPending`` with state
  ``no_callback`` (Clay ran out of credits, a column errored, the HTTP column was never run):
  still unknown, never a free miss; inspect it in the table before re-pushing with ``forget()``.
- The callback is checked like an Apollo answer: a company domain different from the one
  sent is an old-employer match (``clay:employer_mismatch:<domain>``, no value); an email
  that is not an address (Clay error text, ``"null"``, an object) gives no value.
- First callback wins. A later one for the same ref (a forced re-run of the column) is
  reported ``duplicate`` and changes nothing; ``forget(ref)`` before re-pushing on purpose.
- Credits are booked once, on the push (``credits_per_row``, what the Clay table spends per
  row); the waterfall's budget and fixture gate (``paid=True``) apply before anything leaves.
- The live transport refuses a row on a reserved test domain before any request.

``verify_clay_signature`` checks Clay's signed webhooks (routine-run notifications):
``X-Clay-Signature: sha256=<hex HMAC-SHA256 of the raw body>``. HTTP API column callbacks are
not signed by Clay; put a shared secret header in the column and check it with
``check_callback_secret``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol

from enrichment.dedupe import domain_key, email_key
from enrichment.models import (
    ProviderError,
    ProviderResult,
    ProviderTimeout,
    _host,
    clean,
    is_reserved_host,
)

SUBMISSION_CAP = 50_000
REF_FIELD = "lab_ref"
AUTH_HEADER = "x-clay-webhook-auth"
SIGNATURE_HEADER = "x-clay-signature"
INPUT_SETS: tuple[tuple[str, ...], ...] = (
    ("linkedin_url", "company_domain"),
    ("first_name", "last_name", "company_domain"),
    ("linkedin_url",),
)
ALLOWED_INPUTS = frozenset(name for names in INPUT_SETS for name in names)


class ClayHTTPError(Exception):
    """Non-2xx answer from a Clay webhook, raised by a transport."""

    def __init__(self, status: int, message: str = "") -> None:
        super().__init__(f"HTTP {status}: {message}".rstrip(": "))
        self.status = status


class ClayAuthError(Exception):
    """Webhook token wrong or webhook gone (401 / 403 / 404). Not a ``ProviderError``: stop."""


class ClayQuotaError(Exception):
    """The webhook's lifetime submission cap would be passed. Create a new webhook."""


class ClayPending(ProviderTimeout):
    """No answer yet (pushed now, still waiting, or waited past ``callback_timeout``).

    A ``ProviderTimeout``, so the waterfall records ``outcome_unknown``, caches nothing and the
    pipeline sends the person to review instead of calling them ``no_email``.
    """

    def __init__(self, message: str, *, state: str, cost: float = 0.0) -> None:
        super().__init__(message, cost=cost)
        self.state = state


class CallbackStatus(StrEnum):
    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    UNKNOWN_REF = "unknown_ref"
    MALFORMED = "malformed"


class Transport(Protocol):
    def post(self, row: Mapping[str, Any]) -> None:
        """POST one row to the table webhook. Raise ``ClayHTTPError`` / ``TimeoutError``."""
        ...


def clay_ref(inputs: Mapping[str, str]) -> str:
    """Stable correlation id for exactly these inputs (values already normalised)."""
    canonical = json.dumps(sorted(inputs.items()), separators=(",", ":"))
    return "lab-" + hashlib.sha256(canonical.encode()).hexdigest()[:20]


def build_row(inputs: Mapping[str, str]) -> dict[str, str]:
    """Row for the table webhook: normalised inputs plus ``lab_ref``. Placeholders raise."""
    unknown = set(inputs) - ALLOWED_INPUTS
    if unknown:
        raise ValueError(f"no Clay column for {sorted(unknown)}")
    row: dict[str, str] = {}
    for name, value in inputs.items():
        value = clean(value)
        if value is None:
            raise ValueError(f"{name!r} is empty or a placeholder")
        if name == "company_domain":
            value = domain_key(value) or value
        row[name] = value
    row[REF_FIELD] = clay_ref(row)
    return row


def check_callback_secret(headers: Mapping[str, str], secret: str, header: str) -> bool:
    """Constant-time check of the shared secret your HTTP API column sends."""
    if not secret:
        raise ValueError("an empty callback secret would accept anyone")
    lowered = {k.lower(): v for k, v in headers.items()}
    return hmac.compare_digest(lowered.get(header.lower(), "").encode(), secret.encode())


def verify_clay_signature(raw_body: bytes, signature: str | None, secret: str) -> bool:
    """``X-Clay-Signature`` = ``sha256=`` + hex HMAC-SHA256 of the raw body (not re-serialised)."""
    if not secret:
        raise ValueError("an empty signing secret would accept anyone")
    if not signature:
        return False
    expected = "sha256=" + hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature.strip().encode(), expected.encode())


@dataclass
class UrllibTransport:
    """Live transport (stdlib only). Not exercised against Clay by the lab's tests."""

    webhook_url: str
    auth_token: str | None = None
    timeout: float = 20.0

    def post(self, row: Mapping[str, Any]) -> None:
        for key in ("linkedin_url", "company_domain"):
            host = _host(row[key]) if isinstance(row.get(key), str) else None
            if host and is_reserved_host(host):
                raise ValueError(f"{key!r} is on a reserved test domain: never sent to Clay")
        headers = {"Content-Type": "application/json"}
        if self.auth_token:
            headers[AUTH_HEADER] = self.auth_token
        request = urllib.request.Request(
            self.webhook_url, data=json.dumps(row).encode(), headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout):
                return
        except urllib.error.HTTPError as exc:
            raise ClayHTTPError(exc.code, exc.reason) from exc
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, TimeoutError):
                raise TimeoutError(str(exc.reason)) from exc
            raise ClayHTTPError(0, str(exc.reason)) from exc


@dataclass
class FakeClayTransport:
    """Offline transport: records rows; can fail by ``lab_ref`` or by any input value."""

    http_errors: Mapping[str, int] = field(default_factory=dict)
    timeouts: frozenset[str] = frozenset()
    rows: list[dict[str, Any]] = field(default_factory=list)

    def post(self, row: Mapping[str, Any]) -> None:
        keys = set(row.values())
        if keys & self.timeouts:
            self.rows.append(dict(row))  # landed, but the answer was lost
            raise TimeoutError("simulated timeout")
        for key in keys:
            if key in self.http_errors:
                raise ClayHTTPError(self.http_errors[key], "simulated")
        self.rows.append(dict(row))


@dataclass
class _Pending:
    row: dict[str, str]
    pushed_at: datetime


@dataclass
class ClayEmail:
    """Waterfall provider backed by a Clay table. Run the waterfall again after callbacks.

    ``email_column`` / ``domain_column`` / ``status_column`` name the keys your HTTP API column
    puts in the callback body. ``submitted`` starts at the webhook's existing submission count
    (Clay shows it); the cap counts every push, including ones that timed out.
    """

    transport: Transport
    clock: Any  # callable returning an aware datetime; injected so tests control time
    name: str = "clay"
    output_field: str = "email"
    input_sets: tuple[tuple[str, ...], ...] = INPUT_SETS
    paid: bool = True
    credits_per_row: float = 1.0
    callback_timeout: timedelta = timedelta(hours=2)
    submission_cap: int = SUBMISSION_CAP
    submitted: int = 0
    email_column: str = "work_email"
    domain_column: str = "company_domain"
    status_column: str | None = None
    pending: dict[str, _Pending] = field(default_factory=dict)
    results: dict[str, ProviderResult] = field(default_factory=dict)

    @property
    def max_cost(self) -> float:
        return self.credits_per_row

    def lookup(self, inputs: Mapping[str, str]) -> ProviderResult:
        row = build_row(inputs)
        ref = row[REF_FIELD]
        if ref in self.results:
            return self.results[ref]  # credits were booked on the push
        if ref in self.pending:
            waited = self.clock() - self.pending[ref].pushed_at
            state = "no_callback" if waited >= self.callback_timeout else "pending"
            raise ClayPending(f"{self.name}: {state} for {ref}", state=state)
        self._push(row)
        raise ClayPending(
            f"{self.name}: pushed {ref}; answer arrives by callback",
            state="submitted",
            cost=self.credits_per_row,
        )

    def _push(self, row: dict[str, str]) -> None:
        if self.submitted + 1 > self.submission_cap:
            raise ClayQuotaError(
                f"{self.name}: webhook has used {self.submitted} of {self.submission_cap} "
                "lifetime submissions; create a new webhook"
            )
        try:
            self.transport.post(row)
        except TimeoutError:
            # The row may be in the table: count it and wait for its callback, never re-push.
            self.submitted += 1
            self.pending[row[REF_FIELD]] = _Pending(row, self.clock())
            raise ClayPending(
                f"{self.name}: push of {row[REF_FIELD]} timed out; it may have landed",
                state="push_timeout",
                cost=self.credits_per_row,
            ) from None
        except ClayHTTPError as exc:
            if exc.status in (401, 403, 404):
                raise ClayAuthError(f"{self.name}: {exc}") from exc
            raise ProviderError(f"{self.name}: {exc}") from exc
        self.submitted += 1
        self.pending[row[REF_FIELD]] = _Pending(row, self.clock())

    def receive(self, body: Any) -> CallbackStatus:
        """Take one callback from the HTTP API column. Check its secret before calling this."""
        if not isinstance(body, Mapping) or not isinstance(body.get(REF_FIELD), str):
            return CallbackStatus.MALFORMED
        ref = body[REF_FIELD]
        if ref in self.results:
            return CallbackStatus.DUPLICATE
        if ref not in self.pending:
            return CallbackStatus.UNKNOWN_REF
        sent = self.pending.pop(ref).row
        self.results[ref] = self._parse(body, sent_domain=sent.get("company_domain"))
        return CallbackStatus.ACCEPTED

    def forget(self, ref: str) -> None:
        """Drop a ref's pending entry or result so the next lookup pushes the row again."""
        self.pending.pop(ref, None)
        self.results.pop(ref, None)

    def _parse(self, body: Mapping[str, Any], *, sent_domain: str | None) -> ProviderResult:
        raw_status = body.get(self.status_column) if self.status_column else None
        status = clean(raw_status) if isinstance(raw_status, str) else None
        label = f"clay:{status.lower()}" if status else "clay:answered"
        raw_domain = body.get(self.domain_column)
        got_domain = domain_key(raw_domain) if isinstance(raw_domain, str) else None
        if sent_domain and got_domain and got_domain != sent_domain:
            return ProviderResult(provider_status=f"clay:employer_mismatch:{got_domain}")
        raw_email = body.get(self.email_column)
        email = email_key(raw_email) if isinstance(raw_email, str) else None
        if email is None or " " in email or not _host(email):
            return ProviderResult(provider_status="clay:no_email" if status is None else label)
        return ProviderResult(value=email, provider_status=label)
