from datetime import UTC, datetime

from signal_engine import (
    DEFAULT_EVENT_TYPES,
    SignalEvent,
    SignalType,
    dedupe,
    to_engagement_events,
)

SEEN = datetime(2026, 10, 21, 9, 0, tzinfo=UTC)
DOMAIN = "northwind.example"


def ev(type_, occurred_on, url, **kw):
    base = {
        "company_domain": DOMAIN,
        "type": type_,
        "status": "found",
        "observed_at": SEEN,
        "source_url": url,
        "evidence": f"Evidence for {type_}.",
        "occurred_on": occurred_on,
    }
    base.update(kw)
    return SignalEvent(**base)


def test_one_event_per_merged_signal_not_per_sighting():
    press = ev("funding", "2026-03-14", "https://news.northwind.example/a", subject="Series A")
    db = ev("funding", "2026-03", "https://db.example/rounds/1", subject="Series A")
    rescrape = ev("funding", "2026-03-14", "https://news.northwind.example/a/")
    mapping = to_engagement_events(dedupe([press, db, rescrape]))
    assert len(mapping.events) == 1
    (event,) = mapping.events
    assert event.type == "signal_funding"
    assert event.sightings == 3
    assert event.occurred_at == datetime(2026, 3, 14, tzinfo=UTC)
    assert event.source_url == "https://news.northwind.example/a"


def test_dated_at_oldest_possible_day():
    month = ev("hiring", "2026-09", "https://jobs.northwind.example/1")
    year = ev("expansion", "2025", "https://news.northwind.example/office")
    mapping = to_engagement_events(dedupe([month, year]))
    assert [e.occurred_at for e in mapping.events] == [
        datetime(2026, 9, 1, tzinfo=UTC),
        datetime(2025, 1, 1, tzinfo=UTC),
    ]


def test_date_conflict_uses_oldest_sighting():
    a = ev(
        "funding",
        "2026-03-14",
        "https://news.northwind.example/a",
        source_name="db",
        source_event_id="r1",
    )
    b = ev("funding", "2026-05-02", "https://db.example/r1", source_name="db", source_event_id="r1")
    result = dedupe([b, a])
    assert result.signals[0].date_conflict
    (event,) = to_engagement_events(result).events
    assert event.occurred_at == datetime(2026, 3, 14, tzinfo=UTC)


def test_checks_never_become_events_and_undated_is_skipped():
    undated = ev("leadership", None, "https://news.northwind.example/new-cro", subject="New CRO")
    missing = SignalEvent(
        company_domain=DOMAIN,
        type="hiring",
        status="not_found_in_sample",
        observed_at=SEEN,
        sample="careers page, 2026-10-21",
    )
    unknown = SignalEvent(company_domain=DOMAIN, type="funding", status="unknown", observed_at=SEEN)
    mapping = to_engagement_events(dedupe([undated, missing, unknown]))
    assert mapping.events == []
    (skip,) = mapping.skipped
    assert skip.signal_type is SignalType.LEADERSHIP
    assert skip.subject == "New CRO"
    assert "undated" in skip.reason


def test_account_level_no_contact_and_01_shape():
    mapping = to_engagement_events(
        dedupe([ev("site_visit", "2026-10-20", "https://ip.example/v/1")])
    )
    (event,) = mapping.events
    assert event.type == "site_visit"
    assert event.contact_id is None
    core = event.model_dump(mode="json", include={"type", "occurred_at", "contact_id"})
    assert core == {
        "type": "site_visit",
        "occurred_at": "2026-10-20T00:00:00Z",
        "contact_id": None,
    }


def test_custom_mapping_can_drop_a_type():
    events = [
        ev("tech_change", "2026-10-01", "https://stack.example/northwind/hubspot"),
        ev("funding", "2026-10-01", "https://news.northwind.example/seed"),
    ]
    types = {**DEFAULT_EVENT_TYPES, SignalType.TECH_CHANGE: None, SignalType.FUNDING: "intent"}
    mapping = to_engagement_events(dedupe(events).signals, event_types=types)
    assert [e.type for e in mapping.events] == ["intent"]
    assert [s.signal_type for s in mapping.skipped] == [SignalType.TECH_CHANGE]
    assert "not mapped" in mapping.skipped[0].reason


def test_default_types_cover_every_signal_type():
    assert set(DEFAULT_EVENT_TYPES) == set(SignalType)
    assert len(set(DEFAULT_EVENT_TYPES.values())) == len(SignalType)
