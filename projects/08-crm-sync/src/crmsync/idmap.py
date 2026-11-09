"""One stored person key -> CRM id map, kept true when the CRM merges records.

A merge in the CRM deletes the losing record id. Every sheet / Clay table / cache that still
holds it then updates nothing (HubSpot, Attio) or 404s (Pipedrive). Merge webhooks carry the
ids needed to re-point those keys; this module turns each vendor's webhook body into one
``MergeEvent`` and applies it to one ``IdMap`` shared by the three writers.

Shapes (vendor docs DNS-blocked from the build environment, 2026-11-09):
- HubSpot ``*.merge`` subscription events: ``primaryObjectId``, ``mergedObjectIds``,
  sometimes ``newObjectId`` (community / third-party summaries; ``contact.merge`` itself is in
  the official ``@hubspot/api-client`` 14.1.2 webhook enums). A merge may give the result a
  new id, so ``newObjectId`` wins over ``primaryObjectId`` and the primary is then a loser too.
- Attio ``record.merged`` (in the ``attio-ts-sdk`` 2.3.3 event enum): ``id.record_id`` is the
  record after the merge, ``duplicate_record_id`` one original; one event per original.
- Pipedrive webhooks v2 have no merge action: a ``change`` event carries ``merged_from_id``
  (search summary of the v2 migration guide) next to a ``delete`` of the loser.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Protocol


class IdMap(Protocol):
    def get(self, crm: str, key: str) -> str | None: ...
    def put(self, crm: str, key: str, crm_id: str) -> None: ...
    def evict(self, crm: str, key: str) -> None: ...
    def keys_for(self, crm: str, crm_id: str) -> list[str]: ...


@dataclass
class MemoryIdMap:
    """In-process ``(crm, key) -> id`` with a reverse index; swap for a table to persist."""

    ids: dict[tuple[str, str], str] = field(default_factory=dict)
    _by_id: dict[tuple[str, str], set[str]] = field(default_factory=dict, repr=False)

    def get(self, crm: str, key: str) -> str | None:
        return self.ids.get((crm, key))

    def put(self, crm: str, key: str, crm_id: str) -> None:
        self.evict(crm, key)
        self.ids[(crm, key)] = crm_id
        self._by_id.setdefault((crm, crm_id), set()).add(key)

    def evict(self, crm: str, key: str) -> None:
        old = self.ids.pop((crm, key), None)
        if old is not None:
            keys = self._by_id.get((crm, old), set())
            keys.discard(key)
            if not keys:
                self._by_id.pop((crm, old), None)

    def keys_for(self, crm: str, crm_id: str) -> list[str]:
        return sorted(self._by_id.get((crm, crm_id), ()))


@dataclass(frozen=True)
class MergeEvent:
    """Records ``losing_ids`` no longer exist; ``winner_id`` is the record now holding them.

    ``winner_id`` None = the merge was seen but the survivor is unknown: keys are evicted so
    the next write searches again instead of writing to a dead id.
    """

    crm: str
    losing_ids: tuple[str, ...]
    winner_id: str | None


@dataclass
class MergeReport:
    events: int = 0
    remapped: list[tuple[str, str, str]] = field(default_factory=list)  # (key, old, new)
    evicted: list[tuple[str, str]] = field(default_factory=list)  # (key, old)
    ignored: list[str] = field(default_factory=list)  # reason per body item not understood

    def summary(self) -> dict[str, int]:
        return {
            "events": self.events,
            "remapped": len(self.remapped),
            "evicted": len(self.evicted),
            "ignored": len(self.ignored),
        }


def _id(value: object) -> str | None:
    if isinstance(value, bool) or value is None:
        return None
    text = str(value).strip()
    return text or None


def _merge(crm: str, winner: str | None, losers: Iterable[object]) -> MergeEvent | None:
    ids: list[str] = []
    for item in losers:
        lid = _id(item)
        if lid is not None and lid != winner and lid not in ids:
            ids.append(lid)
    return MergeEvent(crm, tuple(ids), winner) if ids else None


def parse_hubspot_merges(body: object, *, crm: str = "hubspot") -> tuple[list[MergeEvent], list]:
    """HubSpot webhook POST body (a JSON array of events) -> merge events + ignored reasons.

    Non-merge events are skipped silently; the object type prefix (``contact`` in
    ``contact.merge``) is appended to ``crm`` so contact and company ids never mix.
    """
    items = body if isinstance(body, list) else [body]
    events: list[MergeEvent] = []
    ignored: list[str] = []
    for i, item in enumerate(items):
        if not isinstance(item, Mapping):
            ignored.append(f"{i}:not_an_object")
            continue
        kind = str(item.get("subscriptionType", ""))
        if not kind.endswith(".merge"):
            continue
        primary = _id(item.get("primaryObjectId"))
        winner = _id(item.get("newObjectId")) or primary
        merged = item.get("mergedObjectIds")
        losers = list(merged) if isinstance(merged, list) else []
        if primary is not None:
            losers.append(primary)  # only a loser when newObjectId names another record
        event = _merge(f"{crm}:{kind.removesuffix('.merge')}", winner, losers)
        if event is None:
            ignored.append(f"{i}:no_merged_ids")
        else:
            events.append(event)
    return events, ignored


def parse_attio_merges(body: object, *, crm: str = "attio") -> tuple[list[MergeEvent], list]:
    """Attio webhook body ``{"webhook_id", "events": [...]}`` -> ``record.merged`` events.

    Ids are namespaced by ``object_id``; a duplicate from another object is not remapped.
    """
    raw = body.get("events") if isinstance(body, Mapping) else None
    events: list[MergeEvent] = []
    ignored: list[str] = []
    for i, item in enumerate(raw if isinstance(raw, list) else []):
        if not isinstance(item, Mapping) or item.get("event_type") != "record.merged":
            continue
        ref = item.get("id") if isinstance(item.get("id"), Mapping) else {}
        object_id = _id(ref.get("object_id"))
        winner = _id(ref.get("record_id"))
        dup_object = _id(item.get("duplicate_object_id")) or object_id
        if winner is None or object_id is None:
            ignored.append(f"{i}:missing_record_id")
            continue
        if dup_object != object_id:
            ignored.append(f"{i}:cross_object_merge")
            continue
        event = _merge(f"{crm}:{object_id}", winner, [item.get("duplicate_record_id")])
        if event is None:
            ignored.append(f"{i}:no_duplicate_record_id")
        else:
            events.append(event)
    return events, ignored


def parse_pipedrive_merge(body: object, *, crm: str = "pipedrive") -> tuple[list[MergeEvent], list]:
    """Pipedrive webhook v2 body -> one merge event when ``merged_from_id`` is present.

    Read from ``meta`` first, then ``data``; the survivor is ``meta.entity_id`` or ``data.id``.
    """
    if not isinstance(body, Mapping):
        return [], ["0:not_an_object"]
    meta = body.get("meta") if isinstance(body.get("meta"), Mapping) else {}
    data = body.get("data") if isinstance(body.get("data"), Mapping) else {}
    loser = meta.get("merged_from_id", data.get("merged_from_id"))
    if _id(loser) is None:
        return [], []
    entity = _id(meta.get("entity")) or "person"
    winner = _id(meta.get("entity_id")) or _id(data.get("id"))
    if winner is None:
        return [MergeEvent(f"{crm}:{entity}", (str(_id(loser)),), None)], []
    event = _merge(f"{crm}:{entity}", winner, [loser])
    return ([event], []) if event else ([], ["0:merged_into_itself"])


def apply_merges(idmap: IdMap, events: Iterable[MergeEvent]) -> MergeReport:
    """Re-point every stored key of a losing id; replaying the same event changes nothing."""
    report = MergeReport()
    for event in events:
        report.events += 1
        for lost in event.losing_ids:
            for key in idmap.keys_for(event.crm, lost):
                if event.winner_id is None:
                    idmap.evict(event.crm, key)
                    report.evicted.append((key, lost))
                else:
                    idmap.put(event.crm, key, event.winner_id)
                    report.remapped.append((key, lost, event.winner_id))
    return report


@dataclass
class IdMapPersonCache:
    """The Pipedrive writer's cache interface over a shared ``IdMap`` (``pipedrive:person``)."""

    idmap: IdMap
    crm: str = "pipedrive:person"

    def get(self, email: str) -> str | None:
        return self.idmap.get(self.crm, email)

    def put(self, email: str, person_id: str) -> None:
        self.idmap.put(self.crm, email, person_id)

    def evict(self, email: str) -> None:
        self.idmap.evict(self.crm, email)
