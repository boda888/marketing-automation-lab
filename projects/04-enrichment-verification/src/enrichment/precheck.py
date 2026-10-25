"""Email precheck without SMTP probing: syntax, mail host, disposable, role, catch-all.

A precheck can rule an address *out*; it can never rule one *in*. Nothing here opens an SMTP
connection, so no result is ever ``valid``: the best a clean address gets is ``unknown``,
which places it on the ladder as ``checked``, never ``verified``.

    syntax          dot-atom local part (<= 64), LDH domain labels (<= 63), address <= 254
    mail host       MX via an injectable resolver; a single ``0 .`` record is a null MX
                    (RFC 7505: the domain accepts no mail); no MX but an A/AAAA record is an
                    implicit MX (RFC 5321 §5.1), allowed but riskier; NXDOMAIN or no host at
                    all is invalid; a DNS failure is unknown, never invalid
    disposable      domain or any parent domain on a blocklist -> do not mail
    role            ``info@``, ``sales@``, ... (``+tag`` ignored) -> risk, not invalid
    catch-all       a domain the caller already knows accepts every address -> catch_all

Lists are injectable; the built-in ones are small samples (see ``DISPOSABLE_SAMPLE`` and
``ROLE_LOCAL_PARTS``), and ``load_domain_list`` reads the one-domain-per-line format of the
disposable-email-domains blocklist. DNS answers are cached per domain, so a batch of 500
contacts at one company costs one MX query.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, Field

from enrichment.ladder import CheckResult, EmailCheck, normalise_email

PRECHECK_CHECKER = "lab-precheck"


class DNSTempError(Exception):
    """The lookup failed for now (timeout, SERVFAIL): the answer is unknown, not empty."""


class DomainNotFound(Exception):
    """NXDOMAIN: the domain does not exist."""


class Resolver(Protocol):
    def lookup(self, name: str, rtype: str) -> list[str]:
        """Answers as text (MX: ``"10 mx1.example."``); [] when the name has none of ``rtype``.

        Raises ``DomainNotFound`` for NXDOMAIN and ``DNSTempError`` for temporary failures.
        """
        ...


class FakeResolver:
    """In-memory resolver: ``records[(name, rtype)]``; ``nxdomain`` and ``fail`` name sets."""

    def __init__(
        self,
        records: Mapping[tuple[str, str], Iterable[str]],
        *,
        nxdomain: Iterable[str] = (),
        fail: Iterable[str] = (),
    ) -> None:
        self._records = {(_dns_name(n), t.upper()): list(v) for (n, t), v in records.items()}
        self._nxdomain = {_dns_name(n) for n in nxdomain}
        self._fail = {_dns_name(n) for n in fail}
        self.queries: list[tuple[str, str]] = []

    def lookup(self, name: str, rtype: str) -> list[str]:
        key = (_dns_name(name), rtype.upper())
        self.queries.append(key)
        if key[0] in self._fail:
            raise DNSTempError(f"temporary failure for {key[0]} {key[1]}")
        if key[0] in self._nxdomain:
            raise DomainNotFound(key[0])
        return list(self._records.get(key, []))


def _dns_name(name: str) -> str:
    return name.strip().lower().rstrip(".")


# A sample of the disposable-email-domains blocklist (CC0); load the full list in production.
DISPOSABLE_SAMPLE = frozenset(
    {"10minutemail.com", "guerrillamail.com", "mailinator.com", "trashmail.com", "yopmail.com"}
)

# Mailboxes for a function, not a person: a subset of the role-based-email-addresses
# package 3.1.0 (MIT). Pass ``role_local_parts=`` to use a longer list.
ROLE_LOCAL_PARTS = frozenset(
    {
        "abuse",
        "admin",
        "billing",
        "careers",
        "contact",
        "help",
        "hello",
        "hostmaster",
        "hr",
        "info",
        "jobs",
        "legal",
        "marketing",
        "no-reply",
        "noreply",
        "office",
        "postmaster",
        "privacy",
        "sales",
        "security",
        "support",
        "team",
        "webmaster",
    }
)


def load_domain_list(path: str | Path) -> frozenset[str]:
    """One domain per line; blanks and ``#`` comments skipped; lower-cased."""
    domains = set()
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        entry = line.split("#", 1)[0].strip().lower().rstrip(".")
        if entry:
            domains.add(entry)
    return frozenset(domains)


class PrecheckSeverity(StrEnum):
    BLOCK = "block"  # the address cannot receive mail, or must not be mailed
    RISK = "risk"  # may be mailable; raises the risk score


