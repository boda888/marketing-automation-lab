from crmsync import (
    CrmRow,
    HttpResponse,
    IdMapPersonCache,
    MemoryIdMap,
    MergeEvent,
    RateBudget,
    apply_merges,
    execute_persons,
    parse_attio_merges,
    parse_hubspot_merges,
    parse_pipedrive_merge,
    plan_persons,
)


def _map(crm: str, pairs: dict[str, str]) -> MemoryIdMap:
    m = MemoryIdMap()
    for key, cid in pairs.items():
        m.put(crm, key, cid)
    return m


def test_memory_idmap_reverse_index_follows_put_and_evict():
    m = MemoryIdMap()
    m.put("hubspot:contact", "ana@northwind.example", "101")
    m.put("hubspot:contact", "ana.ruiz@northwind.example", "101")
    assert m.keys_for("hubspot:contact", "101") == [
        "ana.ruiz@northwind.example",
        "ana@northwind.example",
    ]
    m.put("hubspot:contact", "ana@northwind.example", "102")  # re-pointed: old reverse entry gone
    assert m.keys_for("hubspot:contact", "101") == ["ana.ruiz@northwind.example"]
    m.evict("hubspot:contact", "ana.ruiz@northwind.example")
    assert m.keys_for("hubspot:contact", "101") == []
    assert m.keys_for("attio:people", "102") == []  # namespaces never mix


def test_hubspot_merge_remaps_losers_to_primary():
    body = [
        {"subscriptionType": "contact.propertyChange", "objectId": 5},
        {
            "subscriptionType": "contact.merge",
            "objectId": 101,
            "primaryObjectId": 101,
            "mergedObjectIds": [202, 303],
            "numberOfPropertiesMoved": 4,
        },
    ]
    events, ignored = parse_hubspot_merges(body)
    assert ignored == []
    assert events == [MergeEvent("hubspot:contact", ("202", "303"), "101")]
    m = _map("hubspot:contact", {"bo@lumen.example": "202", "cy@lumen.example": "101"})
    report = apply_merges(m, events)
    assert report.remapped == [("bo@lumen.example", "202", "101")]
    assert m.get("hubspot:contact", "bo@lumen.example") == "101"
    assert apply_merges(m, events).summary()["remapped"] == 0  # replay changes nothing


def test_hubspot_new_object_id_makes_primary_a_loser_too():
    events, _ = parse_hubspot_merges(
        {
            "subscriptionType": "contact.merge",
            "primaryObjectId": "101",
            "mergedObjectIds": ["202"],
            "newObjectId": "909",
        }
    )
    assert events == [MergeEvent("hubspot:contact", ("202", "101"), "909")]
    m = _map("hubspot:contact", {"a@x.example": "101", "b@x.example": "202"})
    apply_merges(m, events)
    assert m.keys_for("hubspot:contact", "909") == ["a@x.example", "b@x.example"]


def test_hubspot_merge_without_ids_is_reported_not_guessed():
    events, ignored = parse_hubspot_merges(
        [{"subscriptionType": "company.merge", "primaryObjectId": 7}, "junk"]
    )
    assert events == []
    assert ignored == ["0:no_merged_ids", "1:not_an_object"]


def test_company_merge_does_not_touch_contact_ids():
    events, _ = parse_hubspot_merges(
        [{"subscriptionType": "company.merge", "primaryObjectId": 1, "mergedObjectIds": [2]}]
    )
    m = _map("hubspot:contact", {"a@x.example": "2"})
    assert apply_merges(m, events).summary()["remapped"] == 0
    assert m.get("hubspot:contact", "a@x.example") == "2"


def _attio_event(record: str, dup: str, *, obj: str = "obj-people", dup_obj: str | None = None):
    return {
        "event_type": "record.merged",
        "id": {"workspace_id": "ws-1", "object_id": obj, "record_id": record},
        "duplicate_object_id": dup_obj or obj,
        "duplicate_record_id": dup,
        "actor": {"type": "workspace-member", "id": "m-1"},
    }


def test_attio_one_event_per_original_moves_both_to_new_record():
    body = {
        "webhook_id": "wh-1",
        "events": [
            {"event_type": "record.updated", "id": {"object_id": "obj-people"}},
            _attio_event("rec-new", "rec-a"),
            _attio_event("rec-new", "rec-b"),
        ],
    }
    events, ignored = parse_attio_merges(body)
    assert ignored == []
    m = _map("attio:obj-people", {"a@y.example": "rec-a", "b@y.example": "rec-b"})
    report = apply_merges(m, events)
    assert report.summary() == {"events": 2, "remapped": 2, "evicted": 0, "ignored": 0}
    assert m.keys_for("attio:obj-people", "rec-new") == ["a@y.example", "b@y.example"]


def test_attio_cross_object_or_incomplete_merge_is_ignored():
    body = {
        "events": [
            _attio_event("rec-new", "rec-a", dup_obj="obj-companies"),
            {"event_type": "record.merged", "id": {"object_id": "obj-people"}},
        ]
    }
    events, ignored = parse_attio_merges(body)
    assert events == []
    assert ignored == ["0:cross_object_merge", "1:missing_record_id"]
    assert parse_attio_merges([]) == ([], [])


def test_pipedrive_v2_change_with_merged_from_id():
    body = {
        "meta": {"action": "change", "entity": "person", "entity_id": "77", "merged_from_id": "41"},
        "data": {"id": 77, "name": "Ana Ruiz"},
    }
    events, ignored = parse_pipedrive_merge(body)
    assert (events, ignored) == ([MergeEvent("pipedrive:person", ("41",), "77")], [])
    # plain change events are not merges
    assert parse_pipedrive_merge({"meta": {"action": "change", "entity_id": "77"}}) == ([], [])


def test_pipedrive_merge_without_survivor_evicts_so_next_write_searches():
    events, _ = parse_pipedrive_merge(
        {"meta": {"entity": "person"}, "data": {"merged_from_id": 41}}
    )
    m = _map("pipedrive:person", {"ana@z.example": "41"})
    report = apply_merges(m, events)
    assert report.evicted == [("ana@z.example", "41")]
    assert m.get("pipedrive:person", "ana@z.example") is None


class _Transport:
    def __init__(self):
        self.calls = []

    def send(self, call):
        self.calls.append((call.method, call.url))
        return HttpResponse(200, {"success": True, "data": {"id": 77}}, {})


def test_pipedrive_writer_uses_remapped_id_without_a_search():
    idmap = _map("pipedrive:person", {"ana@northwind.example": "41"})
    events, _ = parse_pipedrive_merge(
        {"meta": {"entity": "person", "entity_id": "77", "merged_from_id": "41"}}
    )
    apply_merges(idmap, events)
    transport = _Transport()
    plan = plan_persons(
        [CrmRow("r1", "ana@northwind.example", {"job_title": "COO"})], allow_fixtures=True
    )
    report = execute_persons(
        plan,
        transport,
        token="t",
        budget=RateBudget(100, 1.0),
        cache=IdMapPersonCache(idmap),
        sleep=lambda _s: None,
    )
    assert transport.calls == [("PATCH", "https://api.pipedrive.com/api/v2/persons/77")]
    assert report.outcomes[0].crm_id == "77"
