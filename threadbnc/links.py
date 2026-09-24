"""Telling when two links lead to the same page, and when two pages tell the
same story.

A link's key is its address with everything that doesn't change the page taken
off: the scheme, www. and m., trailing slashes, click-tracking parameters
(dupes.normalize_url), and the wrappers other sites put around a page (AMP
copies, the Wayback Machine, Google's and Facebook's redirects, paywall
readers). An article is known by the key of the link it was posted with, the
address it was finally read from (after redirects: feedburner, t.co, bit.ly)
and the address the page itself says it lives at (<link rel="canonical"> or
og:url), so a feed's ?utm_source=rss link, a threadiverse post of the plain
address and a Reddit post of the AMP copy all find each other (articles.py).

Different pages can carry the same story too: a wire story syndicated across
papers, or a post republished on another site. Those are only "probably the
same story", told by their text (a simhash of it, near enough) or by the same
headline published within a couple of days, and are shown as that rather than
merged.
"""

from __future__ import annotations

import hashlib
import re
from datetime import timedelta
from typing import Any
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

import lxml.html
from lxml import etree

from .db import parse_ts
from .dupes import normalize_url

# Query parameters that only say where a click came from, beyond dupes' list.
TRACKING = {"source", "rss", "cmp", "cmpid", "ocid", "taid", "at_medium", "at_campaign", "at_custom1", "at_custom2",
            "at_custom3", "at_custom4", "guccounter", "guce_referrer", "guce_referrer_sig", "_ga", "_gl", "ito",
            "mkt_tok", "icid", "int_source", "trk", "sr_share", "s_cid", "wt_mc", "wt.mc_id", "ns_campaign",
            "ns_mchannel", "ns_source", "ns_linkname", "ns_fee", "rss_source", "feedname", "outputtype", "amp",
            "amp_js_v", "usqp"}
# Sites that show someone else's page at an address of their own, with the
# original's address inside theirs.
_WAYBACK = re.compile(r"^/web/\d+[a-z_]*/(.+)$")
_ARCHIVE_TODAY = ("archive.today", "archive.ph", "archive.is", "archive.li", "archive.md", "archive.vn", "archive.fo")
_PROXIES = ("12ft.io", "removepaywall.com", "outline.com", "r.jina.ai", "smry.ai")
_REDIRECTS = {"google.com": ("/url", ("q", "url")), "l.facebook.com": ("/l.php", ("u",)),
              "lm.facebook.com": ("/l.php", ("u",)), "l.instagram.com": ("/", ("u",)),
              "out.reddit.com": (None, ("url",)), "href.li": (None, ()), "t.umblr.com": ("/redirect", ("z",))}
_AMP_PATH = re.compile(r"(?:/amp)+/?$|/amp(?=/)|\.amp(?=\.html?$)", re.IGNORECASE)
_INDEX = re.compile(r"/index\.(?:html?|php|aspx?)$", re.IGNORECASE)
MAX_UNWRAP = 3  # a wrapper inside a wrapper, at most this deep

# "Probably the same story".
SIMHASH_BITS = 64
BANDS = 4  # the simhash in this many 16-bit parts: two within 3 bits of each other share at least one
NEAR_BITS = 3
SHINGLE = 4  # words
MAX_WORDS = 3000  # the start of a long article says enough
MIN_TITLE_WORDS = 4  # shorter headlines ("Live updates", "Letters") say too little
SAME_DAYS = 2
_WORD = re.compile(r"\w+", re.UNICODE)
_TITLE_TAIL = re.compile(r"\s+[|–—-]\s+[^|–—-]{2,40}$")  # " | The Gazette", " - BBC News"


def _embedded(text: str) -> str | None:
    """A web address inside another's path or query, as sites that wrap pages put it."""
    text = unquote(text) if "%3a" in text[:12].lower() else text
    if text.startswith(("http://", "https://")):
        return text
    if re.match(r"^https?:/[^/]", text):  # some servers squash the //
        return text.replace(":/", "://", 1)
    return None


def unwrap(url: str) -> str:
    """The page a wrapped address shows: the original inside an AMP cache,
    archive or redirect address; the address itself otherwise."""
    for _ in range(MAX_UNWRAP):
        try:
            parts = urlsplit(url.strip())
        except ValueError:
            return url
        host = (parts.hostname or "").lower()
        bare = host[4:] if host.startswith("www.") else host
        inner: str | None = None
        if bare == "web.archive.org" and (m := _WAYBACK.match(parts.path)):
            inner = _embedded(m.group(1) + (f"?{parts.query}" if parts.query else ""))
        elif bare in _ARCHIVE_TODAY:
            path = parts.path.lstrip("/")
            path = path.split("/", 1)[1] if path.startswith(("newest/", "oldest/")) or re.match(r"^\d{14}/", path) \
                else path
            inner = _embedded(path + (f"?{parts.query}" if parts.query else ""))
        elif bare.startswith("google.") and parts.path.startswith("/amp/s/"):
            inner = "https://" + parts.path[len("/amp/s/"):]
        elif bare.endswith(".cdn.ampproject.org") and re.match(r"^/[cv]/(s/)?", parts.path):
            m = re.match(r"^/[cv]/(s/)?(.+)$", parts.path)
            inner = ("https://" if m.group(1) else "http://") + m.group(2)  # type: ignore[union-attr]
        elif bare in _PROXIES:
            query = dict(parse_qsl(parts.query))
            inner = _embedded(parts.path.lstrip("/")) or next(
                (u for k in ("url", "q") if (u := _embedded(query.get(k, "")))), None)
        else:
            for site, (path, params) in _REDIRECTS.items():
                if bare == site or (site == "google.com" and bare.startswith("google.")):
                    if path is None or parts.path == path:
                        query = dict(parse_qsl(parts.query))
                        inner = next((u for k in params if (u := _embedded(query.get(k, "")))), None) \
                            or _embedded(parts.query)
                    break
        if not inner or inner == url:
            return url
        url = inner
    return url


