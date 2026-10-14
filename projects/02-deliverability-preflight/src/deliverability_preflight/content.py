"""Content lint for a draft: links, merge tags, formatting, spam-trigger phrases.

The failure this catches is the one that ships to a whole list at once: an
unrendered ``{{first_name}}``, a public link shortener, a link whose visible
text names one domain while ``href`` goes to another, an HTML-only message.

None of these rules is a published standard. Mailbox providers do not publish
their content filters; the rules below are long-standing, widely documented
heuristics (shared shortener domains get blocklisted, mismatched link text is
the classic phishing pattern, image-only mail has no text to classify). Every
threshold and list is a parameter, and each finding says why it fired so a
human can overrule it.

Severity: ``error`` only for what is almost certainly a defect in this send
(unrendered merge tag, link to a bare IP, link text naming a different
domain); everything else is a ``warning`` or ``info``.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Iterable
from html.parser import HTMLParser
from urllib.parse import urlsplit

from pydantic import BaseModel

from .dmarc import OrgDomainFn
from .dmarc import default_org_domain as _default_org_domain
from .findings import Finding

_REF = "content heuristic (not a published provider rule)"

# Public shorteners whose domains are shared by every sender, spammers included.
DEFAULT_SHORTENERS = frozenset(
    {
        "bit.ly",
        "bitly.com",
        "tinyurl.com",
        "t.co",
        "goo.gl",
        "ow.ly",
        "is.gd",
        "buff.ly",
        "rebrand.ly",
        "cutt.ly",
        "shorturl.at",
        "tiny.cc",
        "rb.gy",
    }
)

# Small, explainable default list; replace it with your own via ``spam_phrases``.
DEFAULT_SPAM_PHRASES = (
    "100% free",
    "act now",
    "risk-free",
    "guaranteed",
    "no obligation",
    "click here",
    "limited time",
    "earn extra cash",
    "double your",
    "winner",
    "this is not spam",
    "urgent",
)

MAX_LINKS = 3  # cold first-touch emails; raise it for newsletters
MIN_TEXT_WORDS_WITH_IMAGES = 25

# {{first_name}}, {% if %}, %%FirstName%%, *|FNAME|*, {first_name}
_MERGE_TAG = re.compile(
    r"\{\{.*?\}\}|\{%.*?%\}|%%[A-Za-z_][\w.]*%%|\*\|[A-Za-z_][\w:]*\|\*|\{[a-z_][a-z0-9_.]*\}"
)
_TEXT_URL = re.compile(r"https?://[^\s<>\"')\]]+", re.IGNORECASE)
_DOMAINISH = re.compile(r"^(?:https?://)?(?:www\.)?([a-z0-9-]+(?:\.[a-z0-9-]+)+)(?:[/:?#]\S*)?$")
_HIDDEN_STYLE = re.compile(
    r"display\s*:\s*none|visibility\s*:\s*hidden|font-size\s*:\s*0(?:px|pt|em|%)?\s*(?:;|$)",
    re.IGNORECASE,
)
_WORD = re.compile(r"[A-Za-z0-9À-ÿ']+")


class MessageContent(BaseModel):
    """A draft as it will be sent (after merge-tag rendering)."""

    subject: str = ""
    text: str | None = None
    html: str | None = None


class Link(BaseModel):
    url: str
    host: str | None
    text: str | None = None  # visible anchor text, HTML links only
    source: str  # "html" | "text"


class ContentReport(BaseModel):
    links: list[Link] = []
    link_domains: list[str] = []
    word_count: int = 0
    image_count: int = 0
    findings: list[Finding] = []


class _HtmlScan(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[Link] = []
        self.images = 0
        self.hidden_styles = 0
        self.text_parts: list[str] = []
        self._href: str | None = None
        self._anchor: list[str] = []
        self._skip = 0  # inside <style>/<script>

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k.lower(): v or "" for k, v in attrs}
        if tag in ("style", "script"):
            self._skip += 1
        if tag == "img":
            self.images += 1
        if _HIDDEN_STYLE.search(a.get("style", "")):
            self.hidden_styles += 1
        if tag == "a" and a.get("href"):
            self._href = a["href"].strip()
            self._anchor = []

    def handle_endtag(self, tag: str) -> None:
        if tag in ("style", "script") and self._skip:
            self._skip -= 1
        if tag == "a" and self._href is not None:
            text = " ".join("".join(self._anchor).split())
            self.links.append(
                Link(url=self._href, host=_host(self._href), text=text or None, source="html")
            )
            self._href = None

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        self.text_parts.append(data)
        if self._href is not None:
            self._anchor.append(data)


def _host(url: str) -> str | None:
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https"):
        return None
    try:
        host = parts.hostname
    except ValueError:
        return None
    return host.rstrip(".").lower() if host else None


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def _domain_in_text(text: str) -> str | None:
    """The domain a link's visible text claims to go to, if it looks like one."""
    m = _DOMAINISH.match(text.strip().lower())
    if not m:
        return None
    domain = m.group(1)
    # "v2.0" or "e.g." are not domains: require a letter in the last label
    return domain if re.search(r"[a-z]", domain.rsplit(".", 1)[-1]) else None


