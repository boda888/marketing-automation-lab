"""DNS access behind a tiny interface so every check runs offline in tests.

A resolver returns the record strings for ``(name, rtype)``. NXDOMAIN and
"no records of that type" both return ``[]`` — SPF treats them the same way
(a *void lookup*, RFC 7208 §4.6.4). Transient failures raise ``DNSTempError``.

No live resolver adapter ships yet; plug one in (e.g. dnspython) by
implementing ``Resolver.lookup``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Protocol


class DNSTempError(Exception):
    """Timeout / SERVFAIL — the answer is unknown, not empty."""


class Resolver(Protocol):
    def lookup(self, name: str, rtype: str) -> list[str]: ...


def normalize_name(name: str) -> str:
    return name.strip().rstrip(".").lower()


class FakeResolver:
    """In-memory resolver for tests and examples.

    ``records`` maps ``(name, rtype)`` to answers; names are case-insensitive.
    ``fail`` lists ``(name, rtype)`` pairs that raise ``DNSTempError``.
    Every query is recorded in ``queries`` so tests can assert on DNS cost.
    """

    def __init__(
        self,
        records: Mapping[tuple[str, str], Iterable[str]],
        fail: Iterable[tuple[str, str]] = (),
    ) -> None:
        self._records = {(normalize_name(n), t.upper()): list(v) for (n, t), v in records.items()}
        self._fail = {(normalize_name(n), t.upper()) for n, t in fail}
        self.queries: list[tuple[str, str]] = []

    def lookup(self, name: str, rtype: str) -> list[str]:
        key = (normalize_name(name), rtype.upper())
        self.queries.append(key)
        if key in self._fail:
            raise DNSTempError(f"temporary failure for {key[0]} {key[1]}")
        return list(self._records.get(key, []))