def key(url: str | None) -> str | None:
    """What a link is known by: see the module's notes. None for anything
    that isn't a web address."""
    if not url:
        return None
    url = unwrap(url)
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        return None
    host = parts.hostname.lower()
    if host.startswith("amp.") and host.count(".") > 1:
        host = host[len("amp."):]
    path = _INDEX.sub("", _AMP_PATH.sub("", parts.path))
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                       if k.lower() not in TRACKING])
    netloc = host + (f":{parts.port}" if parts.port else "")
    return normalize_url(urlunsplit((parts.scheme, netloc, path or "/", query, "")))


def page_links(page: bytes | str, url: str) -> dict[str, str | None]:
    """What a page says about where it lives and who's talking about it: its
    own address (<link rel="canonical">, else og:url), its ActivityPub copy
    (a blog that federates: WordPress's, Ghost's, WriteFreely's) and its
    webmention endpoint. Home pages aren't taken as a page's own address: some
    sites point every page there."""
    found: dict[str, str | None] = {"canonical": None, "activitypub": None, "webmention": None}
    try:
        root = lxml.html.fromstring(page)
        root.make_links_absolute(url, resolve_base_href=True, handle_failures="ignore")
    except (ValueError, etree.ParserError):
        return found
    og: str | None = None
    for el in root.iter("link", "meta"):
        rel = {r.lower() for r in (el.get("rel") or "").split()}
        kind = (el.get("type") or "").lower()
        href = (el.get("href") if el.tag == "link" else el.get("content")) or ""
        href = href.strip()
        if not href.startswith(("http://", "https://")):
            continue
        if el.tag == "meta":
            if (el.get("property") or el.get("name") or "").lower() == "og:url":
                og = og or href
        elif "canonical" in rel:
            found["canonical"] = found["canonical"] or href
        elif "alternate" in rel and ("activity+json" in kind or ("ld+json" in kind and "activitystreams" in kind)):
            found["activitypub"] = found["activitypub"] or href
        elif "webmention" in rel:
            found["webmention"] = found["webmention"] or href
    own = [u for u in (found["canonical"], og) if u and urlsplit(u).path not in ("", "/")]
    found["canonical"] = own[0] if own else None
    return found


# --- probably the same story -------------------------------------------------------------

def simhash(text: str) -> int | None:
    """A 64-bit fingerprint of a text: texts that say nearly the same thing
    get fingerprints a few bits apart. None when it's too short to tell."""
    words = [w.lower() for w in _WORD.findall(text)][:MAX_WORDS]
    if len(words) < SHINGLE * 10:
        return None
    weights = [0] * SIMHASH_BITS
    for i in range(len(words) - SHINGLE + 1):
        h = int.from_bytes(hashlib.blake2b(" ".join(words[i:i + SHINGLE]).encode(), digest_size=8).digest(), "big")
        for bit in range(SIMHASH_BITS):
            weights[bit] += 1 if h >> bit & 1 else -1
    return sum(1 << bit for bit in range(SIMHASH_BITS) if weights[bit] > 0)


def bands(fingerprint: int) -> list[int]:
    """The fingerprint's parts, for finding near ones by any part being equal."""
    width = SIMHASH_BITS // BANDS
    return [fingerprint >> (i * width) & ((1 << width) - 1) for i in range(BANDS)]


def near(a: int, b: int) -> bool:
    return bin(a ^ b).count("1") <= NEAR_BITS


def title_key(title: str | None, site: str | None = None) -> str | None:
    """A headline, without the site's name on the end, case or punctuation;
    None when it's too short to be telling."""
    if not title:
        return None
    t = title.strip()
    if site and t.lower().endswith(site.lower()):
        t = re.sub(r"[\s|–—:-]+$", "", t[: -len(site)])
    t = _TITLE_TAIL.sub("", t)
    words = [w.lower() for w in _WORD.findall(t)]
    return " ".join(words) if len(words) >= MIN_TITLE_WORDS else None


def same_time(a: str | None, b: str | None) -> bool:
    """Published within SAME_DAYS of each other, or one of them undated."""
    da, db_ = parse_ts(a), parse_ts(b)
    return da is None or db_ is None or abs(da - db_) <= timedelta(days=SAME_DAYS)


def text_of(content_html: str | None) -> str:
    if not content_html:
        return ""
    try:
        return lxml.html.fragment_fromstring(content_html, create_parent="div").text_content()
    except (ValueError, etree.ParserError):
        return ""


def hex64(value: int | None) -> str | None:
    return f"{value:016x}" if value is not None else None


def unhex(value: Any) -> int | None:
    try:
        return int(value, 16) if value else None
    except (TypeError, ValueError):
        return None
