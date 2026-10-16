"""DMARCbis DNS Tree Walk (draft-ietf-dmarc-dmarcbis-41 §4.10, Appendix B.4)."""

import pytest
from deliverability_preflight import (
    FakeResolver,
    TreeWalkOrgDomain,
    aligned,
    check_alignment,
    check_dmarc,
    parse_dmarc,
    preflight,
    tree_walk_org_domain,
    tree_walk_targets,
)
from deliverability_preflight.dmarc import DmarcSyntaxError

REC = "v=DMARC1; p=reject; rua=mailto:d@example.com"


def codes(report):
    return {f.code for f in report.findings}


def txt(**names):
    return {(f"_dmarc.{n.replace('_', '.')}", "TXT"): [v] for n, v in names.items()}


def test_targets_short_name_walks_every_parent_to_the_tld():
    assert tree_walk_targets("a.mail.example.com") == [
        "a.mail.example.com",
        "mail.example.com",
        "example.com",
        "com",
    ]
    assert tree_walk_targets("Example.COM.") == ["example.com", "com"]


def test_targets_long_name_jumps_to_seven_labels():
    # the §4.10 example: eight queries for a 13-label Author Domain
    assert tree_walk_targets("a.b.c.d.e.f.g.h.i.j.mail.example.com") == [
        "a.b.c.d.e.f.g.h.i.j.mail.example.com",
        "g.h.i.j.mail.example.com",
        "h.i.j.mail.example.com",
        "i.j.mail.example.com",
        "j.mail.example.com",
        "mail.example.com",
        "example.com",
        "com",
    ]
    eight = "a.b.c.d.e.f.example.com"
    assert len(eight.split(".")) == 8
    assert tree_walk_targets(eight)[1] == "b.c.d.e.f.example.com"
    assert len(tree_walk_targets(eight)) == 8


def test_parse_psd_tag():
    assert parse_dmarc("v=DMARC1; p=none").psd == "u"
    assert parse_dmarc("v=DMARC1; p=none; psd=Y").psd == "y"
    with pytest.raises(DmarcSyntaxError):
        parse_dmarc("v=DMARC1; p=none; psd=maybe")


def test_org_domain_is_highest_record_b41():
    # B.4.1: records at example.com and signing.example.com, none at com
    r = FakeResolver({**txt(example_com=REC), **txt(signing_example_com=REC)})
    assert tree_walk_org_domain("example.com", r) == "example.com"
    assert tree_walk_org_domain("signing.example.com", r) == "example.com"
    assert tree_walk_org_domain("x.y.signing.example.com", r) == "example.com"


def test_org_domain_psd_n_stops_the_walk():
    r = FakeResolver({**txt(mail_example_com=REC + "; psd=n"), **txt(example_com=REC)})
    assert tree_walk_org_domain("a.mail.example.com", r) == "mail.example.com"
    assert ("_dmarc.example.com", "TXT") not in r.queries


def test_org_domain_below_psd_y_b43():
    # B.4.3: bank.example is a public suffix that publishes psd=y
    r = FakeResolver(
        {
            **txt(bank_example="v=DMARC1; p=reject; psd=y"),
            **txt(giant_bank_example=REC),
        }
    )
    assert tree_walk_org_domain("giant.bank.example", r) == "giant.bank.example"
    assert tree_walk_org_domain("mail.giant.bank.example", r) == "giant.bank.example"
    assert tree_walk_org_domain("mail.mega.bank.example", r) == "mega.bank.example"
    # only psd=y record at the start: the start is its own org domain
    assert tree_walk_org_domain("bank.example", r) == "bank.example"


def test_org_domain_without_any_record_is_the_domain_itself():
    assert tree_walk_org_domain("news.acme.example", FakeResolver({})) == "news.acme.example"


def test_tree_walk_alignment_where_last_two_labels_is_wrong():
    # co.example acts as a public suffix: last-two-labels would align two companies
    r = FakeResolver(
        {
            **txt(co_example="v=DMARC1; p=none; psd=y"),
            **txt(shop_co_example=REC),
            **txt(other_co_example=REC),
        }
    )
    walk = TreeWalkOrgDomain(r)
    assert aligned("mail.shop.co.example", "shop.co.example", "r", walk)
    assert not aligned("other.co.example", "shop.co.example", "r", walk)
    assert aligned("other.co.example", "shop.co.example", "r")  # naive default
    res = check_alignment(
        "shop.co.example",
        spf_domain="other.co.example",
        dkim_domains=["other.co.example"],
        org_domain=walk,
    )
    assert "DMARC_NOT_ALIGNED" in codes(res)


