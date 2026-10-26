"""Apollo People Match (``POST /api/v1/people/match``) as a waterfall ``Provider`` for ``email``.

Request shape, taken from open-source integrations of the endpoint (Apollo's own docs were not
reachable from the run environment, see the README):

- base ``https://api.apollo.io/api/v1``, header ``x-api-key``, JSON body;
- identifiers ``linkedin_url`` or ``first_name`` + ``last_name`` + ``domain``;
- ``reveal_personal_emails`` and ``reveal_phone_number`` default off. A phone reveal needs a
  ``webhook_url`` and answers asynchronously, so this adapter never asks for one.

Rules the adapter adds on top of the endpoint:

- Apollo matches ``domain`` against the current *or a previous* employer. A match whose current
  organisation domain differs from the domain we sent is an old-employer match: no value,
  ``provider_status`` ``apollo:employer_mismatch``, so a past job's email never feeds the next step.
- Apollo's ``email_status`` is kept as context (``apollo:<status>``); the ladder never treats a
  finder's own label as verification.
- 401 / 403 raise ``ApolloAuthError``, which the waterfall does not swallow: a bad key must stop
  the run, not silently skip Apollo for every record. 429 / 5xx are ``ProviderError``; a
  transport timeout is ``ProviderTimeout`` (the match may have been billed).
- The live transport refuses a body on a reserved test domain (``.example``, ...) before any
  request, even if the waterfall's fixture gate was bypassed; the offline fake accepts them.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass, field
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

APOLLO_BASE_URL = "https://api.apollo.io/api/v1"
MATCH_PATH = "/people/match"

# Lab field name → Apollo body field.
FIELD_MAP = {
    "linkedin_url": "linkedin_url",
    "first_name": "first_name",
    "last_name": "last_name",
    "company_domain": "domain",
}
INPUT_SETS: tuple[tuple[str, ...], ...] = (
    ("linkedin_url",),
    ("first_name", "last_name", "company_domain"),
)


class ApolloHTTPError(Exception):
    """Non-2xx answer from Apollo, raised by a transport."""

    def __init__(self, status: int, message: str = "") -> None:
        super().__init__(f"HTTP {status}: {message}".rstrip(": "))
        self.status = status


class ApolloAuthError(Exception):
    """Key missing, wrong or without access. Not a ``ProviderError``: the run must stop."""


class Transport(Protocol):
    def post(self, path: str, body: Mapping[str, Any]) -> Mapping[str, Any]:
        """POST JSON, return the decoded body. Raise ``ApolloHTTPError`` / ``TimeoutError``."""
        ...


@dataclass
class UrllibTransport:
    """Live transport (stdlib only). Not exercised against Apollo by the lab's tests."""

    api_key: str
    base_url: str = APOLLO_BASE_URL
    timeout: float = 20.0

    def post(self, path: str, body: Mapping[str, Any]) -> Mapping[str, Any]:
        for key in ("linkedin_url", "domain", "email"):
            host = _host(body[key]) if isinstance(body.get(key), str) else None
            if host and is_reserved_host(host):
                raise ValueError(f"{key!r} is on a reserved test domain: never sent to Apollo")
        request = urllib.request.Request(
            self.base_url + path,
            data=json.dumps(body).encode(),
            headers={
                "Content-Type": "application/json",
                "Cache-Control": "no-cache",
                "x-api-key": self.api_key,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            raise ApolloHTTPError(exc.code, exc.reason) from exc
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, TimeoutError):
                raise TimeoutError(str(exc.reason)) from exc
            raise ApolloHTTPError(0, str(exc.reason)) from exc


@dataclass
class FakeApolloTransport:
    """Offline transport: canned bodies by LinkedIn URL or domain; records every request."""

    answers: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    http_errors: Mapping[str, int] = field(default_factory=dict)
    timeouts: frozenset[str] = frozenset()
    requests: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def post(self, path: str, body: Mapping[str, Any]) -> Mapping[str, Any]:
        self.requests.append((path, dict(body)))
        keys = [body.get("linkedin_url"), body.get("domain")]
        for key in keys:
            if key in self.timeouts:
                raise TimeoutError("simulated timeout")
            if key in self.http_errors:
                raise ApolloHTTPError(self.http_errors[key], "simulated")
        for key in keys:
            if key in self.answers:
                return self.answers[key]
        return {"person": None}


def build_match_body(inputs: Mapping[str, str]) -> dict[str, Any]:
    """Apollo body from lab inputs. Unknown fields and placeholders raise; reveals stay off."""
    unknown = set(inputs) - FIELD_MAP.keys()
    if unknown:
        raise ValueError(f"no Apollo mapping for {sorted(unknown)}")
    body: dict[str, Any] = {}
    for name, value in inputs.items():
        value = clean(value)
        if value is None:
            raise ValueError(f"{name!r} is empty or a placeholder")
        if name == "company_domain":
            value = domain_key(value) or value
        body[FIELD_MAP[name]] = value
    body["reveal_personal_emails"] = False
    body["reveal_phone_number"] = False
    return body


@dataclass
class ApolloPeopleMatch:
    """Waterfall provider: one person in, a work email out.

    ``credits_per_match`` is booked only when an email comes back; set ``charge_on_miss=True``
    if your plan bills misses too. ``max_cost`` is what the budget reserves before the call.
    """

    transport: Transport
    name: str = "apollo"
    output_field: str = "email"
    input_sets: tuple[tuple[str, ...], ...] = INPUT_SETS
    paid: bool = True
    credits_per_match: float = 1.0
    charge_on_miss: bool = False

    @property
    def max_cost(self) -> float:
        return self.credits_per_match

    def lookup(self, inputs: Mapping[str, str]) -> ProviderResult:
        body = build_match_body(inputs)
        try:
            payload = self.transport.post(MATCH_PATH, body)
        except TimeoutError as exc:
            raise ProviderTimeout(f"{self.name}: {exc}") from exc
        except ApolloHTTPError as exc:
            if exc.status in (401, 403):
                raise ApolloAuthError(f"{self.name}: {exc}") from exc
            raise ProviderError(f"{self.name}: {exc}") from exc
        return self._parse(payload, sent_domain=body.get("domain"))

    def _parse(self, payload: Mapping[str, Any], *, sent_domain: str | None) -> ProviderResult:
        person = payload.get("person") if isinstance(payload, Mapping) else None
        miss_cost = self.credits_per_match if self.charge_on_miss else 0.0
        if not isinstance(person, Mapping):
            return ProviderResult(cost=miss_cost, provider_status="apollo:no_match")
        org = person.get("organization")
        org_domain = domain_key(org.get("primary_domain")) if isinstance(org, Mapping) else None
        if sent_domain and org_domain and org_domain != sent_domain:
            return ProviderResult(
                cost=miss_cost, provider_status=f"apollo:employer_mismatch:{org_domain}"
            )
        email = email_key(person.get("email"))
        status = clean(person.get("email_status"))
        label = f"apollo:{status.lower()}" if status else "apollo:no_status"
        if email is None or email.startswith("email_not_unlocked@"):
            return ProviderResult(cost=miss_cost, provider_status=label)
        return ProviderResult(value=email, cost=self.credits_per_match, provider_status=label)
