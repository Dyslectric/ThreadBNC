"""RSS and Atom feeds, followed like communities.

A feed is a "community" whose canonical id is ``rss:<feed url>``, and each
entry a "post" with the id ``rss:<entry guid, else link>``, all on the pseudo
server RSS_DOMAIN. Entries have no comments or votes. Article HTML is turned
into Markdown, so images in it are archived and show up in tiles like any
other post's.

Feeds only list their latest entries, so an entry that drops out of the feed
isn't gone: the adapter says so (`ages_out`) and the bouncer stops checking it
instead of recording it as missing.

Fetching is polite: conditional requests (ETag / Last-Modified), one fetch per
feed shared by every thread from it for FEED_CACHE seconds, the same
per-host spacing as everything else, a size cap, and no private addresses.
"""

from __future__ import annotations

import hashlib
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from html import escape, unescape
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from .. import youtube
from ..db import fmt_ts, parse_ts
from .base import (
    RSS_DOMAIN,
    RSS_PREFIX,
    CommentList,
    CommunityRef,
    NActor,
    NCommunity,
    NPost,
    RemoteNotFound,
    RemotePaused,
    RemoteUnavailable,
    ThreadiverseAdapter,
    ThreadRef,
)
from .http import HostThrottle

MAX_FEED_BYTES = 5_000_000
MAX_REDIRECTS = 5
FEED_CACHE = 300  # seconds one fetch of a feed serves every thread from it
ACCEPT = "application/rss+xml, application/atom+xml, application/xml;q=0.9, text/xml;q=0.9, */*;q=0.5"

ATOM = "{http://www.w3.org/2005/Atom}"
RSS1 = "{http://purl.org/rss/1.0/}"
CONTENT = "{http://purl.org/rss/1.0/modules/content/}encoded"
DC = "{http://purl.org/dc/elements/1.1/}"
MEDIA = "{http://search.yahoo.com/mrss/}"


# -- HTML -> Markdown -----------------------------------------------------------------