def test_tree_walk_org_domain_caches_and_fails_strict():
    r = FakeResolver(txt(acme_example=REC), fail=[("_dmarc.example", "TXT")])
    walk = TreeWalkOrgDomain(r)
    assert walk("bounce.acme.example") == "bounce.acme.example"  # DNS failed: strict
    assert walk.errors == ["bounce.acme.example"]
    ok = TreeWalkOrgDomain(FakeResolver(txt(acme_example=REC)))
    assert ok("bounce.acme.example") == ok("bounce.acme.example") == "acme.example"


def test_policy_from_org_domain_not_nearest_parent_b42():
    # nearest parent mail.acme.example has a record, but the org domain is acme.example
    r = FakeResolver(
        {
            **txt(mail_acme_example="v=DMARC1; p=none; rua=mailto:d@acme.example"),
            **txt(acme_example="v=DMARC1; p=reject; sp=quarantine; rua=mailto:d@acme.example"),
        }
    )
    rep = check_dmarc("news.mail.acme.example", r)
    assert rep.policy_domain == rep.organizational_domain == "acme.example"
    assert rep.effective_policy == "quarantine"
    assert "DMARC_INHERITED" in codes(rep)


def test_author_domain_record_wins():
    r = FakeResolver(
        {
            **txt(news_acme_example="v=DMARC1; p=none; rua=mailto:d@acme.example"),
            **txt(acme_example=REC),
        }
    )
    rep = check_dmarc("news.acme.example", r)
    assert rep.policy_domain == "news.acme.example" and rep.effective_policy == "none"
    assert r.queries == [("_dmarc.news.acme.example", "TXT")]


def test_psd_policy_applies_when_org_domain_has_none():
    r = FakeResolver(txt(bank_example="v=DMARC1; p=reject; psd=y; rua=mailto:psd@bank.example"))
    rep = check_dmarc("giant.bank.example", r)
    assert rep.status == "valid"
    assert rep.policy_domain == "bank.example"
    assert rep.organizational_domain == "giant.bank.example"
    assert "DMARC_PSD_POLICY_ONLY" in codes(rep)


def test_duplicate_author_records_are_discarded_and_walk_continues():
    r = FakeResolver(
        {
            ("_dmarc.news.acme.example", "TXT"): ["v=DMARC1; p=none", "v=DMARC1; p=reject"],
            **txt(acme_example=REC),
        }
    )
    rep = check_dmarc("news.acme.example", r)
    assert "DMARC_MULTIPLE_RECORDS" in codes(rep)
    assert rep.policy_domain == "acme.example" and rep.effective_policy == "reject"


def test_long_author_domain_is_capped_at_eight_queries():
    deep = "a.b.c.d.e.f.g.h.i.j.k.acme.example"
    r = FakeResolver(txt(acme_example=REC))
    rep = check_dmarc(deep, r)
    assert rep.policy_domain == "acme.example"
    walk = [n for n, _ in r.queries if n.startswith("_dmarc.")]
    assert len(walk) == 8 and walk[-1] == "_dmarc.example"


def test_temp_error_in_parent_walk():
    r = FakeResolver({}, fail=[("_dmarc.acme.example", "TXT")])
    assert check_dmarc("news.acme.example", r).status == "temperror"


def test_preflight_alignment_can_use_tree_walk():
    r = FakeResolver(
        {
            **txt(co_example="v=DMARC1; p=none; psd=y"),
            **txt(shop_co_example="v=DMARC1; p=reject; rua=mailto:d@shop.co.example"),
            ("shop.co.example", "TXT"): ["v=spf1 -all"],
            ("other.co.example", "TXT"): ["v=spf1 ip4:192.0.2.1 -all"],
        }
    )
    naive = preflight("shop.co.example", r, return_path_domain="other.co.example")
    walked = preflight(
        "shop.co.example",
        r,
        return_path_domain="other.co.example",
        org_domain=TreeWalkOrgDomain(r),
    )
    assert naive.alignment.spf_aligned
    assert not walked.alignment.spf_aligned