class PrecheckFinding(BaseModel):
    code: str
    severity: PrecheckSeverity
    detail: str


# Risk added per finding; any BLOCK finding sets the score to 100.
RISK_WEIGHTS = {
    "implicit_mx": 20,
    "role_address": 30,
    "catch_all_domain": 40,
    "dns_temp_error": 30,
    "non_ascii": 20,
}


class PrecheckReport(BaseModel):
    email: str
    domain: str | None
    result: CheckResult = Field(description="Never 'valid': a precheck cannot prove delivery")
    risk: int = Field(ge=0, le=100)
    findings: list[PrecheckFinding]
    mx_hosts: list[str] = Field(default_factory=list, description="By preference, best first")

    def codes(self) -> list[str]:
        return [f.code for f in self.findings]

    def to_check(self, *, checked_at: datetime) -> EmailCheck:
        """An ``EmailCheck`` for the ladder, by checker ``lab-precheck``."""
        return EmailCheck(
            email=self.email,
            checker=PRECHECK_CHECKER,
            checked_at=checked_at,
            result=self.result,
            provider_status=",".join(self.codes()) or "clean",
        )


_ATOM = r"[a-z0-9!#$%&'*+/=?^_`{|}~-]+"
_DOT_ATOM = re.compile(rf"{_ATOM}(\.{_ATOM})*", re.IGNORECASE)
_LABEL = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?", re.IGNORECASE)


def _syntax_error(local: str, domain: str) -> str | None:
    if not local or not domain:
        return "missing local part or domain"
    if len(local) > 64:
        return "local part longer than 64 octets"
    if len(local) + 1 + len(domain) > 254:
        return "address longer than 254 octets"
    if not _DOT_ATOM.fullmatch(local):
        return "local part is not a dot-atom (quoted local parts are not accepted)"
    if len(domain) > 253:
        return "domain longer than 253 octets"
    labels = domain.split(".")
    if len(labels) < 2:
        return "domain has no dot"
    if not all(_LABEL.fullmatch(label) for label in labels):
        return "domain label is not letters, digits and inner hyphens (<= 63)"
    if labels[-1].isdigit():
        return "top-level label is numeric"
    return None


def _parse_mx(answer: str) -> tuple[int, str] | None:
    parts = answer.split()
    if len(parts) != 2 or not parts[0].isdigit():
        return None
    return int(parts[0]), parts[1].lower()


def _on_list(domain: str, blocklist: frozenset[str]) -> str | None:
    labels = domain.split(".")
    for i in range(len(labels) - 1):
        candidate = ".".join(labels[i:])
        if candidate in blocklist:
            return candidate
    return None


class _HostAnswer(BaseModel):
    findings: list[PrecheckFinding]
    mx_hosts: list[str]


