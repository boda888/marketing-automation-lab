"""write(rows, crm=...): one entry point, ids stored where merge webhooks will look."""

from collections import deque

import pytest
from crmsync import (
    CrmRow,
    HttpResponse,
    MemoryIdMap,
    Outcome,
    RateBudget,
    WriteOptions,
    apply_merges,
    parse_attio_merges,
    parse_hubspot_merges,
    write,
)
from crmsync.hubspot import trace_id

PEOPLE_UUID = "0f6c3b9e-6d1a-4c55-9a0e-2b8d7f1e4a10"


class Fake:
    def __init__(self, *script):
        self.script = deque(script)
        self.calls = []

    def send(self, call):
        self.calls.append(call)
        return self.script.popleft()


def budget():
    return RateBudget(100, 1.0, clock=lambda: 0.0, sleep=lambda s: None)


def run(rows, fake, **kw):
    return write(
        rows,
        transport=fake,
        token="t",
        budget=budget(),
        allow_fixtures=True,
        sleep=lambda s: None,
        **kw,
    )


ANA = CrmRow("r1", "ana@northwind.example", {"firstname": "Ana"})
BEN = CrmRow("r2", "ben@northwind.example", {"firstname": "Ben"})
NOKEY = CrmRow("r3", None, {"firstname": "Cy"})


def hubspot_body(*results, errors=()):
    return {"status": "COMPLETE", "results": list(results), "errors": list(errors)}


def result(email, crm_id, new):
    return {"id": crm_id, "_new": new, "objectWriteTraceId": trace_id(email), "properties": {}}


def test_hubspot_landed_ids_go_to_the_contact_namespace_and_follow_a_merge():
    idmap = MemoryIdMap()
    body = hubspot_body(
        result("ana@northwind.example", "101", True),
        errors=[{"category": "VALIDATION_ERROR", "message": "bad", "context": {"id": [BEN.email]}}],
    )
    out = run([ANA, BEN, NOKEY], Fake(HttpResponse(207, body)), crm="hubspot", idmap=idmap)

    assert [o.row_id for o in out.outcomes] == ["r1", "r2", "r3"]
    assert [o.outcome for o in out.outcomes] == [Outcome.CREATED, Outcome.FAILED, Outcome.SKIPPED]
    assert out.idmap_namespace == "hubspot:contact"
    assert out.idmap_recorded == 1
    assert idmap.ids == {("hubspot:contact", "ana@northwind.example"): "101"}

    # HubSpot merges contact 101 into 202: the key this write stored is re-pointed.
    events, _ = parse_hubspot_merges(
        [{"subscriptionType": "contact.merge", "primaryObjectId": 202, "mergedObjectIds": [101]}]
    )
    report = apply_merges(idmap, events)
    assert report.remapped == [("ana@northwind.example", "101", "202")]
    assert idmap.get("hubspot:contact", "ana@northwind.example") == "202"


def test_without_idmap_outcomes_are_unchanged_and_nothing_is_recorded():
    body = hubspot_body(result("ana@northwind.example", "101", False))
    out = run([ANA], Fake(HttpResponse(200, body)), crm="hubspot")
    assert out.outcomes[0].outcome is Outcome.UPDATED
    assert out.idmap_namespace is None and out.idmap_recorded == 0
    assert out.summary["updated"] == 1 and out.summary["calls"] == 1


def test_attio_with_idmap_refuses_a_slug_only_object_before_any_call():
    fake = Fake()
    with pytest.raises(ValueError, match="attio_object_id"):
        run([ANA], fake, crm="attio", idmap=MemoryIdMap())
    assert fake.calls == []


def test_attio_ids_are_namespaced_by_object_uuid_so_record_merged_finds_them():
    idmap = MemoryIdMap()
    record = {"data": {"id": {"object_id": PEOPLE_UUID, "record_id": "rec-a"}}}
    out = run(
        [CrmRow("r1", "ana@northwind.example", {"job_title": "COO"})],
        Fake(HttpResponse(200, record)),
        crm="attio",
        idmap=idmap,
        options=WriteOptions(attio_object_id=PEOPLE_UUID.upper()),
    )
    assert out.outcomes[0].outcome is Outcome.UPSERTED
    assert out.idmap_namespace == f"attio:{PEOPLE_UUID}"

    merged = {
        "events": [
            {
                "event_type": "record.merged",
                "id": {"object_id": PEOPLE_UUID, "record_id": "rec-b"},
                "duplicate_record_id": "rec-a",
            }
        ]
    }
    events, ignored = parse_attio_merges(merged)
    assert ignored == []
    apply_merges(idmap, events)
    assert idmap.get(f"attio:{PEOPLE_UUID}", "ana@northwind.example") == "rec-b"


def test_pipedrive_reads_the_shared_idmap_and_patches_without_searching():
    idmap = MemoryIdMap()
    idmap.put("pipedrive:person", "ana@northwind.example", "77")
    fake = Fake(HttpResponse(200, {"success": True, "data": {"id": 77}}))
    out = run(
        [CrmRow("r1", "ana@northwind.example", {"name": "Ana"})], fake, crm="pipedrive", idmap=idmap
    )

    assert [c.method for c in fake.calls] == ["PATCH"]
    assert fake.calls[0].url.endswith("/persons/77")
    assert out.outcomes[0].outcome is Outcome.UPDATED and out.outcomes[0].crm_id == "77"
    assert out.summary["cache_hits"] == 1 and out.idmap_recorded == 1


def test_unknown_crm_is_refused():
    with pytest.raises(ValueError, match="unknown crm"):
        run([ANA], Fake(), crm="salesforce")
