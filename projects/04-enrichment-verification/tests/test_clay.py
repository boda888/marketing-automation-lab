import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta

import pytest
from enrichment import (
    AttemptStatus,
    CreditBudget,
    FakeProvider,
    LeadRecord,
    ProviderError,
    ResultCache,
    Waterfall,
    WaterfallStatus,
)
from enrichment.clay import (
    AUTH_HEADER,
    REF_FIELD,
    CallbackStatus,
    ClayAuthError,
    ClayEmail,
    ClayPending,
    ClayQuotaError,
    FakeClayTransport,
    UrllibTransport,
    build_row,
    check_callback_secret,
    clay_ref,
    verify_clay_signature,
)

LI = "https://www.linkedin.com/in/ada-fictional"
DOMAIN = "northwind.example"  # the fake transport accepts it, the live one refuses
T0 = datetime(2026, 10, 31, 9, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now


def clay(**kw):
    transport = kw.pop("transport", None) or FakeClayTransport()
    clock = kw.pop("clock", None) or Clock()
    return ClayEmail(transport=transport, clock=clock, **kw), transport, clock


def inputs():
    return {"linkedin_url": LI, "company_domain": DOMAIN}


def pushed_ref(provider, the_inputs=None):
    with pytest.raises(ClayPending) as exc:
        provider.lookup(the_inputs or inputs())
    assert exc.value.state == "submitted"
    return build_row(the_inputs or inputs())[REF_FIELD]


def callback(ref, email="ada@northwind.example", domain=DOMAIN, **extra):
    return {REF_FIELD: ref, "work_email": email, "company_domain": domain, **extra}


# --- row shape -------------------------------------------------------------------------


def test_build_row_normalises_domain_and_adds_stable_ref():
    row = build_row({"linkedin_url": LI, "company_domain": "https://WWW.Northwind.example/about"})
    assert row["company_domain"] == "northwind.example"
    assert row[REF_FIELD] == clay_ref({"linkedin_url": LI, "company_domain": "northwind.example"})
    assert row[REF_FIELD] == build_row(inputs())[REF_FIELD]
    assert row[REF_FIELD] != build_row({"linkedin_url": LI})[REF_FIELD]


@pytest.mark.parametrize(
    "bad",
    [{"linkedin_url": "Unknown"}, {"linkedin_url": LI, "phone": "+100"}],
)
def test_build_row_refuses_placeholders_and_unmapped_fields(bad):
    with pytest.raises(ValueError):
        build_row(bad)


# --- push and callback -----------------------------------------------------------------


def test_lookup_pushes_once_then_waits_without_resubmitting():
    provider, transport, clock = clay(credits_per_row=2)
    with pytest.raises(ClayPending) as first:
        provider.lookup(inputs())
    assert first.value.state == "submitted" and first.value.cost == 2
    clock.now += timedelta(minutes=30)
    with pytest.raises(ClayPending) as second:
        provider.lookup(inputs())
    assert second.value.state == "pending" and second.value.cost == 0
    assert len(transport.rows) == 1 and provider.submitted == 1
    assert transport.rows[0] == build_row(inputs())


def test_callback_answers_the_next_lookup_at_no_extra_cost():
    provider, transport, _ = clay(status_column="email_status")
    ref = pushed_ref(provider)
    body = callback(ref, email=" Ada@Northwind.example ", email_status="Valid")
    assert provider.receive(body) is CallbackStatus.ACCEPTED
    result = provider.lookup(inputs())
    assert result.value == "ada@northwind.example"
    assert result.cost == 0 and result.provider_status == "clay:valid"
    assert len(transport.rows) == 1


def test_unknown_ref_and_malformed_callbacks_write_nothing():
    provider, _, _ = clay()
    pushed_ref(provider)
    other = clay_ref({"linkedin_url": "https://www.linkedin.com/in/someone-else"})
    assert provider.receive(callback(other)) is CallbackStatus.UNKNOWN_REF
    assert provider.receive({"work_email": "ada@northwind.example"}) is CallbackStatus.MALFORMED
    assert provider.receive([callback(other)]) is CallbackStatus.MALFORMED
    assert provider.results == {}


def test_first_callback_wins_and_forget_allows_a_deliberate_repush():
    provider, transport, _ = clay()
    ref = pushed_ref(provider)
    provider.receive(callback(ref, email="ada@northwind.example"))
    assert provider.receive(callback(ref, email="wrong@northwind.example")) is (
        CallbackStatus.DUPLICATE
    )
    assert provider.lookup(inputs()).value == "ada@northwind.example"
    provider.forget(ref)
    assert pushed_ref(provider) == ref
    assert len(transport.rows) == 2 and provider.submitted == 2


@pytest.mark.parametrize(
    ("body", "status"),
    [
        ({"company_domain": "oldjob.example"}, "clay:employer_mismatch:oldjob.example"),
        ({"work_email": None}, "clay:no_email"),
        ({"work_email": "null"}, "clay:no_email"),
        ({"work_email": "Error: out of credits"}, "clay:no_email"),
        ({"work_email": {"email": "ada@northwind.example"}}, "clay:no_email"),
    ],
)
def test_callback_without_a_usable_current_employer_email_gives_no_value(body, status):
    provider, _, _ = clay()
    ref = pushed_ref(provider)
    provider.receive({**callback(ref), **body})
    result = provider.lookup(inputs())
    assert result.value is None and result.provider_status == status


def test_no_callback_after_timeout_stays_unknown_and_is_not_resubmitted():
    provider, transport, clock = clay(callback_timeout=timedelta(hours=1))
    pushed_ref(provider)
    clock.now += timedelta(hours=1)
    with pytest.raises(ClayPending) as exc:
        provider.lookup(inputs())
    assert exc.value.state == "no_callback"
    assert len(transport.rows) == 1


# --- faults and quota ------------------------------------------------------------------


def test_push_timeout_counts_as_submitted_and_is_never_pushed_twice():
    transport = FakeClayTransport(timeouts=frozenset({LI}))
    provider, _, _ = clay(transport=transport)
    with pytest.raises(ClayPending) as exc:
        provider.lookup(inputs())
    assert exc.value.state == "push_timeout" and exc.value.cost == 1
    with pytest.raises(ClayPending) as again:
        provider.lookup(inputs())
    assert again.value.state == "pending"
    assert provider.submitted == 1 and len(transport.rows) == 1
    ref = build_row(inputs())[REF_FIELD]
    assert provider.receive(callback(ref)) is CallbackStatus.ACCEPTED


@pytest.mark.parametrize("status", [401, 403, 404])
def test_bad_token_or_deleted_webhook_stops_the_run(status):
    provider, _, _ = clay(transport=FakeClayTransport(http_errors={LI: status}))
    with pytest.raises(ClayAuthError):
        provider.lookup(inputs())
    assert provider.submitted == 0 and provider.pending == {}


def test_rate_limit_is_a_provider_error_and_not_counted():
    provider, _, _ = clay(transport=FakeClayTransport(http_errors={LI: 429}))
    with pytest.raises(ProviderError) as exc:
        provider.lookup(inputs())
    assert not isinstance(exc.value, ClayPending)
    assert provider.submitted == 0 and provider.pending == {}


def test_lifetime_cap_is_checked_before_the_post():
    provider, transport, _ = clay(submission_cap=50_000, submitted=49_999)
    pushed_ref(provider)
    with pytest.raises(ClayQuotaError, match="50000 of 50000"):
        provider.lookup({"linkedin_url": "https://www.linkedin.com/in/bo-fictional"})
    assert len(transport.rows) == 1 and provider.submitted == 50_000


# --- inside the waterfall --------------------------------------------------------------


def record(rid="lead-1", **fields):
    fields = {"linkedin_url": LI, "company_domain": DOMAIN, **fields}
    return LeadRecord(record_id=rid, fields=fields)


def test_waterfall_first_pass_is_unknown_second_pass_finds_with_provenance():
    # paid=False only to get a .example fixture past the waterfall's fixture gate offline.
    provider, transport, _ = clay(credits_per_row=3, paid=False)
    cheap = FakeProvider(
        "cheap", "email", (("linkedin_url",),), answers={}, paid=False, cost_per_call=0
    )
    wf = Waterfall("email", [cheap, provider])
    budget = CreditBudget(5)
    first = wf.run(record(), budget=budget, cache=ResultCache())
    assert first.status is WaterfallStatus.OUTCOME_UNKNOWN
    assert first.attempts[-1].status is AttemptStatus.OUTCOME_UNKNOWN
    assert first.attempts[-1].cost == 3 and budget.remaining == 2

    provider.receive(callback(build_row(inputs())[REF_FIELD]))
    second = wf.run(record(), budget=CreditBudget(5), cache=ResultCache())
    assert second.status is WaterfallStatus.FOUND and second.answered_by == "clay"
    assert second.value == "ada@northwind.example" and second.attempts[-1].cost == 0
    assert len(transport.rows) == 1


def test_waterfall_never_pushes_a_fixture_record():
    provider, transport, _ = clay()
    out = Waterfall("email", [provider]).run(record())
    assert out.attempts[0].status is AttemptStatus.SKIPPED_FIXTURE
    assert transport.rows == []


def test_waterfall_prefers_linkedin_plus_domain_and_sends_only_that_set():
    provider, transport, _ = clay(paid=False)
    Waterfall("email", [provider]).run(record(first_name="Ada", last_name="Lovelace"))
    assert set(transport.rows[0]) == {"linkedin_url", "company_domain", REF_FIELD}


# --- secrets and signatures ------------------------------------------------------------


def test_callback_secret_is_compared_case_insensitively_on_the_header_name():
    assert check_callback_secret({"X-Lab-Secret": "s3cret"}, "s3cret", "x-lab-secret")
    assert not check_callback_secret({"X-Lab-Secret": "nope"}, "s3cret", "x-lab-secret")
    assert not check_callback_secret({}, "s3cret", "x-lab-secret")
    with pytest.raises(ValueError):
        check_callback_secret({"x-lab-secret": ""}, "", "x-lab-secret")


def test_clay_signature_is_over_the_raw_body():
    raw = b'{"data": {"routine_run_id": "run-1"}}'
    good = "sha256=" + hmac.new(b"k", raw, hashlib.sha256).hexdigest()
    assert verify_clay_signature(raw, good, "k")
    reserialised = json.dumps(json.loads(raw), separators=(",", ":")).encode()
    assert not verify_clay_signature(reserialised, good, "k")
    assert not verify_clay_signature(raw, good.removeprefix("sha256="), "k")
    assert not verify_clay_signature(raw, None, "k")
    with pytest.raises(ValueError):
        verify_clay_signature(raw, good, "")


# --- live transport guard --------------------------------------------------------------


def test_live_transport_refuses_reserved_domains_before_any_request(monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("no request may be made")

    monkeypatch.setattr("urllib.request.urlopen", boom)
    transport = UrllibTransport("https://api.clay.invalid/webhook", auth_token="t")
    with pytest.raises(ValueError, match="reserved test domain"):
        transport.post(build_row(inputs()))


def test_live_transport_sends_the_auth_header(monkeypatch):
    seen = {}

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(request, timeout):
        seen["headers"] = {k.lower(): v for k, v in request.header_items()}
        seen["body"] = json.loads(request.data)
        return Resp()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    row = build_row({"linkedin_url": LI})
    UrllibTransport("https://api.clay.invalid/webhook", auth_token="t").post(row)
    assert seen["headers"][AUTH_HEADER] == "t"
    assert seen["body"] == row


# --- inside the batch pipeline ---------------------------------------------------------


def test_pipeline_reviews_a_pending_clay_row_then_sends_after_the_callback():
    from enrichment import (
        ContactCandidate,
        EmailCheck,
        EnrichmentPipeline,
        FakeResolver,
        NextAction,
    )
    from enrichment.pipeline import OUTCOME_UNKNOWN

    provider, transport, _ = clay(paid=False)  # paid=False: .example fixtures, offline only
    pipe = EnrichmentPipeline(
        waterfall=Waterfall("email", [provider]),
        resolver=FakeResolver({(DOMAIN, "MX"): ["10 mx.northwind.example."]}),
    )
    candidates = [
        ContactCandidate(
            candidate_id="c1",
            source="crm",
            observed_at=T0,
            fields={"linkedin_url": LI, "company_domain": DOMAIN, "full_name": "Ada Fictional"},
        )
    ]
    first = pipe.run(candidates, now=T0).results[0]
    assert first.findings == [OUTCOME_UNKNOWN] and first.action is NextAction.REVIEW

    provider.receive(callback(build_row(inputs())[REF_FIELD]))
    check = EmailCheck.from_zerobounce("ada@northwind.example", "valid", checked_at=T0)
    second = pipe.run(candidates, [check], now=T0 + timedelta(hours=1)).results[0]
    assert second.email == "ada@northwind.example" and second.email_source == "clay"
    assert second.action is NextAction.SEND
    assert len(transport.rows) == 1
