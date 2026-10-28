from datetime import UTC, datetime, timedelta

import pytest
from enrichment import (
    AttemptStatus,
    ContactCandidate,
    EmailCheck,
    EmailStatus,
    EnrichmentPipeline,
    FakeProvider,
    FakeResolver,
    NextAction,
    ResultCache,
    Waterfall,
    WaterfallStatus,
    email_matches_company,
)
from enrichment.pipeline import EMAIL_DOMAIN_MISMATCH, EMAIL_SHARED, OUTCOME_UNKNOWN

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)

RECORDS = {
    ("northwind.example", "MX"): ["10 mx.northwind.example."],
    ("contoso.example", "MX"): ["10 mx.contoso.example."],
    ("oldco.example", "MX"): ["10 mx.oldco.example."],
    ("parked.example", "MX"): ["0 ."],
}

ANSWERS = {
    "https://www.linkedin.com/in/ada": "ada@northwind.example",
    "https://www.linkedin.com/in/cara": "cara@oldco.example",
    "https://www.linkedin.com/in/finn": "team@northwind.example",
    "https://www.linkedin.com/in/gail": "team@northwind.example",
    "https://www.linkedin.com/in/hugo": "hugo@contoso.example",
}


def li(slug):
    return f"https://www.linkedin.com/in/{slug}"


def cand(cid, source="apollo", days_ago=0, **fields):
    return ContactCandidate(
        candidate_id=cid,
        source=source,
        observed_at=NOW - timedelta(days=days_ago),
        fields=fields,
    )


def zb(email, status, days_ago=1):
    return EmailCheck.from_zerobounce(email, status, checked_at=NOW - timedelta(days=days_ago))


def provider(**kw):
    return FakeProvider(
        name=kw.pop("name", "finder"),
        output_field="email",
        input_sets=(("linkedin_url",),),
        answers=kw.pop("answers", ANSWERS),
        **kw,
    )


def pipeline(*providers, **kw):
    return EnrichmentPipeline(
        waterfall=Waterfall("email", providers or (provider(),)),
        resolver=FakeResolver(RECORDS),
        **kw,
    )


def by_person(batch):
    return {r.person.person_id: r for r in batch.results}


def test_one_pass_routes_each_person_to_one_action():
    candidates = [
        cand("ada-1", linkedin_url=li("ada"), company_domain="northwind.example"),
        cand("ada-2", source="csv", linkedin_url=li("ada"), full_name="Ada Byron"),
        cand("bob", email="bob@northwind.example", company_domain="northwind.example"),
        cand("dan", full_name="Dan Doe", company_domain="northwind.example"),
        cand("eve", email="eve@parked.example"),
    ]
    batch = pipeline().run(candidates, [zb("ada@northwind.example", "valid")], now=NOW)

    assert (batch.candidates_in, batch.people) == (5, 4)
    r = by_person(batch)
    assert r["ada-1"].action is NextAction.SEND
    assert r["ada-1"].email_source == "finder"
    assert r["ada-1"].assessment.status is EmailStatus.VERIFIED
    assert r["ada-1"].person.candidate_ids == ["ada-1", "ada-2"]
    # An existing address is not re-bought, and is not "valid" without a check.
    assert r["bob"].waterfall.status is WaterfallStatus.NOT_RUN
    assert (r["bob"].action, r["bob"].email_source) == (NextAction.VERIFY, "apollo")
    assert r["bob"].assessment.status is EmailStatus.CHECKED  # the precheck alone: unknown
    assert r["dan"].action is NextAction.NO_EMAIL
    assert (r["dan"].precheck, r["dan"].assessment) == (None, None)
    assert r["eve"].action is NextAction.SUPPRESS
    assert "null_mx" in r["eve"].precheck.codes()

    assert batch.actions == {
        NextAction.SEND: 1,
        NextAction.VERIFY: 1,
        NextAction.REVIEW: 0,
        NextAction.SUPPRESS: 1,
        NextAction.NO_EMAIL: 1,
    }
    assert batch.ladder.sendable.numerator == 1
    assert batch.ladder.sendable.denominator == 3
    assert batch.coverage.found_of_run.numerator == 1
    assert batch.cost == 1.0  # only ada was looked up: dan has no input, bob / eve not run


def test_found_email_on_another_domain_goes_to_review():
    candidates = [cand("cara", linkedin_url=li("cara"), company_domain="contoso.example")]
    result = pipeline().run(candidates, [zb("cara@oldco.example", "valid")], now=NOW).results[0]
    assert result.assessment.status is EmailStatus.VERIFIED
    assert result.findings == [EMAIL_DOMAIN_MISMATCH]
    assert result.action is NextAction.REVIEW


def test_old_employer_email_from_an_older_source_is_caught_after_merge():
    candidates = [
        cand("old", days_ago=40, linkedin_url=li("ivy"), company_domain="oldco.example"),
        cand("old-mail", days_ago=40, linkedin_url=li("ivy"), email="ivy@oldco.example"),
        cand("new", source="linkedin", linkedin_url=li("ivy"), company_domain="northwind.example"),
    ]
    result = pipeline().run(candidates, now=NOW).results[0]
    assert result.person.fields["company_domain"] == "northwind.example"
    assert result.email == "ivy@oldco.example"
    assert result.waterfall.status is WaterfallStatus.NOT_RUN
    assert EMAIL_DOMAIN_MISMATCH in result.findings
    assert result.action is NextAction.REVIEW


