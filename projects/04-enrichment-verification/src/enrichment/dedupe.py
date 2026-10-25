"""Contact dedupe: LinkedIn URL -> email -> normalised name + company, with provenance.

PRACTICE: candidates for the same person arrive from several sources; QA found the right
current employer, but later steps still read the old employer and built the wrong domain and
email. Here every candidate is merged into one person record, and that one record feeds
every downstream step:

- Strong keys join candidates: the normalised LinkedIn profile URL (``/in/<slug>``, any
  ``*.linkedin.com`` host, query and trailing slash dropped) and the normalised email.
- The weak key ``name + company domain`` joins only when the rows sharing it carry at most one
  LinkedIn profile: two people with the same name at one company stay apart, and a row with
  that name but no profile joins neither and is flagged ``ambiguous_name_match``.
- Two profiles joined by one email are flagged ``conflicting_linkedin_profiles``.
- Each field takes the value from the newest observation; same date -> the earlier source in
  ``source_priority``, then input order. Every field keeps its provenance (source, candidate,
  date); a field seen with several values lists them all in ``conflicts``.
- Identity consistency: an email whose domain is not the chosen company domain (or a
  subdomain of it) is flagged ``email_domain_mismatch`` and the person goes to ``review``.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from datetime import datetime
from urllib.parse import unquote, urlsplit

from pydantic import BaseModel, Field, field_validator

from enrichment.ladder import normalise_email
from enrichment.models import clean


class ContactCandidate(BaseModel):
    """One source's view of a person: its own id, the source, when it was observed."""

    candidate_id: str = Field(min_length=1)
    source: str = Field(min_length=1, description="Provider or list the row came from")
    observed_at: datetime = Field(description="When the source saw this; timezone required")
    fields: dict[str, str | None] = Field(default_factory=dict)

    @field_validator("candidate_id", "source")
    @classmethod
    def _named(cls, v: str) -> str:
        cleaned = clean(v)
        if cleaned is None:
            raise ValueError(f"must be a real value, not {v!r}")
        return cleaned

    @field_validator("observed_at")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None or v.utcoffset() is None:
            raise ValueError("observed_at needs a timezone")
        return v

    @field_validator("fields", mode="before")
    @classmethod
    def _clean_fields(cls, v: object) -> object:
        if isinstance(v, dict):
            return {str(k).strip(): clean(val) for k, val in v.items()}
        return v


def linkedin_key(url: str | None) -> str | None:
    """``linkedin.com/in/<slug>`` for a profile URL; ``None`` for anything else."""
    url = clean(url)
    if url is None:
        return None
    if "://" not in url:
        url = "https://" + url
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if host != "linkedin.com" and not host.endswith(".linkedin.com"):
        return None
    segments = [s for s in unquote(parts.path).split("/") if s]
    if len(segments) < 2 or segments[0].lower() != "in":
        return None
    return f"linkedin.com/in/{segments[1].lower()}"


def email_key(email: str | None) -> str | None:
    email = clean(email)
    return normalise_email(email) if email and "@" in email else None


def domain_key(domain: str | None) -> str | None:
    domain = clean(domain)
    if domain is None:
        return None
    if "://" in domain:
        domain = urlsplit(domain).hostname or ""
    domain = domain.split("/", 1)[0].strip().lower().rstrip(".")
    domain = domain.removeprefix("www.")
    return domain or None


def name_key(name: str | None) -> str | None:
    """Case, accents, punctuation and spacing removed: ``"José  O'Neil"`` -> ``"jose oneil"``."""
    name = clean(name)
    if name is None:
        return None
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    words = re.sub(r"[^a-z0-9 ]", "", ascii_name.lower().replace("-", " ")).split()
    return " ".join(words) or None


class FieldValue(BaseModel):
    value: str
    source: str
    candidate_id: str
    observed_at: datetime


class PersonRecord(BaseModel):
    person_id: str = Field(description="candidate_id of the group's first candidate (input order)")
    fields: dict[str, str]
    provenance: dict[str, FieldValue]
    conflicts: dict[str, list[str]] = Field(
        default_factory=dict, description="Fields seen with more than one value, all values"
    )
    candidate_ids: list[str]
    matched_on: list[str] = Field(description="Keys that joined candidates: linkedin, email, name")
    findings: list[str] = Field(default_factory=list)

    @property
    def review(self) -> bool:
        return bool(self.findings)


class _Groups:
    def __init__(self, n: int) -> None:
        self.parent = list(range(n))

    def find(self, i: int) -> int:
        while self.parent[i] != i:
            self.parent[i] = self.parent[self.parent[i]]
            i = self.parent[i]
        return i

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)


