from datetime import UTC, datetime, timedelta

import pytest
from icp_scoring import Company, EngagementEvent, ScoreRequest, load_config, score
from icp_scoring.config import ICPConfig, RangeCriterion, TechnographicCriterion
from icp_scoring.scoring import (
    assign_tier,
    range_match,
    score_engagement,
    tech_match,
)
from pydantic import ValidationError

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def cfg() -> ICPConfig:
    return load_config()


def ideal_company(**overrides) -> Company:
    data = {
        "domain": "https://www.Northwind.example/about",
        "industry": "Software",
        "employee_count": 120,
        "annual_revenue_usd": 15_000_000,
        "country": "us",
        "technologies": ["HubSpot", "Segment", "Google Workspace"],
    }
    data.update(overrides)
    return Company(**data)


# --- range criterion ---------------------------------------------------------

RANGE = RangeCriterion(weight=1, hard_min=10, ideal_min=100, ideal_max=1000, hard_max=10000)


@pytest.mark.parametrize(
    "value,expected",
    [(100, 1.0), (500, 1.0), (1000, 1.0), (10, 0.0), (10000, 0.0), (5, 0.0), (20000, 0.0)],
)
def test_range_match_edges(value, expected):
    assert range_match(value, RANGE) == pytest.approx(expected)


def test_range_match_is_log_linear_between_bounds():
    # log10: hard_min=1, ideal_min=2 -> 31.62 sits exactly halfway
    assert range_match(10**1.5, RANGE) == pytest.approx(0.5)
    assert range_match(10**3.5, RANGE) == pytest.approx(0.5)


def test_range_match_linear_mode():
    lin = RangeCriterion(
        weight=1, hard_min=0, ideal_min=10, ideal_max=20, hard_max=30, log_scale=False
    )
    assert range_match(5, lin) == pytest.approx(0.5)
    assert range_match(25, lin) == pytest.approx(0.5)


def test_range_config_validation():
    with pytest.raises(ValidationError):
        RangeCriterion(weight=1, hard_min=10, ideal_min=5, ideal_max=20, hard_max=30)
    with pytest.raises(ValidationError):
        RangeCriterion(weight=1, hard_min=0, ideal_min=5, ideal_max=20, hard_max=30)


# --- technographics -----------------------------------------------------------


def test_tech_match_caps_and_competitor_penalty():
    c = TechnographicCriterion(
        weight=1, positive={"A": 0.7, "b": 0.6}, competitors=["Rival"], competitor_penalty=0.5
    )
    s, hits, rivals = tech_match(["a", "B"], c)
    assert s == 1.0 and hits == ["a", "b"] and rivals == []
    s, _, rivals = tech_match(["a", "rival"], c)
    assert s == pytest.approx(0.2) and rivals == ["rival"]
    s, _, _ = tech_match(["rival"], c)
    assert s == 0.0


# --- fit ----------------------------------------------------------------------


def test_ideal_account_scores_high_and_contributions_add_up(cfg):
    r = score(ScoreRequest(company=ideal_company(), as_of=NOW), cfg)
    assert r.domain == "northwind.example"
    assert r.fit_score >= 85
    assert r.missing_fields == [] and not r.needs_enrichment
    assert sum(c.points for c in r.contributions) == pytest.approx(r.fit_score, abs=0.05)
    assert sum(c.max_points for c in r.contributions) == pytest.approx(100, abs=0.05)


def test_disqualified_by_industry_and_suffix(cfg):
    r = score(ScoreRequest(company=ideal_company(industry="Gambling"), as_of=NOW), cfg)
    assert r.disqualified and r.fit_score == 0 and r.tier == 4
    assert any("Gambling" in d for d in r.disqualifiers)
    r = score(ScoreRequest(company=ideal_company(domain="dept.state.gov"), as_of=NOW), cfg)
    assert r.disqualified and r.action == "deprioritize"


def test_missing_data_gets_partial_credit_and_enrichment_flag(cfg):
    sparse = Company(domain="unknown.example", country="DE")
    r = score(ScoreRequest(company=sparse, as_of=NOW), cfg)
    assert set(r.missing_fields) == {
        "employee_count",
        "annual_revenue_usd",
        "industry",
        "technologies",
    }
    assert r.needs_enrichment
    unknown = [c for c in r.contributions if c.input is None]
    assert all(c.match == cfg.missing_value_credit for c in unknown)


def test_empty_tech_list_is_known_not_missing(cfg):
    r = score(ScoreRequest(company=ideal_company(technologies=[]), as_of=NOW), cfg)
    assert "technologies" not in r.missing_fields
    tech = next(c for c in r.contributions if c.signal == "technographics")
    assert tech.points == 0


def test_too_large_company_loses_size_points(cfg):
    small = score(ScoreRequest(company=ideal_company(), as_of=NOW), cfg)
    huge = score(
        ScoreRequest(
            company=ideal_company(employee_count=50_000, annual_revenue_usd=5e9), as_of=NOW
        ),
        cfg,
    )
    assert huge.fit_score < small.fit_score - 30


def test_scoring_is_deterministic(cfg):
    req = ScoreRequest(company=ideal_company(), as_of=NOW)
    assert score(req, cfg) == score(req, cfg)


# --- engagement -------------------------------------------------------------


def ev(kind, days_ago, contact=None):
    return EngagementEvent(
        type=kind, occurred_at=NOW - timedelta(days=days_ago), contact_id=contact
    )


def test_engagement_half_life(cfg):
    fresh = score_engagement([ev("demo_request", 0)], cfg, NOW)
    one_half_life = score_engagement([ev("demo_request", cfg.engagement.half_life_days)], cfg, NOW)
    w, sat = cfg.engagement.event_weights["demo_request"], cfg.engagement.saturation
    import math

    assert fresh == pytest.approx(100 * (1 - math.exp(-w / sat)), abs=0.01)
    assert one_half_life == pytest.approx(100 * (1 - math.exp(-w / 2 / sat)), abs=0.01)


def test_engagement_ignores_old_unknown_and_handles_naive_dates(cfg):
    assert score_engagement([ev("demo_request", 400)], cfg, NOW) == 0
    assert score_engagement([ev("made_up_event", 0)], cfg, NOW) == 0
    naive = EngagementEvent(type="pricing_page_view", occurred_at=datetime(2026, 10, 10, 12, 0))
    assert score_engagement([naive], cfg, NOW) > 0


def test_multi_stakeholder_bonus(cfg):
    single = [ev("pricing_page_view", 1, "c1"), ev("pricing_page_view", 1, "c1")]
    multi = [ev("pricing_page_view", 1, "c1"), ev("pricing_page_view", 1, "c2")]
    assert score_engagement(multi, cfg, NOW) > score_engagement(single, cfg, NOW)


# --- tiers ------------------------------------------------------------------


@pytest.mark.parametrize(
    "fit,eng,disq,tier",
    [
        (90, 80, False, 1),
        (90, 10, False, 2),
        (55, 80, False, 3),
        (55, 10, False, 4),
        (20, 99, False, 4),
        (95, 95, True, 4),
    ],
)
def test_assign_tier(cfg, fit, eng, disq, tier):
    assert assign_tier(fit, eng, disq, cfg) == tier


def test_end_to_end_tier1(cfg):
    events = [
        ev("demo_request", 2, "a"),
        ev("pricing_page_view", 1, "b"),
        ev("email_reply", 3, "a"),
    ]
    r = score(ScoreRequest(company=ideal_company(), events=events, as_of=NOW), cfg)
    assert r.tier == 1 and r.action == "ae_one_to_one"