class EmailPrecheck:
    """Offline-first precheck; one instance per batch shares its DNS cache."""

    def __init__(
        self,
        resolver: Resolver,
        *,
        disposable_domains: Iterable[str] = DISPOSABLE_SAMPLE,
        role_local_parts: Iterable[str] = ROLE_LOCAL_PARTS,
        catch_all_domains: Iterable[str] = (),
    ) -> None:
        self.resolver = resolver
        self.disposable = frozenset(_dns_name(d) for d in disposable_domains)
        self.roles = frozenset(r.strip().lower() for r in role_local_parts)
        self.catch_all = frozenset(_dns_name(d) for d in catch_all_domains)
        self._hosts: dict[str, _HostAnswer] = {}

    def check(self, email: str) -> PrecheckReport:
        address = normalise_email(email)
        local, at, domain = address.rpartition("@")
        domain = domain.rstrip(".")
        findings: list[PrecheckFinding] = []

        if not at:
            return self._report(address, None, [_block("syntax_invalid", "no @ in the address")])
        if not address.isascii():
            try:
                ascii_domain = domain.encode("idna").decode("ascii")
            except UnicodeError:
                ascii_domain = ""
            if not local.isascii() or not ascii_domain:
                detail = "non-ASCII local part needs SMTPUTF8; most senders cannot deliver it"
                return self._report(address, domain, [_block("syntax_invalid", detail)])
            findings.append(_risk("non_ascii", f"IDN domain sent as {ascii_domain}"))
            domain = ascii_domain
        error = _syntax_error(local, domain)
        if error:
            return self._report(address, domain, [_block("syntax_invalid", error)])

        listed = _on_list(domain, self.disposable)
        if listed:
            detail = f"{listed} is on the disposable-domain list"
            findings.append(_block("disposable", detail))
            return self._report(address, domain, findings)

        host = self._host(domain)
        findings.extend(host.findings)
        if local.split("+", 1)[0] in self.roles:
            findings.append(_risk("role_address", f"{local}@ is a function, not a person"))
        if domain in self.catch_all:
            detail = "domain accepts every address: deliverability of this one is unknowable"
            findings.append(_risk("catch_all_domain", detail))
        return self._report(address, domain, findings, host.mx_hosts)

    def check_many(self, emails: Sequence[str]) -> list[PrecheckReport]:
        return [self.check(e) for e in emails]

    def _host(self, domain: str) -> _HostAnswer:
        if domain not in self._hosts:
            self._hosts[domain] = self._resolve(domain)
        return self._hosts[domain]

    def _resolve(self, domain: str) -> _HostAnswer:
        def answer(*findings: PrecheckFinding, hosts: Sequence[str] = ()) -> _HostAnswer:
            return _HostAnswer(findings=list(findings), mx_hosts=list(hosts))

        try:
            raw = self.resolver.lookup(domain, "MX")
        except DomainNotFound:
            return answer(_block("domain_not_found", f"{domain} does not exist (NXDOMAIN)"))
        except DNSTempError as exc:
            return answer(_temp(f"MX lookup failed: {exc}"))

        records = [_parse_mx(a) for a in raw]
        if any(r is None for r in records):
            return answer(_temp(f"unparseable MX answer {raw!r}"))
        parsed = sorted(r for r in records if r is not None)
        if parsed and any(host in {".", ""} for _, host in parsed):
            if len(parsed) == 1 and parsed[0][0] == 0:
                return answer(
                    _block("null_mx", f"{domain} publishes a null MX: it accepts no mail")
                )
            detail = "null MX mixed with other MX records (RFC 7505 forbids it)"
            return answer(_block("null_mx", detail))
        if parsed:
            return answer(hosts=[host.rstrip(".") for _, host in parsed])

        found_address = False
        for rtype in ("A", "AAAA"):
            try:
                found_address = bool(self.resolver.lookup(domain, rtype))
            except DomainNotFound:
                return answer(_block("domain_not_found", f"{domain} does not exist (NXDOMAIN)"))
            except DNSTempError as exc:
                return answer(_temp(f"{rtype} lookup failed: {exc}"))
            if found_address:
                break
        if not found_address:
            return answer(_block("no_mail_host", f"{domain} has no MX and no A/AAAA record"))
        detail = "no MX record; mail would go to the domain's A/AAAA host"
        return answer(_risk("implicit_mx", detail), hosts=[domain])

    def _report(
        self,
        email: str,
        domain: str | None,
        findings: list[PrecheckFinding],
        mx_hosts: Sequence[str] = (),
    ) -> PrecheckReport:
        codes = {f.code for f in findings}
        if "disposable" in codes:
            result = CheckResult.DO_NOT_MAIL
        elif any(f.severity is PrecheckSeverity.BLOCK for f in findings):
            result = CheckResult.INVALID
        elif "catch_all_domain" in codes:
            result = CheckResult.CATCH_ALL
        else:
            result = CheckResult.UNKNOWN
        if any(f.severity is PrecheckSeverity.BLOCK for f in findings):
            risk = 100
        else:
            risk = min(100, sum(RISK_WEIGHTS.get(f.code, 0) for f in findings))
        return PrecheckReport(
            email=email,
            domain=domain,
            result=result,
            risk=risk,
            findings=findings,
            mx_hosts=list(mx_hosts),
        )


def _block(code: str, detail: str) -> PrecheckFinding:
    return PrecheckFinding(code=code, severity=PrecheckSeverity.BLOCK, detail=detail)


def _risk(code: str, detail: str) -> PrecheckFinding:
    return PrecheckFinding(code=code, severity=PrecheckSeverity.RISK, detail=detail)


def _temp(detail: str) -> PrecheckFinding:
    return _risk("dns_temp_error", detail)


def catch_all_domains(checks: Iterable[EmailCheck]) -> frozenset[str]:
    """Domains an independent verifier has already reported as catch-all, from its checks.

    The lab's own precheck never feeds this: it cannot tell a catch-all domain on its own.
    """
    return frozenset(
        normalise_email(c.email).rpartition("@")[2]
        for c in checks
        if c.result is CheckResult.CATCH_ALL and c.checker != PRECHECK_CHECKER
    )
