import pytest
from enrichment import (
    AttemptStatus,
    CreditBudget,
    FakeProvider,
    LeadRecord,
    ResultCache,
    Waterfall,
    WaterfallStatus,
    coverage,
)

LI = "https://www.linkedin.com/in/ada-fictional"
LI2 = "https://www.linkedin.com/in/grace-fictional"
BY_LINKEDIN = (("linkedin_url",),)


def rec(rid="lead-1", **fields):
    return LeadRecord(record_id=rid, fields=fields)


def provider(name, answers=None, output_field="email", **kw):
    return FakeProvider(name, output_field, BY_LINKEDIN, answers=answers or {}, **kw)


# ── budget ────────────────────────────────────────────────────────────────────


def test_budget_rejects_negative_values():
    with pytest.raises(ValueError):
        CreditBudget(-1)
    with pytest.raises(ValueError):
        CreditBudget(1).charge(-0.5)


def test_provider_over_budget_is_skipped_not_called():
    cheap = provider("cheap", cost_per_call=1)
    pricey = provider("pricey", {LI: "ada@northwind.example"}, cost_per_call=5)
    budget = CreditBudget(3)
    out = Waterfall("email", [cheap, pricey]).run(rec(linkedin_url=LI), budget=budget)
    assert pricey.calls == []
    assert [a.status for a in out.attempts] == [
        AttemptStatus.NOT_FOUND,
        AttemptStatus.SKIPPED_BUDGET,
    ]
    assert out.attempts[1].detail == "max_cost 5 > remaining 2"
    assert out.status is WaterfallStatus.BUDGET_SPENT
    assert budget.spent == 1 and out.calls == 1


def test_cheaper_provider_after_a_skipped_one_still_runs():
    pricey = provider("pricey", cost_per_call=5)
    free = provider("free", {LI: "ada@northwind.example"}, cost_per_call=0)
    out = Waterfall("email", [pricey, free]).run(rec(linkedin_url=LI), budget=CreditBudget(1))
    assert out.status is WaterfallStatus.FOUND and out.answered_by == "free"
    assert out.attempts[0].status is AttemptStatus.SKIPPED_BUDGET


def test_exact_fit_is_affordable():
    p = provider("p", {LI: "ada@northwind.example"}, cost_per_call=0.1)
    budget = CreditBudget(0.3)
    budget.charge(0.2)
    out = Waterfall("email", [p]).run(rec(linkedin_url=LI), budget=budget)
    assert out.status is WaterfallStatus.FOUND


def test_one_budget_spans_waterfalls_on_the_same_record():
    email = provider("mail", {LI: "ada@northwind.example"}, cost_per_call=2)
    phone = provider("phone", {LI: "+1 555 0100"}, output_field="phone", cost_per_call=10)
    budget = CreditBudget(5)
    record = rec(linkedin_url=LI)
    assert Waterfall("email", [email]).run(record, budget=budget).status is WaterfallStatus.FOUND
    out = Waterfall("phone", [phone]).run(record, budget=budget)
    assert out.status is WaterfallStatus.BUDGET_SPENT and phone.calls == []
    assert budget.remaining == 3


def test_charged_only_on_hit_provider_keeps_budget_on_a_miss():
    p = provider("onhit", cost_per_call=1, charge_on_miss=False)
    budget = CreditBudget(1)
    Waterfall("email", [p]).run(rec(linkedin_url=LI), budget=budget)
    assert budget.spent == 0


def test_run_many_gives_each_record_its_own_budget():
    p = provider("p", {LI: "ada@northwind.example", LI2: "grace@contoso.example"}, cost_per_call=2)
    outs = Waterfall("email", [p]).run_many(
        [rec("a", linkedin_url=LI), rec("b", linkedin_url=LI2)], budget_per_record=2
    )
    assert [o.status for o in outs] == [WaterfallStatus.FOUND, WaterfallStatus.FOUND]


def test_without_budget_nothing_is_skipped():
    p = provider("p", cost_per_call=1000)
    out = Waterfall("email", [p]).run(rec(linkedin_url=LI))
    assert out.status is WaterfallStatus.NOT_FOUND and len(p.calls) == 1


# ── cache ─────────────────────────────────────────────────────────────────────


def test_same_inputs_are_not_paid_twice():
    p = provider("p", {LI: "ada@northwind.example"}, cost_per_call=2)
    cache = ResultCache()
    wf = Waterfall("email", [p])
    first = wf.run(rec("a", linkedin_url=LI), cache=cache)
    second = wf.run(rec("b", linkedin_url=LI), cache=cache)
    assert len(p.calls) == 1 and cache.hits == 1
    assert (second.value, second.answered_by) == ("ada@northwind.example", "p")
    assert first.cost == 2 and second.cost == 0
    assert second.attempts[0].cached and second.calls == 0


