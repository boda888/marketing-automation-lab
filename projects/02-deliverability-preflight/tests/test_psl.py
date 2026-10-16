"""Public Suffix List matching.

The ICANN/private rules in the sample are real rules from the published list
(publicsuffix.org); ``co.example`` is a fictional suffix used for the
end-to-end tests so every company domain stays on ``.example``.
"""

import pytest
from deliverability_preflight import (
    FakeResolver,
    PublicSuffixList,
    aligned,
    lint_content,
    preflight,
)
from deliverability_preflight.content import MessageContent

PSL_SAMPLE = """\
// sample of rules from public_suffix_list.dat
// ===BEGIN ICANN DOMAINS===

// ck : https://en.wikipedia.org/wiki/.ck
*.ck
!www.ck

com

// jp
jp
*.kawasaki.jp
!city.kawasaki.jp

// uk
uk
co.uk

公司.cn
cn

// fictional, for tests
co.example

// ===END ICANN DOMAINS===
// ===BEGIN PRIVATE DOMAINS===

github.io
blogspot.com

// ===END PRIVATE DOMAINS===
"""


@pytest.fixture
def psl():
    return PublicSuffixList(PSL_SAMPLE)


@pytest.mark.parametrize(
    ("domain", "suffix", "registrable"),
    [
        ("a.co.uk", "co.uk", "a.co.uk"),
        ("mail.a.co.uk", "co.uk", "a.co.uk"),
        ("co.uk", "co.uk", None),
        ("uk", "uk", None),
        ("www.a.com", "com", "a.com"),
        ("news.acme.example", "example", "acme.example"),  # unlisted TLD: implicit "*"
        ("a.b.ck", "b.ck", "a.b.ck"),  # wildcard
        ("b.ck", "b.ck", None),
        ("www.ck", "ck", "www.ck"),  # exception beats the wildcard
        ("mail.www.ck", "ck", "www.ck"),
        ("x.y.kawasaki.jp", "y.kawasaki.jp", "x.y.kawasaki.jp"),
        ("www.city.kawasaki.jp", "kawasaki.jp", "city.kawasaki.jp"),
        ("a.github.io", "github.io", "a.github.io"),
        ("Mail.A.CO.uk.", "co.uk", "a.co.uk"),
    ],
)
def test_public_suffix_and_registrable_domain(psl, domain, suffix, registrable):
    assert psl.public_suffix(domain) == suffix
    assert psl.registrable_domain(domain) == registrable


def test_idn_rules_match_a_labels(psl):
    a_label = "公司.cn".encode("idna").decode()
    assert psl.public_suffix(f"a.{a_label}") == a_label
    assert psl.public_suffix("a.公司.cn") == "公司.cn"


def test_private_section_can_be_excluded():
    icann = PublicSuffixList(PSL_SAMPLE, include_private=False)
    assert icann.org_domain("a.github.io") == "github.io"
    assert icann.org_domain("co.uk") == "co.uk"  # a suffix is its own org domain


def test_comments_and_trailing_text_are_ignored():
    psl = PublicSuffixList("// co.example\nco.example  some trailing note\n")
    assert psl.public_suffix("shop.co.example") == "co.example"
    assert psl.public_suffix("co") == "co"


def test_from_file(tmp_path):
    path = tmp_path / "public_suffix_list.dat"
    path.write_text(PSL_SAMPLE, encoding="utf-8")
    assert PublicSuffixList.from_file(path).org_domain("mail.a.co.uk") == "a.co.uk"


def test_alignment_under_a_multi_label_suffix(psl):
    assert aligned("bounce.shop.co.example", "shop.co.example", "r", psl.org_domain)
    assert not aligned("other.co.example", "shop.co.example", "r", psl.org_domain)
    assert aligned("other.co.example", "shop.co.example", "r")  # naive last-two-labels


def test_content_lint_sees_third_party_tracking_domain(psl):
    draft = MessageContent(
        subject="Quick question",
        text="Hi, see https://click.other.co.example/r/1 for details.",
    )
    naive = lint_content(
        draft, from_domain="shop.co.example", tracking_domain="click.other.co.example"
    )
    exact = lint_content(
        draft,
        from_domain="shop.co.example",
        tracking_domain="click.other.co.example",
        org_domain=psl.org_domain,
    )
    assert naive.findings == []  # last-two-labels: everything is "co.example"
    assert {"CONTENT_TRACKING_DOMAIN_UNBRANDED", "CONTENT_LINK_THIRD_PARTY"} <= {
        f.code for f in exact.findings
    }


def test_preflight_with_psl(psl):
    r = FakeResolver(
        {
            ("_dmarc.shop.co.example", "TXT"): ["v=DMARC1; p=reject; rua=mailto:d@shop.co.example"],
            ("shop.co.example", "TXT"): ["v=spf1 -all"],
            ("other.co.example", "TXT"): ["v=spf1 ip4:192.0.2.1 -all"],
        }
    )
    rep = preflight(
        "shop.co.example", r, return_path_domain="other.co.example", org_domain=psl.org_domain
    )
    assert not rep.alignment.spf_aligned
