import pytest
from deliverability_preflight import MessageContent, lint_content, preflight
from test_preflight import HEADERS, good_dns

CLEAN_TEXT = (
    "Hi Dana,\n\nSaw that Northwind Example is hiring three SDRs in Lisbon. "
    "We help teams like yours ramp new reps faster with call reviews.\n\n"
    "Worth a 15-minute chat next week? Details: https://acme.example/sdr-ramp\n\nSam"
)
CLEAN = MessageContent(subject="Ramping the new Lisbon SDRs", text=CLEAN_TEXT)


def codes(rep):
    return {f.code for f in rep.findings}


def lint(**kw):
    tracking = kw.pop("tracking_domain", None)
    return lint_content(
        MessageContent(**{"subject": "Quick question", "text": CLEAN_TEXT, **kw}),
        from_domain="acme.example",
        tracking_domain=tracking,
    )


def test_clean_message_has_no_findings():
    rep = lint_content(CLEAN, from_domain="acme.example")
    assert rep.findings == []
    assert [lk.host for lk in rep.links] == ["acme.example"]
    assert rep.word_count > 25


@pytest.mark.parametrize(
    "tag", ["{{first_name}}", "{{ company.name }}", "%%FirstName%%", "*|FNAME|*", "{first_name}"]
)
def test_unrendered_merge_tags_are_errors(tag):
    rep = lint(subject=f"Hi {tag}")
    msgs = [f.message for f in rep.findings if f.code == "CONTENT_UNRENDERED_MERGE_TAG"]
    assert msgs and tag in msgs[0] and msgs[0].startswith("subject")


def test_merge_tag_in_html_href_is_found():
    html = '<p>Hi there, read <a href="https://acme.example/r?id={{lead_id}}">this</a></p>'
    rep = lint(html=html)
    assert any(
        f.code == "CONTENT_UNRENDERED_MERGE_TAG" and f.message.startswith("html")
        for f in rep.findings
    )


def test_ordinary_braces_and_prices_are_not_merge_tags():
    rep = lint(text=CLEAN_TEXT + '\nPricing starts at $49 {per seat}. Use JSON like {"a": 1}.')
    assert "CONTENT_UNRENDERED_MERGE_TAG" not in codes(rep)


def test_shortener_insecure_and_ip_links():
    text = CLEAN_TEXT + "\nhttps://bit.ly/abc http://acme.example/x http://203.0.113.9/login"
    rep = lint(text=text)
    c = codes(rep)
    assert {"CONTENT_LINK_SHORTENER", "CONTENT_LINK_INSECURE", "CONTENT_LINK_IP_HOST"} <= c
    ip = next(f for f in rep.findings if f.code == "CONTENT_LINK_IP_HOST")
    assert ip.severity == "error"
    # 4 links > default max of 3
    assert "CONTENT_TOO_MANY_LINKS" in c


def test_trailing_punctuation_is_not_part_of_text_url():
    rep = lint(text="See https://acme.example/case-study. Thanks, Sam and the team at Acme Example")
    assert rep.links[0].url == "https://acme.example/case-study"


def test_link_text_naming_other_domain_is_mismatch():
    html = (
        '<p>Log in at <a href="https://acme-login.example/x">www.bank.example</a> '
        'or see <a href="https://www.acme.example/p">acme.example/pricing</a>.</p>'
    )
    rep = lint(html=html)
    mism = [f for f in rep.findings if f.code == "CONTENT_LINK_TEXT_MISMATCH"]
    assert len(mism) == 1 and "bank.example" in mism[0].message
    assert mism[0].severity == "error"


def test_plain_anchor_text_is_not_mismatch():
    rep = lint(html='<p>Read <a href="https://acme.example/x">the case study v2.0</a></p>')
    assert "CONTENT_LINK_TEXT_MISMATCH" not in codes(rep)


def test_third_party_links_are_info_only():
    rep = lint(text=CLEAN_TEXT + " https://news.example/article")
    f = next(f for f in rep.findings if f.code == "CONTENT_LINK_THIRD_PARTY")
    assert f.severity == "info" and "news.example" in f.message


