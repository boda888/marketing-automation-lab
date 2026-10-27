import json

import pytest
from enrichment import (
    ApolloBulkMatch,
    AttemptStatus,
    CreditBudget,
    LeadRecord,
    ProviderError,
    ResultCache,
    Waterfall,
    WaterfallStatus,
)
from enrichment.apollo import (
    MATCH_PATH,
    ApolloAuthError,
    ApolloPeopleMatch,
    FakeApolloTransport,
    UrllibTransport,
)
from enrichment.apollo_bulk import BULK_MATCH_PATH, align_matches, build_bulk_body

# paid=False only to get .example fixtures past the fixture gates offline (as in test_apollo);
# the fixture test below uses a paid provider.


def li(n):
    return f"https://www.linkedin.com/in/person-{n}"


def person(n, org="northwind.example", email=None):
    return {
        "person": {
            "linkedin_url": li(n),
            "email": email or f"p{n}@{org}",
            "email_status": "verified",
            "organization": {"primary_domain": org},
        }
    }


def rec(n, **fields):
    return LeadRecord(record_id=f"lead-{n}", fields={"linkedin_url": li(n), **fields})


def bulk(answers=None, **kw):
    transport = FakeApolloTransport(answers=answers or {}, **kw)
    return ApolloBulkMatch(ApolloPeopleMatch(transport, paid=False)), transport


def bulk_requests(transport):
    return [body for path, body in transport.requests if path == BULK_MATCH_PATH]


# ── request body ──────────────────────────────────────────────────────────────


def test_bulk_body_reuses_match_mapping_with_reveals_off_once():
    body = build_bulk_body(
        [
            {"linkedin_url": li(1)},
            {"first_name": "Ada", "last_name": "Byron", "company_domain": "www.northwind.example"},
        ]
    )
    assert body == {
        "details": [
            {"linkedin_url": li(1)},
            {"first_name": "Ada", "last_name": "Byron", "domain": "northwind.example"},
        ],
        "reveal_personal_emails": False,
        "reveal_phone_number": False,
    }


@pytest.mark.parametrize("size", [0, 11])
def test_bulk_body_takes_one_to_ten_people(size):
    with pytest.raises(ValueError, match="1..10"):
        build_bulk_body([{"linkedin_url": li(n)} for n in range(size)])


@pytest.mark.parametrize("size", [0, 11])
def test_batch_size_is_capped_at_ten(size):
    with pytest.raises(ValueError, match="batch_size"):
        ApolloBulkMatch(ApolloPeopleMatch(FakeApolloTransport()), batch_size=size)


# ── prefetch ──────────────────────────────────────────────────────────────────


def test_prefetch_sends_chunks_of_ten_and_serves_each_record_once():
    answers = {li(n): person(n) for n in range(23) if n % 2 == 0}
    provider, transport = bulk(answers)
    records = [rec(n) for n in range(23)]
    report = provider.prefetch(records)

    assert [len(b["details"]) for b in bulk_requests(transport)] == [10, 10, 3]
    assert report.requests == 3 and all(c.status == "ok" for c in report.chunks)
    assert report.credits == 12  # one credit per email returned (12 even numbers in 0..22)
    assert report.sent["lead-0"] == "apollo:verified"
    assert report.sent["lead-1"] == "apollo:no_match"

    outcomes = Waterfall("email", [provider]).run_many(records)
    assert [o.value for o in outcomes[:3]] == ["p0@northwind.example", None, "p2@northwind.example"]
    assert sum(o.cost for o in outcomes) == 12
    assert len(transport.requests) == 3  # no single People Match call was needed


def test_prefetch_skips_filled_fixture_and_unfeedable_records_without_sending():
    transport = FakeApolloTransport(answers={li(1): person(1)})
    provider = ApolloBulkMatch(ApolloPeopleMatch(transport))  # paid: fixtures never sent
    records = [
        rec(1),
        rec(2, email="known@northwind.test"),
        LeadRecord(record_id="lead-3", fields={"company_domain": "acme.example"}),
        LeadRecord(record_id="lead-4", fields={"first_name": "Ada", "linkedin_url": "Unknown"}),
    ]
    report = provider.prefetch(records)
    assert report.skipped == {
        "lead-2": "run_if false",
        "lead-3": "fixture",
        "lead-4": "missing inputs",
    }
    assert bulk_requests(transport) == [{"details": [{"linkedin_url": li(1)}], **_OFF}]


_OFF = {"reveal_personal_emails": False, "reveal_phone_number": False}


def test_same_inputs_are_sent_once_and_billed_once():
    provider, transport = bulk({li(1): person(1)})
    twin = LeadRecord(record_id="lead-1b", fields={"linkedin_url": li(1)})
    report = provider.prefetch([rec(1), twin])
    assert report.duplicates == 1
    assert len(bulk_requests(transport)[0]["details"]) == 1
    assert report.sent == {"lead-1": "apollo:verified", "lead-1b": "apollo:verified"}

    outcomes = Waterfall("email", [provider]).run_many([rec(1), twin])
    assert [o.value for o in outcomes] == ["p1@northwind.example"] * 2
    assert [o.cost for o in outcomes] == [1.0, 0.0]


def test_second_prefetch_does_not_resend_answered_inputs():
    provider, transport = bulk({li(1): person(1)})
    provider.prefetch([rec(1)])
    report = provider.prefetch([rec(1), rec(2)])
    assert [b["details"] for b in bulk_requests(transport)] == [
        [{"linkedin_url": li(1)}],
        [{"linkedin_url": li(2)}],
    ]
    assert report.duplicates == 1 and report.sent["lead-1"] == "apollo:verified"


