from enrichment import EnrichmentPipeline, FakeProvider, FakeResolver, Waterfall
from enrichment.api import MAX_BATCH, create_app
from fastapi.testclient import TestClient

NOW = "2026-10-28T12:00:00Z"


def client():
    finder = FakeProvider(
        name="finder",
        output_field="email",
        input_sets=(("linkedin_url",),),
        answers={"https://www.linkedin.com/in/ada": "ada@northwind.example"},
    )
    pipe = EnrichmentPipeline(
        waterfall=Waterfall("email", (finder,)),
        resolver=FakeResolver({("northwind.example", "MX"): ["10 mx.northwind.example."]}),
    )
    return TestClient(create_app(pipe))


def candidate(cid, **fields):
    return {"candidate_id": cid, "source": "apollo", "observed_at": NOW, "fields": fields}


def test_healthz_names_the_wired_providers():
    body = client().get("/healthz").json()
    assert body["status"] == "ok"
    assert body["email_providers"] == ["finder"]


def test_batch_returns_one_action_per_person():
    payload = {
        "candidates": [
            candidate(
                "ada",
                linkedin_url="https://www.linkedin.com/in/ada",
                company_domain="northwind.example",
            ),
            candidate("dan", full_name="Dan Doe"),
        ],
        "checks": [
            {
                "email": "ada@northwind.example",
                "checker": "zerobounce",
                "checked_at": "2026-10-27T09:00:00Z",
                "result": "valid",
            }
        ],
        "now": NOW,
    }
    resp = client().post("/v1/enrich/batch", json=payload)
    assert resp.status_code == 200
    body = resp.json()
    assert [r["action"] for r in body["results"]] == ["send", "no_email"]
    assert body["results"][0]["email"] == "ada@northwind.example"
    assert body["actions"]["send"] == 1
    assert body["ladder"]["by_status"]["verified"]["denominator_name"] == "emails found"


def test_oversized_batch_is_refused():
    payload = {"candidates": [candidate(f"c{i}") for i in range(MAX_BATCH + 1)]}
    assert client().post("/v1/enrich/batch", json=payload).status_code == 413


def test_bad_batches_are_422_not_500():
    dup = {"candidates": [candidate("a"), candidate("a")], "now": NOW}
    resp = client().post("/v1/enrich/batch", json=dup)
    assert resp.status_code == 422
    assert "duplicate candidate_id" in resp.json()["detail"]

    naive = {"candidates": [{**candidate("a"), "observed_at": "2026-10-28T12:00:00"}]}
    assert client().post("/v1/enrich/batch", json=naive).status_code == 422

    future = {
        "candidates": [candidate("a", email="a@northwind.example")],
        "checks": [
            {
                "email": "a@northwind.example",
                "checker": "zerobounce",
                "checked_at": "2026-11-30T00:00:00Z",
                "result": "valid",
            }
        ],
        "now": NOW,
    }
    assert client().post("/v1/enrich/batch", json=future).status_code == 422