class _Markdown(HTMLParser):
    """Just enough HTML -> Markdown for article bodies: paragraphs, headings,
    emphasis, links, images, lists, quotes, code. Scripts and styles are
    dropped; anything else keeps only its text."""

    BLOCKS = {"p", "div", "section", "article", "header", "footer", "figure", "figcaption", "table", "tr",
              "main", "aside", "details", "summary", "dl", "dt", "dd"}
    DROP = {"script", "style", "iframe", "noscript", "svg", "form", "button", "template", "head", "title"}

    def __init__(self, base: str):
        super().__init__(convert_charrefs=True)
        self.base = base
        self.out: list[str] = []
        self.hrefs: list[str | None] = []
        self.lists: list[list[Any]] = []  # [tag, count]
        self.drop = 0
        self.pre = 0

    def _url(self, value: str | None) -> str | None:
        url = urljoin(self.base, (value or "").strip())
        return url if urlparse(url).scheme in ("http", "https") else None

    def _block(self) -> None:
        self.out.append("\n\n")

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.rsplit(":", 1)[-1]  # Atom's XHTML content comes as html:p etc.
        a = dict(attrs)
        if tag in self.DROP:
            self.drop += 1
        if self.drop:
            return
        if tag in self.BLOCKS:
            self._block()
        elif tag == "br":
            self.out.append("  \n")
        elif re.fullmatch(r"h[1-6]", tag):
            self._block()
            self.out.append("#" * int(tag[1]) + " ")
        elif tag in ("strong", "b"):
            self.out.append("**")
        elif tag in ("em", "i"):
            self.out.append("*")
        elif tag == "a":
            self.hrefs.append(self._url(a.get("href")))
            self.out.append("[")
        elif tag == "img":
            src = self._url(a.get("src") or a.get("data-src"))
            if src:
                alt = (a.get("alt") or "").replace("[", "").replace("]", "")
                self.out.append(f"![{alt}]({src})")
        elif tag in ("ul", "ol"):
            self._block()
            self.lists.append([tag, 0])
        elif tag == "li":
            depth = max(len(self.lists) - 1, 0)
            if self.lists and self.lists[-1][0] == "ol":
                self.lists[-1][1] += 1
                bullet = f"{self.lists[-1][1]}. "
            else:
                bullet = "- "
            self.out.append("\n" + "  " * depth + bullet)
        elif tag == "blockquote":
            self._block()
            self.out.append("> ")
        elif tag == "pre":
            self._block()
            self.out.append("```\n")
            self.pre += 1
        elif tag == "code" and not self.pre:
            self.out.append("`")
        elif tag == "hr":
            self.out.append("\n\n---\n\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.rsplit(":", 1)[-1]
        if tag in self.DROP:
            self.drop = max(self.drop - 1, 0)
            return
        if self.drop:
            return
        if tag in self.BLOCKS or re.fullmatch(r"h[1-6]", tag) or tag == "blockquote":
            self._block()
        elif tag in ("strong", "b"):
            self.out.append("**")
        elif tag in ("em", "i"):
            self.out.append("*")
        elif tag == "a":
            href = self.hrefs.pop() if self.hrefs else None
            self.out.append(f"]({href})" if href else "]")
        elif tag in ("ul", "ol"):
            if self.lists:
                self.lists.pop()
            self._block()
        elif tag == "pre":
            self.pre = max(self.pre - 1, 0)
            self.out.append("\n```\n\n")
        elif tag == "code" and not self.pre:
            self.out.append("`")

    def handle_data(self, data: str) -> None:
        if self.drop:
            return
        if self.pre:
            self.out.append(data)
            return
        text = re.sub(r"\s+", " ", data)
        self.out.append(re.sub(r"([\\`*_\[\]])", r"\\\1", text))

    def markdown(self) -> str:
        text = "".join(self.out)
        text = re.sub(r"[ \t]+\n", lambda m: "  \n" if m.group(0).startswith("  ") else "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return "\n".join(line.rstrip() if not line.endswith("  ") else line for line in text.strip().splitlines())


def html_to_markdown(html: str | None, base: str = "") -> str:
    if not html or not html.strip():
        return ""
    parser = _Markdown(base)
    parser.feed(html)
    parser.close()
    return parser.markdown()


def _plain(text: str | None) -> str:
    """Text of something that may contain markup (Atom titles can be HTML)."""
    return " ".join(unescape(re.sub(r"<[^>]+>", " ", text or "")).split())


# -- parsing -----------------------------------------------------------------------------

@dataclass
class Entry:
    key: str
    link: str | None
    title: str
    html: str
    published: str | None
    updated: str | None
    author: str | None
    thumb: str | None


@dataclass
class Feed:
    url: str
    title: str
    link: str | None
    description: str | None
    entries: list[Entry] = field(default_factory=list)


def _date(value: str | None) -> str | None:
    value = (value or "").strip()
    if not value:
        return None
    dt = parse_ts(value)
    if dt is None:
        try:
            dt = parsedate_to_datetime(value)  # RSS uses RFC 822 dates
        except (TypeError, ValueError, IndexError):
            return None
        if dt.tzinfo is None:
            from datetime import timezone
            dt = dt.replace(tzinfo=timezone.utc)
    return fmt_ts(dt)


def _t(el: ET.Element | None, path: str) -> str:
    found = el.find(path) if el is not None else None
    return (found.text or "").strip() if found is not None and found.text else ""


def _thumb(item: ET.Element, base: str) -> str | None:
    """media:thumbnail, an image media:content or an image enclosure."""
    for el in [*item.iter(MEDIA + "thumbnail"), *item.iter(MEDIA + "content"), *item.iter("enclosure"),
               *item.findall(ATOM + "link")]:
        url = el.get("url") or (el.get("href") if el.get("rel") == "enclosure" else None)
        kind = (el.get("type") or el.get("medium") or ("image" if el.tag == MEDIA + "thumbnail" else "")).lower()
        if url and kind.startswith("image"):
            return urljoin(base, url)
    return None


def _key(guid: str, link: str | None, title: str, published: str | None) -> str:
    if guid:
        return guid
    if link:
        return link
    return "sha1:" + hashlib.sha1(f"{title}\n{published}".encode()).hexdigest()


def _rss_item(item: ET.Element, base: str, ns: str = "") -> Entry:
    link = urljoin(base, _t(item, ns + "link")) or None
    title = _plain(_t(item, ns + "title"))
    published = _date(_t(item, ns + "pubDate") or _t(item, DC + "date"))
    html = _t(item, CONTENT) or _t(item, ns + "description")
    return Entry(key=_key(_t(item, "guid"), link, title, published), link=link, title=title, html=html,
                 published=published, updated=None, author=_t(item, DC + "creator") or _t(item, "author") or None,
                 thumb=_thumb(item, base))


def _atom_text(el: ET.Element | None) -> str:
    """An Atom text construct as HTML."""
    if el is None:
        return ""
    kind = el.get("type", "text")
    if kind == "xhtml":
        return "".join(ET.tostring(child, encoding="unicode") for child in el)
    return (el.text or "") if kind == "html" else escape(el.text or "")


def _atom_link(el: ET.Element, base: str) -> str | None:
    links = el.findall(ATOM + "link")
    chosen = next((l for l in links if l.get("rel", "alternate") == "alternate"), links[0] if links else None)
    return urljoin(base, chosen.get("href", "")) if chosen is not None and chosen.get("href") else None


def _atom_entry(entry: ET.Element, base: str) -> Entry:
    link = _atom_link(entry, base)
    title = _plain(_atom_text(entry.find(ATOM + "title")))
    published = _date(_t(entry, ATOM + "published"))
    updated = _date(_t(entry, ATOM + "updated"))
    html = _atom_text(entry.find(ATOM + "content")) or _atom_text(entry.find(ATOM + "summary"))
    if not html:  # YouTube: the video's description, as plain text
        described = _t(entry, f"{MEDIA}group/{MEDIA}description")
        html = "<br>".join(escape(line) for line in described.splitlines())
    return Entry(key=_key(_t(entry, ATOM + "id"), link, title, published or updated), link=link, title=title,
                 html=html, published=published or updated, updated=updated if updated != published else None,
                 author=_t(entry, f"{ATOM}author/{ATOM}name") or None, thumb=_thumb(entry, base))


def parse_feed(data: bytes, url: str) -> Feed | None:
    """An RSS 2.0, RSS 1.0 (RDF) or Atom feed, or None if `data` isn't one."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return None
    if root.tag == "rss":
        ch = root.find("channel")
        link = _t(ch, "link") or None
        return Feed(url, _plain(_t(ch, "title")), link, _plain(_t(ch, "description")) or None,
                    [_rss_item(i, link or url) for i in (ch.findall("item") if ch is not None else [])])
    if root.tag == ATOM + "feed":
        link = _atom_link(root, url)
        return Feed(url, _plain(_atom_text(root.find(ATOM + "title"))), link,
                    _plain(_atom_text(root.find(ATOM + "subtitle"))) or None,
                    [_atom_entry(e, link or url) for e in root.findall(ATOM + "entry")])
    if root.tag.endswith("RDF"):
        ch = root.find(RSS1 + "channel")
        link = _t(ch, RSS1 + "link") or None
        return Feed(url, _plain(_t(ch, RSS1 + "title")), link, _plain(_t(ch, RSS1 + "description")) or None,
                    [_rss_item(i, link or url, RSS1) for i in root.findall(RSS1 + "item")])
    return None


class _FeedLinks(HTMLParser):
    """<link rel="alternate" type="application/rss+xml" href="..."> in a web page."""

    def __init__(self) -> None:
        super().__init__()
        self.found: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: (v or "") for k, v in attrs}
        if tag == "link" and "alternate" in a.get("rel", "").lower().split() and a.get("href") and \
                a.get("type", "").lower() in ("application/rss+xml", "application/atom+xml", "application/rdf+xml"):
            self.found.append(a["href"])


def discover(page: bytes, base: str) -> str | None:
    """The feed a web page advertises, if any."""
    finder = _FeedLinks()
    try:
        finder.feed(page.decode("utf-8", "replace"))
    except Exception:  # a broken page just has no feed
        return None
    return urljoin(base, finder.found[0]) if finder.found else None


# -- fetching ------------------------------------------------------------------------------

class FeedFetcher:
    def __init__(self, user_agent: str, timeout: float = 20.0, throttle: HostThrottle | None = None,
                 check_host: bool = True, transport: httpx.BaseTransport | None = None):
        self.client = httpx.Client(timeout=timeout, transport=transport, follow_redirects=False,
                                   headers={"User-Agent": user_agent, "Accept": ACCEPT})
        self.throttle = throttle or HostThrottle(1.0)
        self.check_host = check_host
        self._cache: dict[str, tuple[float, str | None, str | None, Feed]] = {}

    def _get(self, url: str, headers: dict[str, str]) -> tuple[int, str, bytes, httpx.Headers]:
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            if self.check_host:
                from ..media import MediaRejected, _assert_public_host
                try:
                    _assert_public_host(current)
                except MediaRejected as exc:
                    raise RemoteNotFound(f"{current}: {exc}") from exc
                except httpx.HTTPError as exc:
                    raise RemoteUnavailable(f"{current}: {exc}") from exc
            host = (urlparse(current).hostname or "").lower()
            self.throttle.wait(host)
            sent = {**headers, "Cookie": youtube.CONSENT_COOKIE} if youtube.is_youtube_host(host) else headers
            try:
                with self.client.stream("GET", current, headers=sent) as resp:
                    self.throttle.note(host, resp.status_code, resp.headers)
                    if resp.status_code in (301, 302, 303, 307, 308) and "location" in resp.headers:
                        current = urljoin(current, resp.headers["location"])
                        continue
                    if resp.status_code == 429:
                        raise RemotePaused(f"{host}: HTTP 429 (too many requests)", self.throttle.paused_for(host))
                    body = b""
                    if resp.status_code == 200:
                        for chunk in resp.iter_bytes(65536):
                            body += chunk
                            if len(body) > MAX_FEED_BYTES:
                                raise RemoteUnavailable(f"{url}: feed larger than {MAX_FEED_BYTES // 1_000_000} MB")
                    return resp.status_code, current, body, resp.headers
            except httpx.HTTPError as exc:
                raise RemoteUnavailable(f"{urlparse(current).hostname}: {type(exc).__name__}: {exc}") from exc
        raise RemoteUnavailable(f"{url}: too many redirects")

    def feed(self, url: str, find: bool = False) -> Feed:
        """The feed at `url`, from the cache when it's fresh. With `find`, a
        web page's advertised feed is followed instead, and a YouTube
        channel's link leads to the channel's feed."""
        rewritten = youtube.feed_url(url)
        if rewritten and rewritten != url:
            return self.feed(rewritten)
        page = youtube.channel_page(url) if find else None
        if page:
            return self.feed(self._youtube_channel_feed(page))
        cached = self._cache.get(url)
        if cached and time.monotonic() - cached[0] < FEED_CACHE:
            return cached[3]
        listing = youtube.page_url(url)
        if listing:  # YouTube's feeds are unreliable: read the page they'd list instead
            return self._youtube_page(url, listing)
        headers = {}
        if cached and cached[1]:
            headers["If-None-Match"] = cached[1]
        if cached and cached[2]:
            headers["If-Modified-Since"] = cached[2]
        status, final, body, resp_headers = self._get(url, headers)
        if status == 304 and cached:
            self._cache[url] = (time.monotonic(), *cached[1:])
            return cached[3]
        if status in (404, 410):
            raise RemoteNotFound(f"{url}: HTTP {status}")
        if status != 200:
            raise RemoteUnavailable(f"{url}: HTTP {status}")
        parsed = parse_feed(body, final)
        if parsed is None:
            advertised = discover(body, final) if find else None
            if advertised and advertised != url:
                return self.feed(advertised)
            why = ", and the page doesn't link one" if find else ""
            raise RemoteNotFound(f"{url} isn't an RSS or Atom feed{why}")
        parsed.url = url
        if len(self._cache) > 500:
            self._cache.clear()
        self._cache[url] = (time.monotonic(), resp_headers.get("etag"), resp_headers.get("last-modified"), parsed)
        return parsed

    def _youtube_page(self, url: str, page: str) -> Feed:
        """A YouTube feed's videos, from the channel's Videos tab or the
        playlist's page (youtube.py). Entries get the ids the feed uses, so
        nothing is doubled if the feed's own entries are ever read again."""
        status, _, body, _ = self._get(page, {"Accept": "text/html", "Accept-Language": "en"})
        if status in (404, 410):
            raise RemoteNotFound(f"{page}: HTTP {status}")
        if status != 200:
            raise RemoteUnavailable(f"{page}: HTTP {status}")
        listed = youtube.parse_page(body.decode("utf-8", "replace"), page)
        if listed is None:
            raise RemoteUnavailable(f"{page}: no video list on the page (YouTube may have changed it)")
        entries = [Entry(key=f"yt:video:{v.id}", link=youtube.watch_url(v.id), title=v.title, html="",
                         published=fmt_ts(v.published), updated=None, author=v.author,
                         thumb=youtube.thumbnail_url(v.id)) for v in listed.videos]
        parsed = Feed(url, listed.title, listed.link, listed.description, entries)
        if len(self._cache) > 500:
            self._cache.clear()
        self._cache[url] = (time.monotonic(), None, None, parsed)
        return parsed

    def _youtube_channel_feed(self, page: str) -> str:
        """The feed of the channel an @handle or /c/ page is, read from the page
        (once, when it's followed)."""
        status, _, body, _ = self._get(page, {"Accept": "text/html"})
        if status in (404, 410):
            raise RemoteNotFound(f"{page}: no such YouTube channel")
        if status != 200:
            raise RemoteUnavailable(f"{page}: HTTP {status}")
        channel = youtube.channel_id_in(body.decode("utf-8", "replace"))
        if not channel:
            raise RemoteNotFound(f"{page}: couldn't find the channel's id on its page; "
                                 "try its https://www.youtube.com/channel/UC… link")
        return f"{youtube.FEED}?channel_id={channel}"


# -- the adapter ----------------------------------------------------------------------------

def community_of(feed: Feed) -> NCommunity:
    name = feed.title or urlparse(feed.link or feed.url).hostname or feed.url
    return NCommunity(ap_id=RSS_PREFIX + feed.url, name=name[:80], domain=RSS_DOMAIN, title=feed.title or None,
                      local_id=feed.url, description=feed.description)


class RssAdapter(ThreadiverseAdapter):
    software = "rss"
    ages_out = True  # entries leave the feed as new ones arrive; that's not deletion
    poll_page_size = 100_000  # a feed is one page
    max_poll_pages = 1

    def __init__(self, fetcher: FeedFetcher):
        super().__init__(RSS_DOMAIN)
        self.fetcher = fetcher

    def _post(self, feed: Feed, e: Entry) -> NPost:
        body = html_to_markdown(e.html, e.link or feed.link or feed.url)
        host = urlparse(feed.link or feed.url).hostname or RSS_DOMAIN
        who = e.author or feed.title or host
        return NPost(
            ap_id=RSS_PREFIX + e.key,
            local_id=f"{feed.url} {e.key}",  # the feed to look in, and the entry
            title=e.title or (_plain(e.html)[:80] + "…" if e.html else "(untitled)"),
            body=body or None,
            url=e.link,
            created_at=e.published,
            updated_at=e.updated,
            deleted=False, removed=False, locked=False,
            community=community_of(feed),
            author=NActor(f"{RSS_PREFIX}{feed.url}#author={who}", who, host),
            thumbnail_url=e.thumb,
        )

    def resolve_url(self, ref: ThreadRef) -> str:
        raise RemoteNotFound("RSS articles are kept by following their feed")

    def resolve_ap_id(self, ap_id: str) -> str | None:
        return None

    def fetch_community(self, ref: CommunityRef) -> NCommunity:
        return community_of(self.fetcher.feed(ref.name, find=True))

    def list_community_posts(self, ref: CommunityRef, sort: str = "New", page: int = 1,
                             limit: int = 20) -> list[NPost]:
        if page > 1:
            return []
        feed = self.fetcher.feed(ref.name)
        posts = [self._post(feed, e) for e in feed.entries]
        return sorted(posts, key=lambda p: p.created_at or "", reverse=True)

    def fetch_post(self, local_id: str) -> NPost:
        feed_url, _, key = local_id.partition(" ")
        feed = self.fetcher.feed(feed_url)
        entry = next((e for e in feed.entries if e.key == key), None)
        if entry is None:
            raise RemoteNotFound(f"{key} is no longer in {feed_url}")
        return self._post(feed, entry)

    def fetch_comments(self, post_local_id: str) -> CommentList:
        out = CommentList()
        out.complete = False  # feeds have no comments; nothing can go missing
        return out