def test_mismatch_from_dedupe_is_rechecked_not_copied():
    # dedupe flags nothing (no email yet); the pipeline judges the address actually used.
    candidates = [cand("hugo", linkedin_url=li("hugo"), company_domain="www.contoso.example")]
    result = pipeline().run(candidates, now=NOW).results[0]
    assert result.findings == []
    assert result.action is NextAction.VERIFY


def test_one_address_for_two_people_sends_both_to_review_and_counts_once():
    candidates = [
        cand("finn", linkedin_url=li("finn"), company_domain="northwind.example"),
        cand("gail", linkedin_url=li("gail"), company_domain="northwind.example"),
    ]
    batch = pipeline().run(candidates, now=NOW)
    assert [r.action for r in batch.results] == [NextAction.REVIEW, NextAction.REVIEW]
    assert all(EMAIL_SHARED in r.findings for r in batch.results)
    assert batch.ladder.sendable.denominator == 1


def test_invalid_address_is_suppressed_even_with_findings():
    candidates = [cand("eve", email="eve@parked.example", company_domain="contoso.example")]
    result = pipeline().run(candidates, now=NOW).results[0]
    assert EMAIL_DOMAIN_MISMATCH in result.findings
    assert result.action is NextAction.SUPPRESS


def test_timed_out_paid_call_is_reviewed_not_reported_as_no_email():
    slow = provider(timeouts=frozenset({li("jo")}))
    result = pipeline(slow).run([cand("jo", linkedin_url=li("jo"))], now=NOW).results[0]
    assert result.waterfall.status is WaterfallStatus.OUTCOME_UNKNOWN
    assert result.findings == [OUTCOME_UNKNOWN]
    assert result.action is NextAction.REVIEW


def test_catch_all_learned_from_a_colleagues_check_blocks_send():
    candidates = [cand("hugo", linkedin_url=li("hugo"), company_domain="contoso.example")]
    checks = [
        zb("hugo@contoso.example", "valid", days_ago=20),
        zb("someone@contoso.example", "catch-all", days_ago=2),
    ]
    result = pipeline().run(candidates, checks, now=NOW).results[0]
    assert "catch_all_domain" in result.precheck.codes()
    assert result.assessment.status is EmailStatus.CHECKED
    assert result.action is NextAction.VERIFY


def test_paid_provider_never_receives_fixture_people():
    paid = provider(paid=True)
    people = [cand("ada", linkedin_url=li("ada"), company_domain="northwind.example")]
    batch = pipeline(paid).run(people, now=NOW)
    assert paid.calls == []
    assert batch.results[0].waterfall.attempts[0].status is AttemptStatus.SKIPPED_FIXTURE
    assert batch.results[0].action is NextAction.NO_EMAIL
    assert batch.cost == 0


def test_budget_per_person_skips_a_provider_that_no_longer_fits():
    dear = provider(name="dear", cost_per_call=5.0)
    batch = pipeline(dear, budget_per_record=2.0).run(
        [cand("ada", linkedin_url=li("ada"))], now=NOW
    )
    assert batch.results[0].waterfall.status is WaterfallStatus.BUDGET_SPENT
    assert dear.calls == []


def test_shared_cache_replays_across_batches_only_when_passed():
    candidates = [cand("ada", linkedin_url=li("ada"))]
    fresh = provider()
    pipe = pipeline(fresh)
    pipe.run(candidates, now=NOW)
    pipe.run(candidates, now=NOW)
    assert len(fresh.calls) == 2  # a new cache per batch by default

    shared = provider()
    pipe = pipeline(shared, cache=ResultCache())
    pipe.run(candidates, now=NOW)
    second = pipe.run(candidates, now=NOW)
    assert len(shared.calls) == 1
    assert second.results[0].waterfall.attempts[0].cached
    assert second.cost == 0


def test_dns_is_queried_once_per_domain_per_batch():
    resolver = FakeResolver(RECORDS)
    pipe = EnrichmentPipeline(waterfall=Waterfall("email", (provider(),)), resolver=resolver)
    candidates = [cand(f"p{i}", email=f"p{i}@northwind.example") for i in range(5)]
    pipe.run(candidates, now=NOW)
    assert resolver.queries.count(("northwind.example", "MX")) == 1


def test_wiring_errors_fail_early():
    phone = FakeProvider(name="ph", output_field="phone", input_sets=(("linkedin_url",),))
    with pytest.raises(ValueError, match="email"):
        EnrichmentPipeline(waterfall=Waterfall("phone", (phone,)), resolver=FakeResolver({}))
    with pytest.raises(ValueError, match="timezone"):
        pipeline().run([cand("a", email="a@northwind.example")], now=datetime(2026, 10, 28))
    with pytest.raises(ValueError, match="duplicate candidate_id"):
        pipeline().run([cand("a"), cand("a")], now=NOW)


@pytest.mark.parametrize(
    ("email", "company", "expected"),
    [
        ("a@northwind.example", "https://www.northwind.example/", True),
        ("a@eu.northwind.example", "northwind.example", True),
        ("a@notnorthwind.example", "northwind.example", False),
        ("a@northwind.example", None, None),
        ("Unknown", "northwind.example", None),
    ],
)
def test_email_matches_company(email, company, expected):
    assert email_matches_company(email, company) is expected