def dedupe_contacts(
    candidates: Sequence[ContactCandidate], *, source_priority: Sequence[str] = ()
) -> list[PersonRecord]:
    """Merge candidates into people; output in order of each person's first candidate."""
    ids = [c.candidate_id for c in candidates]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate candidate_id: provenance would point at two rows")

    groups = _Groups(len(candidates))
    matched: dict[int, set[str]] = {}
    profiles: dict[int, set[str]] = {}
    for i, c in enumerate(candidates):
        if key := linkedin_key(c.fields.get("linkedin_url")):
            profiles[i] = {key}

    def join(a: int, b: int, key: str) -> None:
        ra, rb = groups.find(a), groups.find(b)
        reasons = matched.pop(ra, set()) | matched.pop(rb, set()) | {key}
        merged = profiles.pop(ra, set()) | profiles.pop(rb, set())
        groups.union(a, b)
        matched[groups.find(a)] = reasons
        profiles[groups.find(a)] = merged

    for kind, key_of in (
        ("linkedin", lambda c: linkedin_key(c.fields.get("linkedin_url"))),
        ("email", lambda c: email_key(c.fields.get("email"))),
    ):
        first: dict[str, int] = {}
        for i, c in enumerate(candidates):
            key = key_of(c)
            if key is None:
                continue
            if key in first:
                join(first[key], i, kind)
            else:
                first[key] = i

    by_name: dict[tuple[str, str], list[int]] = {}
    for i, c in enumerate(candidates):
        name = name_key(c.fields.get("full_name"))
        company = domain_key(c.fields.get("company_domain"))
        if name is not None and company is not None:
            by_name.setdefault((name, company), []).append(i)

    ambiguous: set[int] = set()
    for idx in by_name.values():
        roots = {groups.find(i) for i in idx}
        if len(roots) < 2:
            continue
        if len(set().union(*(profiles.get(r, set()) for r in roots))) > 1:
            # Same name at one company, two LinkedIn profiles: two people. A row without a
            # profile could be either of them, so it is joined to neither.
            ambiguous.update(i for i in idx if groups.find(i) not in profiles)
            continue
        for i in idx[1:]:
            if groups.find(i) != groups.find(idx[0]):
                join(idx[0], i, "name")

    members: dict[int, list[int]] = {}
    for i in range(len(candidates)):
        members.setdefault(groups.find(i), []).append(i)

    rank = {s: n for n, s in enumerate(source_priority)}
    ambiguous_roots = {groups.find(i) for i in ambiguous}
    return [
        _merge(
            [candidates[i] for i in idx],
            sorted(matched.get(root, set())),
            rank,
            ambiguous=root in ambiguous_roots,
        )
        for root, idx in sorted(members.items(), key=lambda kv: kv[1][0])
    ]


def _merge(
    group: list[ContactCandidate],
    matched_on: list[str],
    rank: dict[str, int],
    *,
    ambiguous: bool = False,
) -> PersonRecord:
    ordered = sorted(
        enumerate(group),
        key=lambda ic: (-ic[1].observed_at.timestamp(), rank.get(ic[1].source, len(rank)), ic[0]),
    )
    fields: dict[str, str] = {}
    provenance: dict[str, FieldValue] = {}
    seen: dict[str, dict[str, str]] = {}
    for _, c in ordered:
        for name, value in c.fields.items():
            if value is None:
                continue
            seen.setdefault(name, {}).setdefault(_comparable(name, value), value)
            if name not in fields:
                fields[name] = value
                provenance[name] = FieldValue(
                    value=value,
                    source=c.source,
                    candidate_id=c.candidate_id,
                    observed_at=c.observed_at,
                )
    conflicts = {name: list(v.values()) for name, v in seen.items() if len(v) > 1}

    findings = ["ambiguous_name_match"] if ambiguous else []
    if len(conflicts.get("linkedin_url", [])) > 1:
        findings.append("conflicting_linkedin_profiles")
    email, company = email_key(fields.get("email")), domain_key(fields.get("company_domain"))
    if email and company:
        email_domain = email.rpartition("@")[2]
        if email_domain != company and not email_domain.endswith("." + company):
            findings.append("email_domain_mismatch")
    return PersonRecord(
        person_id=group[0].candidate_id,
        fields=fields,
        provenance=provenance,
        conflicts=conflicts,
        candidate_ids=[c.candidate_id for c in group],
        matched_on=matched_on,
        findings=findings,
    )


def _comparable(field: str, value: str) -> str:
    keyed = {
        "linkedin_url": linkedin_key,
        "email": email_key,
        "full_name": name_key,
        "company_domain": domain_key,
    }.get(field)
    return (keyed(value) if keyed else None) or value.strip().lower()
