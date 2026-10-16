"""Public Suffix List (PSL) matching for organizational domains.

RFC 7489 §3.2 derives a domain's Organizational Domain from a public suffix
list: the public suffix plus one more label. ``PublicSuffixList`` reads the
list in its published format (https://publicsuffix.org/list/public_suffix_list.dat):

- one rule per line; ``//`` comments and blank lines are ignored;
- ``*`` as a label matches any one label (``*.ck``);
- a rule starting with ``!`` is an exception (``!www.ck``): it wins over
  other rules and its public suffix is the rule minus its left-most label;
- otherwise the matching rule with the most labels wins, and when nothing
  matches, the implicit rule ``*`` makes the TLD the public suffix;
- rules between ``===BEGIN PRIVATE DOMAINS===`` and ``===END PRIVATE
  DOMAINS===`` (``github.io``, ``blogspot.com``…) can be left out.

The list is not bundled: it changes weekly and its maintainers ask users to
download it from publicsuffix.org. ``PublicSuffixList.from_file`` loads a
local copy; ``.org_domain`` plugs into every ``org_domain=`` parameter.
"""

from __future__ import annotations

from pathlib import Path

from .dns import normalize_name

_PRIVATE_BEGIN = "===BEGIN PRIVATE DOMAINS==="
_PRIVATE_END = "===END PRIVATE DOMAINS==="


def _ascii(name: str) -> str:
    """IDNA A-label form of ``name``; unchanged when it is already ASCII or not encodable."""
    if name.isascii():
        return name
    try:
        return name.encode("idna").decode("ascii")
    except UnicodeError:
        return name


class PublicSuffixList:
    def __init__(self, text: str, *, include_private: bool = True) -> None:
        self.rules: set[str] = set()
        self.exceptions: set[str] = set()
        private = False
        for raw in text.splitlines():
            line = raw.strip()
            if line.startswith("//"):
                if _PRIVATE_BEGIN in line:
                    private = True
                elif _PRIVATE_END in line:
                    private = False
                continue
            # a rule ends at the first whitespace
            rule = line.split()[0].lower().rstrip(".") if line else ""
            if not rule or (private and not include_private):
                continue
            target = self.exceptions if rule.startswith("!") else self.rules
            rule = rule.lstrip("!")
            target.add(rule)
            target.add(_ascii(rule))

    @classmethod
    def from_file(cls, path: str | Path, *, include_private: bool = True) -> PublicSuffixList:
        return cls(Path(path).read_text(encoding="utf-8"), include_private=include_private)

    def public_suffix(self, domain: str) -> str:
        """The public suffix of ``domain`` (``co.uk`` for ``shop.co.uk``)."""
        labels = normalize_name(domain).split(".")
        for n in range(len(labels), 0, -1):
            candidate = ".".join(labels[-n:])
            if candidate in self.exceptions:
                return ".".join(labels[-n + 1 :])
        best = 1  # implicit "*" rule
        for n in range(len(labels), 1, -1):
            tail = labels[-n:]
            wild = ".".join(["*", *tail[1:]])
            if ".".join(tail) in self.rules or wild in self.rules:
                best = n
                break
        return ".".join(labels[-best:])

    def registrable_domain(self, domain: str) -> str | None:
        """Public suffix plus one label; ``None`` when ``domain`` is itself a suffix."""
        labels = normalize_name(domain).split(".")
        n = self.public_suffix(domain).count(".") + 1
        if len(labels) <= n:
            return None
        return ".".join(labels[-(n + 1) :])

    def org_domain(self, domain: str) -> str:
        """``OrgDomainFn``: the registrable domain, or ``domain`` itself when it is a suffix."""
        return self.registrable_domain(domain) or normalize_name(domain)
