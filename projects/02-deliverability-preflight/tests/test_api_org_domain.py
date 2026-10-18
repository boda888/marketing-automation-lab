"""Org-domain strategy of the FastAPI service (PREFLIGHT_ORG_DOMAIN).

``co.example`` plays a public suffix: ``shop.co.example`` and
``other.co.example`` are two different companies that the naive
last-two-labels rule merges into one organization.
"""

import pytest
from deliverability_preflight.api import create_app, org_domain_from_env
from deliverability_preflight.dns import FakeResolver
from fastapi.testclient import TestClient

SPF = "v=spf1 ip4:198.51.100.0/24 -all"
REC = "v=DMARC1; p=reject; rua=mailto:d@shop.co.example"
PSL = "// fictional suffix for tests\nexample\nco.example\n"

# From shop.co.example, bounces on another company's domain under the suffix
REQ = {
    "from_domain": "shop.co.example",
    "return_path_domain": "other.co.example",
    "dkim_selectors": [],
}


def _resolver(**extra):
    return FakeResolver(
        {
            ("other.co.example", "TXT"): [SPF],
            ("_dmarc.shop.co.example", "TXT"): [REC],
            ("_dmarc.co.example", "TXT"): ["v=DMARC1; p=none; psd=y"],
        },
        **extra,
    )


@pytest.fixture()
def psl_file(tmp_path):
    p = tmp_path / "public_suffix_list.dat"
    p.write_text(PSL)
    return p


def _post(monkeypatch, resolver, **env):
    for key in ("PREFLIGHT_ORG_DOMAIN", "PREFLIGHT_PSL_FILE"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, str(value))
    client = TestClient(create_app(resolver))
    return client.get("/healthz").json(), client.post("/v1/preflight", json=REQ).json()


def test_default_last_two_labels_aligns_two_companies(monkeypatch):
    health, body = _post(monkeypatch, _resolver())
    assert health["org_domain_strategy"] == body["org_domain_strategy"] == "last-two"
    assert body["alignment"]["spf_aligned"] is True  # the false pass PSL / tree walk fix


def test_psl_strategy_keeps_companies_apart(monkeypatch, psl_file):
    _, body = _post(
        monkeypatch, _resolver(), PREFLIGHT_ORG_DOMAIN="psl", PREFLIGHT_PSL_FILE=psl_file
    )
    assert body["org_domain_strategy"] == "psl"
    assert body["alignment"]["spf_aligned"] is False
    assert "DMARC_NOT_ALIGNED" in {f["code"] for f in body["findings"]}


def test_tree_walk_strategy_keeps_companies_apart(monkeypatch):
    _, body = _post(monkeypatch, _resolver(), PREFLIGHT_ORG_DOMAIN="tree-walk")
    assert body["org_domain_strategy"] == "tree-walk"
    assert body["alignment"]["spf_aligned"] is False
    assert "ORG_DOMAIN_TEMPERROR" not in {f["code"] for f in body["findings"]}


def test_tree_walk_dns_failure_is_reported_not_silent(monkeypatch):
    resolver = _resolver(fail=[("_dmarc.other.co.example", "TXT")])
    _, body = _post(monkeypatch, resolver, PREFLIGHT_ORG_DOMAIN="tree-walk")
    temp = [f for f in body["findings"] if f["code"] == "ORG_DOMAIN_TEMPERROR"]
    assert len(temp) == 1 and temp[0]["severity"] == "warning"
    assert "other.co.example" in temp[0]["message"]
    assert body["verdict"] != "pass"


def test_tree_walk_cache_does_not_outlive_a_request(monkeypatch):
    # a long-running service must re-walk on each request, or DNS changes never show
    resolver = _resolver()
    monkeypatch.setenv("PREFLIGHT_ORG_DOMAIN", "tree-walk")
    client = TestClient(create_app(resolver))
    walked = ("_dmarc.other.co.example", "TXT")  # queried only by the org-domain walk
    client.post("/v1/preflight", json=REQ)
    first = resolver.queries.count(walked)
    client.post("/v1/preflight", json=REQ)
    assert first >= 1 and resolver.queries.count(walked) == 2 * first


@pytest.mark.parametrize(
    ("env", "match"),
    [
        ({"PREFLIGHT_ORG_DOMAIN": "psl"}, "PREFLIGHT_PSL_FILE"),
        ({"PREFLIGHT_ORG_DOMAIN": "suffix"}, "expected one of"),
    ],
)
def test_misconfiguration_fails_at_startup(monkeypatch, env, match):
    monkeypatch.delenv("PREFLIGHT_PSL_FILE", raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(ValueError, match=match):
        org_domain_from_env()


def test_empty_psl_file_rejected(monkeypatch, tmp_path):
    empty = tmp_path / "empty.dat"
    empty.write_text("// nothing here\n")
    monkeypatch.setenv("PREFLIGHT_ORG_DOMAIN", "psl")
    monkeypatch.setenv("PREFLIGHT_PSL_FILE", str(empty))
    with pytest.raises(ValueError, match="no rules"):
        org_domain_from_env()