def test_subdomain_links_are_same_organization():
    rep = lint(text=CLEAN_TEXT + " https://go.acme.example/demo")
    assert "CONTENT_LINK_THIRD_PARTY" not in codes(rep)


def test_tracking_domain_branding():
    assert "CONTENT_TRACKING_DOMAIN_UNBRANDED" in codes(lint(tracking_domain="trk.esp.example"))
    assert "CONTENT_TRACKING_DOMAIN_UNBRANDED" not in codes(
        lint(tracking_domain="clicks.acme.example")
    )


def test_html_only_image_heavy_and_hidden():
    html = (
        '<div style="display:none">cheap pills</div>'
        '<img src="https://acme.example/banner.png"><p>Hello there</p>'
        "<style>p{font-size:0}</style>"
    )
    rep = lint_content(MessageContent(subject="Hello", html=html), from_domain="acme.example")
    c = codes(rep)
    assert {"CONTENT_HTML_WITHOUT_TEXT", "CONTENT_IMAGE_HEAVY", "CONTENT_HIDDEN_ELEMENTS"} <= c
    hidden = next(f for f in rep.findings if f.code == "CONTENT_HIDDEN_ELEMENTS")
    assert hidden.message.startswith("1 element")  # <style> content is not an element style
    assert rep.image_count == 1


def test_empty_message():
    rep = lint_content(MessageContent(), from_domain="acme.example")
    assert {"CONTENT_EMPTY_BODY", "CONTENT_EMPTY_SUBJECT"} <= codes(rep)


def test_subject_shouting_punctuation_and_phrases():
    rep = lint(subject="ACT NOW: GUARANTEED RESULTS!!!")
    c = codes(rep)
    assert {"CONTENT_SUBJECT_SHOUTING", "CONTENT_SUBJECT_PUNCTUATION"} <= c
    phrases = next(f for f in rep.findings if f.code == "CONTENT_SPAM_PHRASES")
    assert "'act now'" in phrases.message and "'guaranteed'" in phrases.message
    # short acronyms are not shouting
    assert "CONTENT_SUBJECT_SHOUTING" not in codes(lint(subject="Q3 SDR ramp at ACME"))


def test_spam_phrases_match_whole_words_and_are_configurable():
    rep = lint_content(
        MessageContent(subject="Insurgent marketing", text=CLEAN_TEXT),
        spam_phrases=["urgent"],
    )
    assert "CONTENT_SPAM_PHRASES" not in codes(rep)
    rep = lint_content(
        MessageContent(subject="Quarterly review", text=CLEAN_TEXT), spam_phrases=["quarterly"]
    )
    assert "CONTENT_SPAM_PHRASES" in codes(rep)


def test_max_links_parameter():
    text = CLEAN_TEXT + " https://acme.example/a https://acme.example/b"
    assert "CONTENT_TOO_MANY_LINKS" not in codes(
        lint_content(MessageContent(subject="Hi", text=text), max_links=5)
    )


def test_mailto_links_are_not_unparsable():
    html = '<p><a href="mailto:sam@acme.example">Email me</a> <a href="javascript:x()">x</a></p>'
    rep = lint(html=html)
    unparsable = [f for f in rep.findings if f.code == "CONTENT_LINK_UNPARSABLE"]
    assert len(unparsable) == 1 and "javascript" in unparsable[0].message


def test_preflight_wires_content_into_verdict():
    kw = {"dkim_selectors": ["s1"], "return_path_domain": "bounce.acme.example", "headers": HEADERS}
    clean = preflight("acme.example", good_dns(), content=CLEAN, **kw)
    assert clean.verdict == "pass", clean.findings
    assert clean.content is not None and clean.content.findings == []
    bad = preflight(
        "acme.example",
        good_dns(),
        content=MessageContent(subject="Hi {{first_name}}", text=CLEAN_TEXT),
        **kw,
    )
    assert bad.verdict == "block"
    assert "CONTENT_UNRENDERED_MERGE_TAG" in {f.code for f in bad.findings}
    review = preflight(
        "acme.example", good_dns(), content=CLEAN, tracking_domain="trk.esp.example", **kw
    )
    assert review.verdict == "review"
    assert preflight("acme.example", good_dns(), **kw).content is None
