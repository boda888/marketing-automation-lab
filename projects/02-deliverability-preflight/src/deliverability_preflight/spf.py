"""SPF record parsing and static lookup-budget analysis (RFC 7208).

``check_spf`` walks a domain's SPF policy the way a receiver would in the
worst case (no mechanism matches before ``all``) and reports:

- the number of DNS-querying terms (``include``, ``a``, ``mx``, ``ptr``,
  ``exists``, ``redirect``) — receivers must return ``permerror`` above 10;
- the number of *void* lookups (empty answer / NXDOMAIN) — ``permerror`` above 2;
- structural problems: no record, several records, syntax errors, ``include``
  of a domain without SPF, include loops, ``+all`` / ``?all``, ``ptr``.

Unlike a receiver it keeps counting past the limit (up to a hard safety cap)
so the report shows how far over budget a policy is. Terms containing macros
(``%{i}``…) depend on the connecting IP and are counted but not resolved.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel

from .dns import DNSTempError, Resolver, normalize_name
from .findings import Finding

LOOKUP_LIMIT = 10
VOID_LOOKUP_LIMIT = 2
MX_RECORD_LIMIT = 10
_SAFETY_CAP = 50  # stop walking pathological policies

_REF_LIMITS = "RFC 7208 §4.6.4"
_MECHANISMS = {"all", "include", "a", "mx", "ptr", "ip4", "ip6", "exists"}
_LOOKUP_MECHANISMS = {"include", "a", "mx", "ptr", "exists"}
_MODIFIER_RE = re.compile(r"^([a-zA-Z][a-zA-Z0-9_.-]*)=(.*)$")
_MECH_RE = re.compile(r"^([+\-~?]?)([a-zA-Z0-9]+)(.*)$")
_CIDR_RE = re.compile(r"^(?:/(\d{1,2}))?(?://(\d{1,3}))?$")


class SpfSyntaxError(ValueError):
    """The record is not valid SPF; receivers return ``permerror``."""


class Mechanism(BaseModel):
    qualifier: Literal["+", "-", "~", "?"] = "+"
    name: str
    domain: str | None = None
    network: str | None = None
    cidr4: int | None = None
    cidr6: int | None = None

    def __str__(self) -> str:
        q = "" if self.qualifier == "+" else self.qualifier
        target = self.network or self.domain
        return f"{q}{self.name}" + (f":{target}" if target else "")


class SpfRecord(BaseModel):
    raw: str
    mechanisms: list[Mechanism]
    redirect: str | None = None
    exp: str | None = None
    unknown_modifiers: dict[str, str] = {}

    @property
    def all_mechanism(self) -> Mechanism | None:
        return next((m for m in self.mechanisms if m.name == "all"), None)


def is_spf_record(txt: str) -> bool:
    """RFC 7208 §4.5: exactly ``v=spf1`` or ``v=spf1`` followed by a space."""
    head = txt[:7].lower()
    return head == "v=spf1" or head == "v=spf1 "


def _parse_mechanism(token: str) -> Mechanism:
    m = _MECH_RE.match(token)
    if not m:
        raise SpfSyntaxError(f"unparseable term {token!r}")
    qualifier, name, rest = m.group(1) or "+", m.group(2).lower(), m.group(3)
    if name not in _MECHANISMS:
        raise SpfSyntaxError(f"unknown mechanism {name!r}")
    mech = Mechanism(qualifier=qualifier, name=name)

    if name == "all":
        if rest:
            raise SpfSyntaxError("'all' takes no argument")
        return mech

    if name in ("ip4", "ip6"):
        if not rest.startswith(":") or len(rest) < 2:
            raise SpfSyntaxError(f"{name} requires an address")
        try:
            net = ipaddress.ip_network(rest[1:], strict=False)
        except ValueError as exc:
            raise SpfSyntaxError(f"invalid {name} value {rest[1:]!r}") from exc
        if (name == "ip4") != (net.version == 4):
            raise SpfSyntaxError(f"{name} value {rest[1:]!r} is the wrong address family")
        mech.network = rest[1:]
        return mech

    domain, cidr = rest, ""
    if rest.startswith(":"):
        domain, _, cidr = rest[1:].partition("/")
        cidr = f"/{cidr}" if cidr else ""
    elif rest.startswith("/"):
        domain, cidr = "", rest
    elif rest:
        raise SpfSyntaxError(f"unexpected text after {name!r}: {rest!r}")
    if rest.startswith(":") and not domain:
        raise SpfSyntaxError(f"{name}: empty domain")

    if name in ("include", "exists") and not domain:
        raise SpfSyntaxError(f"{name} requires a domain")
    if cidr:
        if name not in ("a", "mx"):
            raise SpfSyntaxError(f"{name} does not take a CIDR length")
        c = _CIDR_RE.match(cidr)
        if not c:
            raise SpfSyntaxError(f"invalid CIDR length {cidr!r}")
        mech.cidr4 = int(c.group(1)) if c.group(1) else None
        mech.cidr6 = int(c.group(2)) if c.group(2) else None
        if (mech.cidr4 or 0) > 32 or (mech.cidr6 or 0) > 128:
            raise SpfSyntaxError(f"CIDR length out of range in {token!r}")
    mech.domain = domain or None
    return mech


def parse_spf(raw: str) -> SpfRecord:
    """Parse one SPF TXT string. Raises ``SpfSyntaxError`` on invalid records."""
    tokens = raw.split()
    if not tokens or tokens[0].lower() != "v=spf1":
        raise SpfSyntaxError("record must start with 'v=spf1'")
    mechanisms: list[Mechanism] = []
    modifiers: dict[str, str] = {}
    for token in tokens[1:]:
        mod = _MODIFIER_RE.match(token)
        if mod:
            key, value = mod.group(1).lower(), mod.group(2)
            if key in modifiers:
                raise SpfSyntaxError(f"modifier {key!r} appears more than once")
            if key in ("redirect", "exp") and not value:
                raise SpfSyntaxError(f"{key}= requires a domain")
            modifiers[key] = value
        else:
            mechanisms.append(_parse_mechanism(token))
    return SpfRecord(
        raw=raw,
        mechanisms=mechanisms,
        redirect=modifiers.pop("redirect", None),
        exp=modifiers.pop("exp", None),
        unknown_modifiers=modifiers,
    )


SpfStatus = Literal["valid", "none", "permerror", "temperror"]


class SpfReport(BaseModel):
    domain: str
    status: SpfStatus
    record: str | None = None
    lookups: int = 0
    void_lookups: int = 0
    visited: list[str] = []
    findings: list[Finding] = []


@dataclass
class _Walk:
    resolver: Resolver
    lookups: int = 0
    void_lookups: int = 0
    permerror: bool = False
    temperror: bool = False
    visited: list[str] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)

    def add(self, code: str, severity: str, message: str, ref: str, perm: bool = False) -> None:
        self.findings.append(Finding(code=code, severity=severity, message=message, ref=ref))
        self.permerror = self.permerror or perm

    def query(self, name: str, rtype: str) -> list[str] | None:
        try:
            return self.resolver.lookup(name, rtype)
        except DNSTempError as exc:
            self.temperror = True
            self.add("SPF_DNS_TEMPERROR", "error", str(exc), "RFC 7208 §2.6.6")
            return None


def _has_macro(domain: str) -> bool:
    return "%" in domain


def _fetch_record(w: _Walk, domain: str, role: str) -> SpfRecord | None:
    txts = w.query(domain, "TXT")
    if txts is None:
        return None
    records = [t for t in txts if is_spf_record(t)]
    if not records:
        if role == "top":
            w.add("SPF_MISSING", "error", f"{domain} publishes no SPF record", "RFC 7208 §4.5")
        else:
            w.add(
                "SPF_TARGET_NO_RECORD",
                "error",
                f"{role} target {domain} publishes no SPF record (receivers return permerror)",
                "RFC 7208 §5.2 / §6.1",
                perm=True,
            )
        return None
    if len(records) > 1:
        w.add(
            "SPF_MULTIPLE_RECORDS",
            "error",
            f"{domain} publishes {len(records)} SPF records; exactly one is allowed",
            "RFC 7208 §4.5",
            perm=True,
        )
        return None
    try:
        return parse_spf(records[0])
    except SpfSyntaxError as exc:
        w.add("SPF_SYNTAX", "error", f"{domain}: {exc}", "RFC 7208 §4.6", perm=True)
        return None


def _walk(w: _Walk, domain: str, role: str, stack: tuple[str, ...]) -> SpfRecord | None:
    record = _fetch_record(w, domain, role)
    if record is None:
        return None
    w.visited.append(domain)
    is_policy = role in ("top", "redirect")  # whose 'all' decides the final result
    seen_all = False

    for mech in record.mechanisms:
        if w.lookups >= _SAFETY_CAP:
            w.add("SPF_WALK_ABORTED", "error", "stopped after safety cap", _REF_LIMITS, True)
            return record
        if seen_all:
            w.add(
                "SPF_TERMS_AFTER_ALL",
                "warning",
                f"{domain}: '{mech}' after 'all' is never evaluated",
                "RFC 7208 §5.1",
            )
            continue
        if mech.name in _LOOKUP_MECHANISMS:
            w.lookups += 1
        target = normalize_name(mech.domain) if mech.domain else domain

        if mech.name == "all":
            seen_all = True
            if is_policy:
                _check_all(w, domain, mech)
        elif mech.name == "include":
            if _has_macro(target):
                continue
            if target in stack:
                w.add("SPF_LOOP", "error", f"include loop via {target}", _REF_LIMITS, True)
                continue
            _walk(w, target, "include", (*stack, target))
        elif mech.name == "ptr":
            w.add(
                "SPF_PTR_USED",
                "warning",
                f"{domain}: 'ptr' is slow, unreliable and SHOULD NOT be used",
                "RFC 7208 §5.5",
            )
        elif mech.name in ("a", "mx", "exists") and not _has_macro(target):
            _count_void(w, mech.name, target)

    if record.redirect:
        if record.all_mechanism is not None:
            w.add(
                "SPF_REDIRECT_IGNORED",
                "info",
                f"{domain}: redirect= is ignored because the record has 'all'",
                "RFC 7208 §6.1",
            )
        else:
            w.lookups += 1
            target = normalize_name(record.redirect)
            if target in stack:
                w.add("SPF_LOOP", "error", f"redirect loop via {target}", _REF_LIMITS, True)
            elif not _has_macro(target):
                _walk(w, target, "redirect", (*stack, target))
    elif is_policy and record.all_mechanism is None:
        w.add(
            "SPF_NO_ALL",
            "warning",
            f"{domain}: no 'all' and no redirect — unmatched senders get 'neutral'",
            "RFC 7208 §4.7",
        )
    return record


def _check_all(w: _Walk, domain: str, mech: Mechanism) -> None:
    if mech.qualifier == "+":
        w.add(
            "SPF_PASS_ALL",
            "error",
            f"{domain}: '+all' authorises every host on the internet",
            "RFC 7208 §5.1",
        )
    elif mech.qualifier == "?":
        w.add(
            "SPF_NEUTRAL_ALL",
            "warning",
            f"{domain}: '?all' gives unlisted senders 'neutral'; prefer '~all' or '-all'",
            "RFC 7208 §8.2",
        )


def _count_void(w: _Walk, name: str, target: str) -> None:
    if name == "mx":
        answers = w.query(target, "MX")
        if answers is not None and len(answers) > MX_RECORD_LIMIT:
            w.add(
                "SPF_MX_TOO_MANY",
                "error",
                f"mx:{target} returns {len(answers)} MX records (limit {MX_RECORD_LIMIT})",
                _REF_LIMITS,
                perm=True,
            )
    else:  # a, exists
        answers = w.query(target, "A")
        if answers == [] and name == "a":
            answers = w.query(target, "AAAA")
    if answers == []:
        w.void_lookups += 1


def check_spf(
    domain: str,
    resolver: Resolver,
    *,
    max_lookups: int = LOOKUP_LIMIT,
    max_void_lookups: int = VOID_LOOKUP_LIMIT,
) -> SpfReport:
    """Analyse the SPF policy of ``domain`` without sending any mail."""
    domain = normalize_name(domain)
    w = _Walk(resolver=resolver)
    record = _walk(w, domain, "top", (domain,))

    if w.lookups > max_lookups:
        w.add(
            "SPF_TOO_MANY_LOOKUPS",
            "error",
            f"{w.lookups} DNS-querying terms (limit {max_lookups}); receivers return permerror",
            _REF_LIMITS,
            perm=True,
        )
    elif w.lookups >= max_lookups - 1 and record is not None:
        w.add(
            "SPF_LOOKUPS_NEAR_LIMIT",
            "warning",
            f"{w.lookups}/{max_lookups} DNS-querying terms; one more include will break SPF",
            _REF_LIMITS,
        )
    if w.void_lookups > max_void_lookups:
        w.add(
            "SPF_TOO_MANY_VOID_LOOKUPS",
            "error",
            f"{w.void_lookups} lookups returned nothing (limit {max_void_lookups})",
            _REF_LIMITS,
            perm=True,
        )

    if w.temperror:
        status: SpfStatus = "temperror"
    elif w.permerror:
        status = "permerror"
    elif record is None:
        status = "none"
    else:
        status = "valid"
    return SpfReport(
        domain=domain,
        status=status,
        record=record.raw if record else None,
        lookups=w.lookups,
        void_lookups=w.void_lookups,
        visited=w.visited,
        findings=w.findings,
    )
