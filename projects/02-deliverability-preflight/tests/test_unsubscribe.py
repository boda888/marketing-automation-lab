from deliverability_preflight import (
    check_one_click,
    dkim_signed_headers,
    headers_from_message,
)

GOOD = [
    ("From", "Ada <ada@acme.example>"),
    (
        "List-Unsubscribe",
        "<mailto:u@acme.example?subject=unsub>, <https://acme.example/u/opaque123>",
    ),
    ("List-Unsubscribe-Post", "List-Unsubscribe=One-Click"),
    (
        "DKIM-Signature",
        "v=1; a=rsa-sha256; d=acme.example; s=s1; "
        "h=from:to:subject:list-unsubscribe:list-unsubscribe-post; bh=x; b=y",
    ),
]


def codes(rep):
    return {f.code for f in rep.findings}


def replace(headers, name, value):
    out = [(n, v) for n, v in headers if n.lower() != name.lower()]
    return out + ([(name, value)] if value is not None else [])


def test_compliant_headers():
    rep = check_one_click(GOOD)
    assert rep.one_click and rep.findings == []
    assert rep.https_uris == ["https://acme.example/u/opaque123"]
    assert rep.mailto_uris == ["mailto:u@acme.example?subject=unsub"]
    assert rep.dkim_covers_headers is True


def test_header_names_are_case_insensitive():
    lowered = [(n.lower(), v) for n, v in GOOD]
    assert check_one_click(lowered).one_click


def test_mailto_only_is_not_one_click():
    rep = check_one_click(replace(GOOD, "List-Unsubscribe", "<mailto:u@acme.example>"))
    assert not rep.one_click and "UNSUB_NO_HTTPS_URI" in codes(rep)


def test_http_uri_rejected():
    rep = check_one_click(replace(GOOD, "List-Unsubscribe", "<http://acme.example/u/1>"))
    assert {"UNSUB_HTTP_NOT_HTTPS", "UNSUB_NO_HTTPS_URI"} <= codes(rep)


def test_two_https_uris_rejected():
    value = "<https://acme.example/u/1>, <https://acme.example/u/2>"
    assert "UNSUB_MULTIPLE_HTTPS_URIS" in codes(
        check_one_click(replace(GOOD, "List-Unsubscribe", value))
    )


def test_bare_uri_without_brackets_is_syntax_error():
    rep = check_one_click(replace(GOOD, "List-Unsubscribe", "https://acme.example/u/1"))
    assert "UNSUB_HEADER_SYNTAX" in codes(rep)


def test_missing_and_duplicate_list_unsubscribe():
    assert "UNSUB_HEADER_MISSING" in codes(check_one_click(replace(GOOD, "List-Unsubscribe", None)))
    dup = GOOD + [("List-Unsubscribe", "<https://acme.example/u/2>")]
    assert "UNSUB_HEADER_DUPLICATE" in codes(check_one_click(dup))


def test_post_header_rules():
    missing = check_one_click(replace(GOOD, "List-Unsubscribe-Post", None))
    assert "UNSUB_POST_MISSING" in codes(missing)
    wrong = check_one_click(replace(GOOD, "List-Unsubscribe-Post", "List-Unsubscribe=Yes"))
    assert "UNSUB_POST_INVALID" in codes(wrong)
    extra = replace(GOOD, "List-Unsubscribe-Post", "List-Unsubscribe=One-Click; foo=bar")
    assert "UNSUB_POST_INVALID" in codes(check_one_click(extra))


def test_dkim_must_cover_both_headers():
    partial = replace(GOOD, "DKIM-Signature", "v=1; d=acme.example; h=from:list-unsubscribe; b=y")
    rep = check_one_click(partial)
    assert rep.dkim_covers_headers is False and "UNSUB_NOT_DKIM_SIGNED" in codes(rep)
    # a second signature that does cover them is enough
    ok = partial + [("DKIM-Signature", GOOD[3][1])]
    assert check_one_click(ok).one_click


def test_unsigned_draft_gets_warning_not_error():
    rep = check_one_click(replace(GOOD, "DKIM-Signature", None))
    assert rep.dkim_covers_headers is None
    assert codes(rep) == {"UNSUB_DKIM_UNKNOWN"} and rep.one_click


def test_dkim_h_tag_parsing_handles_folding_and_case():
    sig = "v=1; d=acme.example;\r\n\th=From : List-Unsubscribe:\r\n List-Unsubscribe-Post; b=abc"
    assert dkim_signed_headers(sig) == ["from", "list-unsubscribe", "list-unsubscribe-post"]
    assert dkim_signed_headers("v=1; b=abc") == []


def test_headers_from_raw_message_with_folded_header():
    raw = (
        "From: ada@acme.example\r\n"
        "List-Unsubscribe: <mailto:u@acme.example>,\r\n <https://acme.example/u/opaque123>\r\n"
        "List-Unsubscribe-Post: List-Unsubscribe=One-Click\r\n"
        "Subject: hi\r\n\r\nbody\r\n"
    )
    rep = check_one_click(headers_from_message(raw))
    assert rep.https_uris == ["https://acme.example/u/opaque123"]
    assert codes(rep) == {"UNSUB_DKIM_UNKNOWN"}