def lint_content(
    content: MessageContent,
    *,
    from_domain: str | None = None,
    tracking_domain: str | None = None,
    shorteners: Iterable[str] = DEFAULT_SHORTENERS,
    spam_phrases: Iterable[str] = DEFAULT_SPAM_PHRASES,
    max_links: int = MAX_LINKS,
    org_domain: OrgDomainFn = _default_org_domain,
) -> ContentReport:
    """Lint a rendered draft and explain every finding.

    ``tracking_domain`` is the click-tracking host your ESP rewrites links to;
    when it is outside the From domain's organization it is flagged, because a
    shared tracking domain carries other senders' reputation.
    """
    rep = ContentReport()
    short = {s.lower() for s in shorteners}

    def add(code: str, severity: str, message: str) -> None:
        rep.findings.append(Finding(code=code, severity=severity, message=message, ref=_REF))

    scan = _HtmlScan()
    if content.html:
        scan.feed(content.html)
        scan.close()
    links = list(scan.links)
    if content.text:
        links += [
            Link(url=u.rstrip(".,;:!?"), host=_host(u.rstrip(".,;:!?")), source="text")
            for u in _TEXT_URL.findall(content.text)
        ]
    rep.links = links
    rep.image_count = scan.images

    # merge tags left unrendered: the classic "Hi {{first_name}}" sent to a whole list
    html_text = " ".join(scan.text_parts)
    html_hrefs = " ".join(lk.url for lk in scan.links)
    for where, value in (
        ("subject", content.subject),
        ("text", content.text or ""),
        ("html", f"{html_text} {html_hrefs}"),
    ):
        for tag in sorted(set(_MERGE_TAG.findall(value))):
            add("CONTENT_UNRENDERED_MERGE_TAG", "error", f"{where} contains unrendered {tag!r}")

    from_org = org_domain(from_domain.lower().rstrip(".")) if from_domain else None
    domains: list[str] = []
    for lk in links:
        if lk.host is None:
            if not lk.url.lower().startswith(("mailto:", "tel:", "#")):
                add("CONTENT_LINK_UNPARSABLE", "warning", f"link {lk.url!r} has no http(s) host")
            continue
        if lk.host not in domains:
            domains.append(lk.host)
        if _is_ip(lk.host):
            add("CONTENT_LINK_IP_HOST", "error", f"link {lk.url!r} points at a bare IP address")
            continue
        if lk.host in short or lk.host.removeprefix("www.") in short:
            add(
                "CONTENT_LINK_SHORTENER",
                "warning",
                f"link {lk.url!r} uses public shortener {lk.host}; its shared reputation "
                "is used by spammers too, link the destination or a branded domain",
            )
        if lk.url.lower().startswith("http://"):
            add("CONTENT_LINK_INSECURE", "warning", f"link {lk.url!r} is plain http")
        claimed = _domain_in_text(lk.text) if lk.text else None
        if claimed and org_domain(claimed) != org_domain(lk.host):
            add(
                "CONTENT_LINK_TEXT_MISMATCH",
                "error",
                f"link text says {claimed} but goes to {lk.host}; reads as phishing",
            )
    rep.link_domains = domains
    if len(links) > max_links:
        add(
            "CONTENT_TOO_MANY_LINKS",
            "warning",
            f"{len(links)} links (> {max_links}); first-touch cold email reads as bulk",
        )
    if from_org:
        foreign = sorted(
            {d for d in domains if not _is_ip(d) and org_domain(d) != from_org} - short
        )
        if foreign:
            add(
                "CONTENT_LINK_THIRD_PARTY",
                "info",
                f"links to domains outside {from_org}: {', '.join(foreign)}",
            )
    if tracking_domain and from_org:
        td = tracking_domain.lower().rstrip(".")
        if org_domain(td) != from_org:
            add(
                "CONTENT_TRACKING_DOMAIN_UNBRANDED",
                "warning",
                f"click-tracking domain {td} is not under {from_org}; use a branded "
                "tracking CNAME so clicks do not share another sender's reputation",
            )

    # structure
    if content.html and not (content.text or "").strip():
        add(
            "CONTENT_HTML_WITHOUT_TEXT",
            "warning",
            "HTML part without a text/plain alternative",
        )
    if not content.html and not (content.text or "").strip():
        add("CONTENT_EMPTY_BODY", "error", "message has no body")
    words = len(_WORD.findall(html_text if content.html else content.text or ""))
    rep.word_count = words
    if scan.images and words < MIN_TEXT_WORDS_WITH_IMAGES:
        add(
            "CONTENT_IMAGE_HEAVY",
            "warning",
            f"{scan.images} image(s) with only {words} words of text; filters cannot read images",
        )
    if scan.hidden_styles:
        add(
            "CONTENT_HIDDEN_ELEMENTS",
            "warning",
            f"{scan.hidden_styles} element(s) hidden with CSS (display:none, "
            "visibility:hidden, font-size:0); hidden text is a filter-evasion pattern",
        )

    # wording
    subject = content.subject.strip()
    if not subject:
        add("CONTENT_EMPTY_SUBJECT", "warning", "subject is empty")
    letters = [c for c in subject if c.isalpha()]
    if len(letters) >= 8 and sum(c.isupper() for c in letters) / len(letters) > 0.7:
        add("CONTENT_SUBJECT_SHOUTING", "warning", f"subject is mostly capitals: {subject!r}")
    if re.search(r"[!?]{2,}|\${2,}", subject):
        add(
            "CONTENT_SUBJECT_PUNCTUATION",
            "warning",
            f"subject has repeated !, ? or $: {subject!r}",
        )
    haystack = " ".join([subject, content.text or "", html_text]).lower()
    hits = [p for p in spam_phrases if re.search(rf"(?<!\w){re.escape(p.lower())}(?!\w)", haystack)]
    if hits:
        add(
            "CONTENT_SPAM_PHRASES",
            "warning",
            f"spam-trigger phrases: {', '.join(repr(h) for h in hits)}",
        )
    return rep
