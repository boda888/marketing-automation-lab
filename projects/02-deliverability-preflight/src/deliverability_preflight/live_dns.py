"""Live DNS behind the ``Resolver`` protocol, using dnspython (optional extra).

Install with ``pip install "deliverability-preflight[live]"``. The default
test suite never touches the network: tests inject a stub in place of
``dns.resolver.Resolver``.

Mapping of dnspython outcomes (``Resolver.resolve`` docstring, dnspython 2.9):

- answer → record strings; a TXT record's character-strings are joined with
  no separator (RFC 7208 §3.3, RFC 6376 §3.6.2.2), so a 2048-bit DKIM key
  split into 255-byte chunks comes back as one string;
- ``NXDOMAIN`` / ``NoAnswer`` / ``YXDOMAIN`` → ``[]`` (a void lookup for SPF);
- ``LifetimeTimeout``, ``NoNameservers`` and any other DNS error →
  ``DNSTempError``: the answer is unknown, never treated as empty.

Unlike ``FakeResolver`` it can tell NXDOMAIN from "no records of that type",
so ``domain_exists`` lets ``check_dmarc`` pick ``np`` over ``sp`` itself.
"""

from __future__ import annotations

from typing import Any

from .dns import DNSTempError, normalize_name

try:
    import dns.exception
    import dns.resolver
except ImportError:  # pragma: no cover - exercised only without the extra
    dns = None  # type: ignore[assignment]


class DnsPythonResolver:
    """``Resolver`` that queries real DNS through ``dns.resolver.Resolver``.

    ``resolver`` defaults to a fresh dnspython resolver using the system
    configuration; pass one built with ``configure=False`` and explicit
    ``nameservers`` to pin the recursive resolver. ``lifetime`` caps each
    query in seconds. Names are queried as absolute (no search list), so
    ``acme.example`` never becomes ``acme.example.corp.local``.
    """

    def __init__(self, resolver: Any | None = None, *, lifetime: float = 5.0) -> None:
        if dns is None:
            raise ImportError(
                'dnspython is not installed; pip install "deliverability-preflight[live]"'
            )
        self._resolver = resolver if resolver is not None else dns.resolver.Resolver()
        self._lifetime = lifetime
        self._nxdomain: set[str] = set()
        self.queries: list[tuple[str, str]] = []

    def _resolve(self, name: str, rtype: str, *, raise_on_no_answer: bool = True) -> Any:
        return self._resolver.resolve(
            name + ".",
            rtype,
            raise_on_no_answer=raise_on_no_answer,
            lifetime=self._lifetime,
            search=False,
        )

    def lookup(self, name: str, rtype: str) -> list[str]:
        name, rtype = normalize_name(name), rtype.upper()
        self.queries.append((name, rtype))
        try:
            answer = self._resolve(name, rtype)
        except dns.resolver.NXDOMAIN:
            self._nxdomain.add(name)
            return []
        except (dns.resolver.NoAnswer, dns.resolver.YXDOMAIN):
            return []
        except dns.exception.DNSException as exc:
            raise DNSTempError(f"{name} {rtype}: {exc.__class__.__name__}: {exc}") from exc
        if rtype == "TXT":
            return [b"".join(r.strings).decode("utf-8", "replace") for r in answer]
        return [r.to_text() for r in answer]

    def domain_exists(self, name: str) -> bool | None:
        """False on NXDOMAIN, True when the name exists, None if DNS failed.

        Reuses an NXDOMAIN already seen by ``lookup``; otherwise asks for the
        name's A record without raising on an empty answer, because a name
        with no A record (but other records) still exists.
        """
        name = normalize_name(name)
        if name in self._nxdomain:
            return False
        self.queries.append((name, "A"))
        try:
            self._resolve(name, "A", raise_on_no_answer=False)
        except dns.resolver.NXDOMAIN:
            self._nxdomain.add(name)
            return False
        except dns.exception.DNSException:
            return None
        return True
