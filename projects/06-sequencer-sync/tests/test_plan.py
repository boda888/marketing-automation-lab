from sequencer import (
    Campaign,
    DedupeScope,
    InMemoryPushLog,
    SequencerLead,
    SkipReason,
    SuppressionList,
    SuppressionReason,
    idempotency_key,
    plan_push,
)

CAMPAIGN = Campaign(campaign_id="101", name="Q4 Logistics DE")


def lead(i, email, **kw):
    return SequencerLead(lead_id=f"l{i}", email=email, **kw)


def reasons(plan):
    return {s.lead_id: s.reason for s in plan.skipped}


def test_dedupe_scope_is_required():
    import inspect

    param = inspect.signature(plan_push).parameters["dedupe_scope"]
    assert param.default is inspect.Parameter.empty


def test_every_skip_reason_once():
    suppression = SuppressionList()
    suppression.add_email("gone@fabrikam.example", SuppressionReason.UNSUBSCRIBED)
    log = InMemoryPushLog()
    log.commit(idempotency_key("smartlead", "101", "done@acme.example"))
    log.reserve(idempotency_key("smartlead", "101", "pending@acme.example"))
    leads = [
        lead(1, "Unknown"),
        lead(2, "gone@fabrikam.example"),
        lead(3, "ok@acme.example", custom_variables={"first_line": "Saw the Leipzig hub"}),
        lead(4, "OK@acme.example", custom_variables={"first_line": "dupe"}),
        lead(5, "done@acme.example"),
        lead(6, "pending@acme.example"),
        lead(7, "here@acme.example"),
        lead(8, "other@acme.example"),
        lead(9, "list@acme.example"),
        lead(10, "blank@acme.example", custom_variables={"first_line": ""}),
    ]
    plan = plan_push(
        "smartlead",
        CAMPAIGN,
        leads,
        dedupe_scope=DedupeScope.WORKSPACE,
        suppression=suppression,
        push_log=log,
        required_variables={"first_line"},
        in_campaign={"here@acme.example"},
        in_other_campaigns={"other@acme.example"},
        in_workspace={"list@acme.example"},
        allow_fixtures=True,
    )
    assert [p.lead.lead_id for p in plan.to_send] == ["l3"]
    assert reasons(plan) == {
        "l1": SkipReason.MISSING_EMAIL,
        "l2": SkipReason.SUPPRESSED,
        "l4": SkipReason.DUPLICATE_IN_BATCH,
        "l5": SkipReason.ALREADY_PUSHED,
        "l6": SkipReason.PUSH_PENDING,
        "l7": SkipReason.ALREADY_IN_CAMPAIGN,
        "l8": SkipReason.IN_OTHER_CAMPAIGN,
        "l9": SkipReason.IN_WORKSPACE,
        "l10": SkipReason.MISSING_VARIABLES,
    }
    detail = {s.lead_id: s.detail for s in plan.skipped}
    assert detail["l2"] == "unsubscribed: gone@fabrikam.example"
    assert detail["l4"] == "same email as l3"
    assert plan.to_send[0].key == idempotency_key("smartlead", "101", "ok@acme.example")
    summary = plan.summary()
    assert summary["input"] == 10 and summary["to_send"] == 1
    assert summary["skipped"]["suppressed"] == 1


def test_campaign_scope_ignores_other_campaigns_but_never_suppression():
    suppression = SuppressionList()
    suppression.add_domain("contoso.example", SuppressionReason.DO_NOT_CONTACT)
    plan = plan_push(
        "instantly",
        CAMPAIGN,
        [lead(1, "a@acme.example"), lead(2, "b@contoso.example")],
        dedupe_scope=DedupeScope.CAMPAIGN,
        suppression=suppression,
        in_other_campaigns={"a@acme.example"},
        in_workspace={"a@acme.example"},
        allow_fixtures=True,
    )
    assert [p.lead.lead_id for p in plan.to_send] == ["l1"]
    assert reasons(plan) == {"l2": SkipReason.SUPPRESSED}


def test_all_campaigns_scope_does_not_apply_workspace_lists():
    plan = plan_push(
        "lemlist",
        CAMPAIGN,
        [lead(1, "a@acme.example")],
        dedupe_scope=DedupeScope.ALL_CAMPAIGNS,
        in_workspace={"a@acme.example"},
        allow_fixtures=True,
    )
    assert len(plan.to_send) == 1


def test_fixtures_blocked_unless_allowed():
    plan = plan_push(
        "smartlead",
        CAMPAIGN,
        [lead(1, "a@acme.example"), lead(2, "b@acme.io")],
        dedupe_scope=DedupeScope.CAMPAIGN,
    )
    assert reasons(plan) == {"l1": SkipReason.FIXTURE}
    assert [p.lead.lead_id for p in plan.to_send] == ["l2"]


def test_unknown_membership_does_not_skip():
    plan = plan_push(
        "smartlead",
        CAMPAIGN,
        [lead(1, "a@acme.example")],
        dedupe_scope=DedupeScope.WORKSPACE,
        allow_fixtures=True,
    )
    assert len(plan.to_send) == 1 and not plan.skipped


def test_push_log_reserve_commit_release():
    log = InMemoryPushLog()
    assert log.reserve("k")
    assert not log.reserve("k")
    log.release("k")
    assert log.state("k") is None
    log.reserve("k")
    log.commit("k")
    log.release("k")
    assert log.state("k").value == "committed"