def test_cached_miss_is_replayed_and_waterfall_moves_on():
    miss = provider("miss", cost_per_call=1)
    hit = provider("hit", {LI: "ada@northwind.example"}, cost_per_call=3)
    cache = ResultCache()
    wf = Waterfall("email", [miss, hit])
    wf.run(rec("a", linkedin_url=LI), cache=cache)
    out = wf.run(rec("b", linkedin_url=LI), cache=cache)
    assert len(miss.calls) == 1 and len(hit.calls) == 1
    assert [a.status for a in out.attempts] == [AttemptStatus.NOT_FOUND, AttemptStatus.FOUND]
    assert all(a.cached for a in out.attempts) and out.cost == 0


def test_cache_replay_ignores_budget():
    p = provider("p", {LI: "ada@northwind.example"}, cost_per_call=5)
    cache = ResultCache()
    wf = Waterfall("email", [p])
    wf.run(rec("a", linkedin_url=LI), cache=cache)
    out = wf.run(rec("b", linkedin_url=LI), cache=cache, budget=CreditBudget(0))
    assert out.status is WaterfallStatus.FOUND


def test_cached_answer_still_goes_through_this_waterfalls_accept_rule():
    p = provider("p", {LI: "info@northwind.example"})
    cache = ResultCache()
    Waterfall("email", [p]).run(rec("a", linkedin_url=LI), cache=cache)
    strict = Waterfall("email", [p], accept=lambda v: not v.startswith("info@"))
    out = strict.run(rec("b", linkedin_url=LI), cache=cache)
    assert out.attempts[0].status is AttemptStatus.REJECTED and len(p.calls) == 1


def test_cache_key_separates_provider_field_and_inputs():
    k = ResultCache.key
    assert k("p", "email", {"a": "1"}) != k("q", "email", {"a": "1"})
    assert k("p", "email", {"a": "1"}) != k("p", "phone", {"a": "1"})
    assert k("p", "email", {"a": "1", "b": "2"}) == k("p", "email", {"b": "2", "a": "1"})


def test_errors_are_not_cached():
    p = provider("p", errors=frozenset({LI}))
    cache = ResultCache()
    wf = Waterfall("email", [p])
    wf.run(rec("a", linkedin_url=LI), cache=cache)
    wf.run(rec("b", linkedin_url=LI), cache=cache)
    assert len(p.calls) == 2 and len(cache) == 0


# ── timeouts ──────────────────────────────────────────────────────────────────


def test_timeout_is_outcome_unknown_not_a_free_miss():
    p = provider("p", timeouts=frozenset({LI}), cost_per_call=4, paid=True)
    cache = ResultCache()
    budget = CreditBudget(10)
    out = Waterfall("email", [p]).run(rec(linkedin_url=LI), budget=budget, cache=cache)
    assert out.status is WaterfallStatus.OUTCOME_UNKNOWN
    attempt = out.attempts[0]
    assert attempt.status is AttemptStatus.OUTCOME_UNKNOWN
    assert "up to 4 credits may have been charged" in attempt.detail
    assert budget.spent == 4  # booked pessimistically
    assert len(cache) == 0  # never replayed as a known miss


def test_found_after_a_timeout_wins():
    slow = provider("slow", timeouts=frozenset({LI}))
    ok = provider("ok", {LI: "ada@northwind.example"})
    out = Waterfall("email", [slow, ok]).run(rec(linkedin_url=LI))
    assert out.status is WaterfallStatus.FOUND


def test_unknown_outranks_budget_spent():
    slow = provider("slow", timeouts=frozenset({LI}), cost_per_call=1)
    pricey = provider("pricey", cost_per_call=50)
    out = Waterfall("email", [slow, pricey]).run(rec(linkedin_url=LI), budget=CreditBudget(5))
    assert out.status is WaterfallStatus.OUTCOME_UNKNOWN


def test_coverage_counts_cached_and_unknown_apart_from_calls():
    p = provider("p", {LI: "ada@northwind.example"}, timeouts=frozenset({LI2}), cost_per_call=1)
    cache = ResultCache()
    wf = Waterfall("email", [p])
    outs = [
        wf.run(rec("a", linkedin_url=LI), cache=cache),
        wf.run(rec("b", linkedin_url=LI), cache=cache),
        wf.run(rec("c", linkedin_url=LI2), cache=cache),
    ]
    report = coverage(outs)
    (row,) = report.providers
    assert (row.calls, row.cached, row.unknown, row.answered.numerator) == (2, 1, 1, 1)
    assert report.status_counts["found"] == 2
    assert report.status_counts["outcome_unknown"] == 1