def test_previous_employer_match_in_bulk_is_no_answer():
    record = LeadRecord(
        record_id="lead-9",
        fields={"first_name": "Ada", "last_name": "Byron", "company_domain": "northwind.example"},
    )
    provider, _ = bulk({"northwind.example": person(9, org="oldco.example")})
    report = provider.prefetch([record])
    assert report.sent["lead-9"] == "apollo:employer_mismatch:oldco.example"
    assert report.credits == 0
    out = Waterfall("email", [provider]).run(record)
    assert out.status is WaterfallStatus.NOT_FOUND


# ── faults: never guess which answer belongs to whom ──────────────────────────


@pytest.mark.parametrize(
    "payload, reason",
    [
        ({"matches": [person(1)["person"]]}, "1 matches for 2 details"),
        ({"matches": [person(2)["person"], person(1)["person"]]}, "another LinkedIn profile"),
        ({"people": []}, "no 'matches'"),
        ({"matches": ["p1@northwind.example", None]}, "not an object"),
    ],
)
def test_unalignable_answer_is_an_unknown_outcome_for_the_whole_chunk(payload, reason):
    provider, _ = bulk(bulk_bodies=[payload])
    report = provider.prefetch([rec(1), rec(2)])
    assert report.chunks[0].status == "outcome_unknown" and reason in report.chunks[0].detail
    assert report.sent == {"lead-1": "outcome_unknown", "lead-2": "outcome_unknown"}

    budget = CreditBudget(5)
    out = Waterfall("email", [provider]).run(rec(1), budget=budget)
    assert out.status is WaterfallStatus.OUTCOME_UNKNOWN and out.value is None
    assert budget.spent == 1.0  # may have been billed: booked at max_cost


def test_align_accepts_nulls_and_trailing_slash_or_case_differences():
    details = [{"linkedin_url": li(1)}, {"linkedin_url": li(2)}]
    got = {"linkedin_url": li(1).upper() + "/"}
    assert align_matches(details, {"matches": [got, None]}) == [got, None]


def test_timeout_marks_the_chunk_unknown_and_only_that_chunk():
    answers = {li(n): person(n) for n in range(12)}
    provider, _ = bulk(answers, timeouts=frozenset({li(11)}))
    report = provider.prefetch([rec(n) for n in range(12)], run_if=None)
    assert [c.status for c in report.chunks] == ["ok", "outcome_unknown"]
    assert report.sent["lead-0"] == "apollo:verified"
    assert report.sent["lead-10"] == report.sent["lead-11"] == "outcome_unknown"
    out = Waterfall("email", [provider]).run(rec(10))
    assert out.attempts[0].status is AttemptStatus.OUTCOME_UNKNOWN


def test_rate_limit_is_an_error_and_the_waterfall_moves_on():
    provider, _ = bulk(http_errors={li(1): 429})
    report = provider.prefetch([rec(1)])
    assert report.chunks[0].status == "error" and report.sent["lead-1"] == "error"
    out = Waterfall("email", [provider]).run(rec(1))
    assert out.attempts[0].status is AttemptStatus.ERROR
    assert out.status is WaterfallStatus.NOT_FOUND


@pytest.mark.parametrize("status", [401, 403])
def test_bad_key_stops_the_prefetch(status):
    provider, _ = bulk(http_errors={li(1): status})
    with pytest.raises(ApolloAuthError):
        provider.prefetch([rec(1)])


# ── lookup without a prefetch ─────────────────────────────────────────────────


def test_not_prefetched_falls_back_to_one_match_call():
    provider, transport = bulk({li(5): person(5)})
    out = Waterfall("email", [provider]).run(rec(5))
    assert out.value == "p5@northwind.example"
    assert [path for path, _ in transport.requests] == [MATCH_PATH]


def test_fallback_off_never_calls_apollo_per_record():
    transport = FakeApolloTransport(answers={li(5): person(5)})
    provider = ApolloBulkMatch(ApolloPeopleMatch(transport), fallback=False)
    with pytest.raises(ProviderError, match="not prefetched"):
        provider.lookup({"linkedin_url": li(5)})
    assert transport.requests == []


def test_works_behind_a_cheaper_provider_and_with_the_cache():
    provider, _ = bulk({li(1): person(1)})
    provider.prefetch([rec(1)])
    cache = ResultCache()
    wf = Waterfall("email", [provider])
    first, again = wf.run(rec(1), cache=cache), wf.run(rec(1), cache=cache)
    assert first.cost == 1.0 and again.attempts[0].cached and again.cost == 0


# ── live transport guard ──────────────────────────────────────────────────────


def test_urllib_transport_checks_every_detail_for_test_domains(monkeypatch):
    def no_network(request, timeout):
        raise AssertionError("request sent")

    monkeypatch.setattr("urllib.request.urlopen", no_network)
    body = {"details": [{"linkedin_url": li(1)}, {"domain": "northwind.example"}]}
    with pytest.raises(ValueError, match="reserved test domain"):
        UrllibTransport(api_key="test-key").post(BULK_MATCH_PATH, body)


def test_urllib_transport_posts_bulk_body(monkeypatch):
    seen = {}

    class Resp:
        def read(self):
            return json.dumps({"matches": [None]}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(request, timeout):
        seen.update(url=request.full_url, body=json.loads(request.data))
        return Resp()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    provider = ApolloBulkMatch(ApolloPeopleMatch(UrllibTransport(api_key="test-key")))
    report = provider.prefetch([rec(1)])
    assert seen["url"] == "https://api.apollo.io/api/v1/people/bulk_match"
    assert seen["body"]["details"] == [{"linkedin_url": li(1)}]
    assert report.sent == {"lead-1": "apollo:no_match"}
