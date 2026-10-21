import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from signal_engine import __version__
from signal_engine.api import MAX_EVENTS, create_app

EXAMPLE = json.loads(
    (Path(__file__).resolve().parents[1] / "examples" / "prioritise_request.json").read_text()
)


@pytest.fixture(scope="module")
def client():
    return TestClient(create_app())


@pytest.fixture(scope="module")
def body(client):
    resp = client.post("/v1/signals/prioritise", json=EXAMPLE)
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_healthz(client):
    assert client.get("/healthz").json() == {"status": "ok", "version": __version__}


def test_example_covers_every_tier(body):
    tiers = {a["company_domain"]: a["tier"] for a in body["accounts"]}
    assert tiers == {
        "northwind.example": "Tier 1",
        "contoso-labs.example": "Review",
        "tailspin.example": "Tier 3",
        "adatum.example": "Hold",
        "fabrikam.example": "Tier 2",
    }
    assert body["tier_counts"] == {"Tier 1": 1, "Tier 2": 1, "Tier 3": 1, "Hold": 1, "Review": 1}
    assert body["as_of"] == "2026-10-21"


def test_round_from_two_sources_counts_once(body):
    assert body["dedupe"] == {"input_found": 7, "duplicates_removed": 1, "merged_signals": 6}
    funding = [
        e
        for e in body["engagement_events"]
        if e["company_domain"] == "northwind.example" and e["type"] == "signal_funding"
    ]
    assert len(funding) == 1
    assert funding[0]["sightings"] == 2
    assert funding[0]["occurred_at"] == "2026-09-14T00:00:00Z"
    assert all(e["contact_id"] is None for e in body["engagement_events"])


def test_undated_signal_goes_to_review_and_is_not_dated_now(body):
    contoso = next(a for a in body["accounts"] if a["company_domain"] == "contoso-labs.example")
    assert contoso["upgrade_possible"] is True
    assert contoso["best_case_tier"] == "Tier 2"
    assert contoso["best_signal"] is None
    (skipped,) = body["skipped_for_engagement"]
    assert skipped["company_domain"] == "contoso-labs.example"
    assert "undated" in skipped["reason"]


def test_headcount_series_with_missing_month(body):
    (trend,) = body["headcount"]
    assert trend["state"] == "strong_growth"
    twelve = next(g for g in trend["growth"] if g["months"] == 12)
    assert twelve["state"] == "unknown"
    assert twelve["pct"] is None
    fabrikam = next(a for a in body["accounts"] if a["company_domain"] == "fabrikam.example")
    # Strong headcount growth settles Tier 2; the unknown funding check could lift it.
    assert fabrikam["tier"] == "Tier 2"
    assert fabrikam["best_case_tier"] == "Tier 1"


def test_invalid_event_is_rejected_not_guessed(client):
    bad = {
        "events": [
            {
                "company_domain": "northwind.example",
                "type": "funding",
                "status": "found",
                "observed_at": "2026-10-20T08:00:00Z",
                "source_url": "https://northwind.example/",
                "evidence": "Homepage says they are growing.",
            }
        ]
    }
    assert client.post("/v1/signals/prioritise", json=bad).status_code == 422


def test_empty_and_oversized_requests(client):
    assert client.post("/v1/signals/prioritise", json={}).status_code == 422
    one = EXAMPLE["events"][-1]
    resp = client.post("/v1/signals/prioritise", json={"events": [one] * (MAX_EVENTS + 1)})
    assert resp.status_code == 413


def test_as_of_defaults_to_today(client):
    resp = client.post("/v1/signals/prioritise", json={"events": [EXAMPLE["events"][-1]]})
    assert resp.status_code == 200
    (account,) = resp.json()["accounts"]
    assert account["tier"] == "Review"


def test_versions_agree(client):
    import tomllib

    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    project = tomllib.loads(pyproject.read_text())["project"]
    assert project["version"] == __version__
    assert client.get("/openapi.json").json()["info"]["version"] == project["version"]
