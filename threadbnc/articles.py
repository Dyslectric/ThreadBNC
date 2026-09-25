"""Linked articles: a readable copy of the web page a post links to.

A post's link is registered when its revision is recorded (like media), and
the page is downloaded when the post is opened or kept (Bouncer.open_threads)
or scrolled into view in a feed (Bouncer.fetch_articles), never in the
background, and the article pulled out of it with
trafilatura: the text, headings, lists, quotes, links and pictures, without the
site's navigation, ads and scripts. The pictures are registered as the post's
media, so they follow its community's media settings and go when it's purged.

Not every link can be read. Links to pictures, videos, social media, home pages
and other threadiverse posts aren't tried; paywalls, sites that refuse the
bouncer and pages with too little text are given up on, and the post just links
to the original as before.

Different links to the same page find each other by their keys (links.py): the
link as posted, where it was read from and where the page says it lives. So an
article lists every post of it here, whichever link each used, and the articles
that are probably the same story; discussions.py adds what it found elsewhere.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Callable
from urllib.parse import urldefrag, urljoin, urlparse

import httpx
import lxml.html
import nh3
import trafilatura
from markupsafe import Markup

from . import links as links_mod
from .adapters.base import RemotePaused, host_of, is_reddit_host, parse_thread_url
from .adapters.http import HostThrottle
from .db import Conn, Database, fmt_ts, parse_ts, utcnow
from .media import MAX_ATTEMPTS, MAX_REDIRECTS, MediaRejected, _after, _assert_public_host
from .media import media_id
from .media import register as register_media
from .render import (ALLOWED_ATTRS, ALLOWED_TAGS, VIDEO_HREF, VIDEO_LINK_TITLE, MediaLookup, VideoTitles, _media_html,
                     looks_like_media, video_link_text)
from .livestream import stream_of
from .videos import video_of
from .youtube import video_id

MAX_PAGE_BYTES = 5_000_000
CARD_IMAGE_BYTES = 1_000_000  # Bluesky's limit for a link card's picture
STANDALONE_DAYS = 30  # an article last opened from a link this long ago, not kept, no post linking to it, goes
MIN_WORDS = 80  # less than this is a teaser, a paywall or not an article
CHUNK = 64 * 1024

# Sites whose pages are players, feeds or apps rather than articles.
SKIP_HOSTS = ("youtube.com", "youtu.be", "imgur.com", "twitter.com", "x.com", "instagram.com", "tiktok.com",
              "twitch.tv", "vimeo.com", "bsky.app", "facebook.com", "streamable.com", "redgifs.com",
              "catbox.moe", "discord.com", "discord.gg", "spotify.com", "soundcloud.com")

TAGS = ALLOWED_TAGS | {"figure", "figcaption", "i", "b", "u", "mark", "small"}


class ArticleRejected(MediaRejected):
    """Permanent: the page can't be read, or isn't an article."""


class ArticleSkipped(ArticleRejected):
    """Not an article: not a web page, or too little text on it."""


def candidate(url: str | None) -> str | None:
    """The link, if it might be an article worth reading here."""
    if not url or looks_like_media(url) or video_of(url):  # (a video's page shows its player instead)
        return None
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in ("http", "https") or not host or parsed.path in ("", "/"):
        return None
    if is_reddit_host(host) or any(host == h or host.endswith("." + h) for h in SKIP_HOSTS):
        return None
    try:
        parse_thread_url(url)  # a link to a Lemmy or PieFed post
        return None
    except ValueError:
        return url


_DATED = re.compile(r"/(?:19|20)\d\d/")
_PAGE_EXT = (".html", ".htm", ".shtml", ".php", ".asp", ".aspx")


def looks_like_article(url: str | None) -> bool:
    """A link inside an article that is probably an article itself, to read
    here: not just a page we'd read (candidate), but one whose address looks
    like a story (a dated path, a slug, an id, a .html page) rather than a
    section, tag or home page."""
    if not candidate(url):
        return False
    path = urlparse(url).path.rstrip("/")
    last = path.rsplit("/", 1)[-1].lower()
    return bool(_DATED.search(path + "/") or last.endswith(_PAGE_EXT) or re.search(r"\d{4,}", last)
                or last.count("-") >= 2 or last.count("_") >= 2)


# --- registration (inside store transactions) ------------------------------

def register(conn: Conn, object_id: int, url: str | None, now: str) -> None:
    url = candidate(url)
    if not url:
        return
    conn.execute("INSERT INTO articles(url, first_seen_at, next_attempt_at) VALUES (?,?,?) "
                 "ON CONFLICT(url) DO NOTHING", (url, now, now))
    a = conn.execute("SELECT * FROM articles WHERE url=?", (url,)).fetchone()
    add_keys(conn, a["id"], [url])
    conn.execute("INSERT INTO article_refs(object_id, article_id, first_seen_at) VALUES (?,?,?) "
                 "ON CONFLICT(object_id, article_id) DO NOTHING", (object_id, a["id"], now))
    if a["status"] == "ok":  # already read for another post: its pictures are this one's too
        register_media(conn, object_id, pictures(a), now, from_article=True)


def register_all_existing(conn: Conn) -> None:
    """Backfill links posted before articles were archived, and the article
    pictures recorded before articles had their own."""
    now = utcnow()
    for r in conn.execute("SELECT r.object_id, r.url FROM revisions r JOIN objects o ON o.id=r.object_id "
                          "WHERE o.object_type='post' AND r.url IS NOT NULL").fetchall():
        register(conn, r["object_id"], r["url"], now)
    for a in conn.execute("SELECT id, images_json FROM articles a WHERE status='ok' AND images_json IS NOT NULL "
                          "AND NOT EXISTS (SELECT 1 FROM article_media m WHERE m.article_id=a.id)").fetchall():
        attach_pictures(conn, a["id"], pictures(a), now)
    for a in conn.execute("SELECT id, content_html FROM articles a WHERE status='ok' "
                          "AND NOT EXISTS (SELECT 1 FROM article_links l WHERE l.article_id=a.id)").fetchall():
        record_links(conn, a["id"], a["content_html"])
    # Keys and fingerprints for articles read before links were matched by them.
    for a in conn.execute("SELECT id, url, fetched_from, canonical_url FROM articles a WHERE NOT EXISTS "
                          "(SELECT 1 FROM article_keys k WHERE k.article_id=a.id)").fetchall():
        add_keys(conn, a["id"], [a["url"], a["fetched_from"], a["canonical_url"]])
    for r in conn.execute("SELECT article_id, url FROM article_links WHERE link_key IS NULL").fetchall():
        conn.execute("UPDATE article_links SET link_key=? WHERE article_id=? AND url=?",
                     (links_mod.key(r["url"]) or "", r["article_id"], r["url"]))
    for a in conn.execute("SELECT id, title, site_name, content_html FROM articles WHERE status='ok' "
                          "AND simhash IS NULL").fetchall():
        fingerprint(conn, a["id"], a["title"], a["site_name"], a["content_html"])


def links_in(content_html: str | None) -> set[str]:
    """The pages an article links to (ones we might read), without #fragments."""
    if not content_html:
        return set()
    root = lxml.html.fragment_fromstring(content_html, create_parent="div")
    return {url for a in root.iter("a") if (url := candidate(urldefrag((a.get("href") or "").strip())[0]))}


def record_links(conn: Conn, article_id: int, content_html: str | None) -> None:
    """Which pages the article links to, so an article can list the ones that mention it."""
    conn.execute("DELETE FROM article_links WHERE article_id=?", (article_id,))
    conn.executemany("INSERT INTO article_links(article_id, url, link_key) VALUES (?,?,?)",
                     [(article_id, url, links_mod.key(url) or "") for url in links_in(content_html)])


def add_keys(conn: Conn, article_id: int, urls: list[str | None]) -> None:
    """Record what an article is known by, from these addresses of it."""
    for k in {links_mod.key(u) for u in urls if u} - {None}:
        conn.execute("INSERT INTO article_keys(article_id, key) VALUES (?,?) ON CONFLICT(article_id, key) DO NOTHING",
                     (article_id, k))


def keys_of(conn: Conn, article_id: int) -> list[str]:
    return [r[0] for r in conn.execute("SELECT key FROM article_keys WHERE article_id=?", (article_id,))]


def same_page(conn: Conn, article_id: int) -> list[int]:
    """This article and the others that are the same page, reached by another link."""
    ids = {r[0] for r in conn.execute(
        "SELECT DISTINCT k2.article_id FROM article_keys k1 JOIN article_keys k2 ON k2.key=k1.key "
        "WHERE k1.article_id=?", (article_id,))}
    return sorted(ids | {article_id})


def _marks(values: list[Any]) -> str:
    return ",".join("?" * len(values))


def mentioned_by(conn: Conn, a: Any, limit: int = 30) -> list[Any]:
    """Other articles here that link to this one, by any of its addresses."""
    keys = keys_of(conn, a["id"])
    if not keys:
        return []
    same = same_page(conn, a["id"])
    return conn.execute(
        f"SELECT DISTINCT o.id, o.title, o.site_name, o.url, o.fetched_at FROM article_links l "
        f"JOIN articles o ON o.id=l.article_id AND o.status='ok' "
        f"WHERE l.link_key IN ({_marks(keys)}) AND o.id NOT IN ({_marks(same)}) ORDER BY o.fetched_at DESC LIMIT ?",
        (*keys, *same, limit)).fetchall()


def posted_in(conn: Conn, article_ids: list[int], exclude: list[int] | None = None, limit: int = 20) -> list[Any]:
    """The threads here whose post links to one of these articles, newest
    first, leaving out the `exclude`d threads."""
    exclude = exclude or [0]
    return conn.execute(
        f"SELECT DISTINCT t.id, r.title, c.name, c.canonical_ap_id, o.created_at, o.canonical_ap_id AS ap_id "
        f"FROM article_refs ar "
        f"JOIN archived_threads t ON t.root_object_id=ar.object_id AND t.trashed_at IS NULL "
        f"JOIN objects o ON o.id=t.root_object_id JOIN revisions r ON r.object_id=o.id AND r.seq=o.revision_count "
        f"JOIN communities c ON c.id=t.community_id "
        f"WHERE ar.article_id IN ({_marks(article_ids)}) AND t.id NOT IN ({_marks(exclude)}) "
        f"ORDER BY o.created_at DESC LIMIT ?",
        (*article_ids, *exclude, limit)).fetchall()


def fingerprint(conn: Conn, article_id: int, title: str | None, site: str | None,
                content_html: str | None) -> None:
    """Record what tells "probably the same story": the text's fingerprint
    (in parts, to look up by) and the headline. '' when there's none."""
    value = links_mod.simhash(links_mod.text_of(content_html))
    conn.execute("UPDATE articles SET simhash=?, title_key=? WHERE id=?",
                 (links_mod.hex64(value) or "", links_mod.title_key(title, site) or "", article_id))
    conn.execute("DELETE FROM article_prints WHERE article_id=?", (article_id,))
    if value is not None:
        conn.executemany("INSERT INTO article_prints(article_id, band, value) VALUES (?,?,?)",
                         [(article_id, i, part) for i, part in enumerate(links_mod.bands(value))])


def same_story(conn: Conn, a: Any, limit: int = 10) -> list[Any]:
    """Other articles here that are probably the same story, on another page:
    nearly the same text, or the same headline at about the same time."""
    same = same_page(conn, a["id"])
    found: dict[int, Any] = {}
    cols = "o.id, o.title, o.site_name, o.url, o.fetched_from, o.simhash, o.published"
    mine = links_mod.unhex(a["simhash"])
    if mine is not None:
        parts = links_mod.bands(mine)
        rows = conn.execute(
            f"SELECT DISTINCT {cols} FROM article_prints p JOIN articles o ON o.id=p.article_id AND o.status='ok' "
            f"WHERE " + " OR ".join("(p.band=? AND p.value=?)" for _ in parts),
            [v for i, part in enumerate(parts) for v in (i, part)]).fetchall()
        found.update((r["id"], r) for r in rows if links_mod.near(mine, links_mod.unhex(r["simhash"]) or 0))
    if a["title_key"]:
        for r in conn.execute(f"SELECT {cols} FROM articles o WHERE o.title_key=? AND o.status='ok'",
                              (a["title_key"],)).fetchall():
            if links_mod.same_time(a["published"], r["published"]):
                found.setdefault(r["id"], r)
    return [r for i, r in sorted(found.items(), reverse=True) if i not in same][:limit]


def attach_pictures(conn: Conn, article_id: int, urls: list[str], now: str) -> None:
    for url in urls:
        conn.execute("INSERT INTO article_media(article_id, media_id) VALUES (?,?) "
                     "ON CONFLICT(article_id, media_id) DO NOTHING", (article_id, media_id(conn, url, now)))


def collect_orphans(conn: Conn, now: str | None = None) -> int:
    """Delete articles nothing wants any more: no post links to them, they
    weren't kept, and they weren't opened from a link in the last
    STANDALONE_DAYS. Their pictures go with the media, once nothing else uses them."""
    cutoff = fmt_ts(parse_ts(now or utcnow()) - timedelta(days=STANDALONE_DAYS))  # type: ignore[operator]
    unwanted = ("NOT EXISTS (SELECT 1 FROM article_refs r WHERE r.article_id=articles.id) AND kept_at IS NULL "
                "AND (opened_at IS NULL OR opened_at < ?)")
    for table in ("article_media", "article_links", "article_keys", "article_prints", "discussions",
                  "discussion_checks"):
        conn.execute(f"DELETE FROM {table} WHERE article_id IN (SELECT id FROM articles WHERE {unwanted})",
                     (cutoff,))
    return conn.execute(f"DELETE FROM articles WHERE {unwanted}", (cutoff,)).rowcount


def ensure(conn: Conn, url: str, now: str) -> Any:
    """The article for a link (registered to be read if it's new), for reading
    one on demand: a link inside another article."""
    conn.execute("INSERT INTO articles(url, first_seen_at, next_attempt_at) VALUES (?,?,?) "
                 "ON CONFLICT(url) DO NOTHING", (url, now, now))
    conn.execute("UPDATE articles SET opened_at=? WHERE url=?", (now, url))
    a = conn.execute("SELECT * FROM articles WHERE url=?", (url,)).fetchone()
    add_keys(conn, a["id"], [url])
    return a


def media_lookup(conn: Conn, article_id: int) -> dict[str, Any]:
    """The article's pictures, by URL, as the renderer wants them."""
    from .render import MediaInfo
    return {r["url"]: MediaInfo(r["id"], r["status"], r["content_type"], r["error"]) for r in conn.execute(
        "SELECT m.* FROM media m JOIN article_media a ON a.media_id=m.id WHERE a.article_id=?", (article_id,))}


def keep(conn: Conn, article_id: int, kept: bool, now: str) -> None:
    conn.execute("UPDATE articles SET kept_at=? WHERE id=?", (now if kept else None, article_id))


def pictures(a: Any) -> list[str]:
    return json.loads(a["images_json"] or "[]")


# --- reading ----------------------------------------------------------------

def for_object(conn: Conn, object_id: int, url: str | None = None) -> Any:
    """A post's article: the one for its current link if there is one, else
    one it linked to before."""
    return conn.execute(
        "SELECT a.* FROM article_refs r JOIN articles a ON a.id=r.article_id WHERE r.object_id=? "
        "ORDER BY CASE WHEN a.url=? THEN 0 ELSE 1 END, CASE WHEN a.status='ok' THEN 0 ELSE 1 END, a.id DESC "
        "LIMIT 1", (object_id, url or "")).fetchone()


def readable(conn: Conn, object_ids: list[int]) -> set[int]:
    """The posts, of these, that have an article to read."""
    if not object_ids:
        return set()
    marks = ",".join("?" * len(object_ids))
    return {r[0] for r in conn.execute(
        f"SELECT r.object_id FROM article_refs r JOIN articles a ON a.id=r.article_id "
        f"WHERE a.status='ok' AND r.object_id IN ({marks})", object_ids)}


def waiting(conn: Conn, links: list[tuple[int, str | None]]) -> set[int]:
    """The posts, of these (object id, current link), whose article isn't read
    yet, or is but has pictures still to download: what showing one in the
    feed fetches (Bouncer.fetch_articles)."""
    links = [(oid, url) for oid, url in links if candidate(url)]
    if not links:
        return set()
    pairs = " OR ".join("(r.object_id=? AND a.url=?)" for _ in links)
    return {r[0] for r in conn.execute(
        f"SELECT r.object_id FROM article_refs r JOIN articles a ON a.id=r.article_id WHERE ({pairs}) "
        f"AND (a.status='pending' OR (a.status='ok' AND EXISTS (SELECT 1 FROM article_media am "
        f"JOIN media m ON m.id=am.media_id WHERE am.article_id=a.id AND m.status='pending' AND m.held=0)))",
        [v for pair in links for v in pair])}


def render(content_html: str | None, lookup: MediaLookup, page_url: str | None = None,
           read_link: Callable[[str], str] | None = None, titles: VideoTitles | None = None) -> Markup:
    """Stored article HTML with its pictures swapped for the archived copies.
    With `read_link`, links to other articles go to reading them here (marked
    with the article-link class); other links open the site in a new tab.
    Links to livestreams open their player here (livestream.py), and links
    to YouTube videos open their box here, titled by `titles` where
    their text is just the address (as in posts, see render._video_links);
    so do links to other videos (videos.py)."""
    if not content_html:
        return Markup("")
    root = lxml.html.fragment_fromstring(content_html, create_parent="div")
    for img in list(root.iter("img")):
        src = img.get("src") or ""
        new = lxml.html.fragment_fromstring(_media_html(src, img.get("alt") or "", lookup(src)))
        new.tail = img.tail
        img.getparent().replace(img, new)
    here = urldefrag(page_url or "")[0]
    for a in root.iter("a"):
        href = a.get("href") or ""
        stream = stream_of(href)
        vid = video_id(href)
        video = None if vid else video_of(href)
        if stream and not a.get("class"):
            a.set("href", stream.href)
            a.set("class", "live-link")
            a.set("title", stream.link_title)
            a.attrib.pop("target", None)
        elif vid and not a.get("class"):
            a.set("href", VIDEO_HREF.format(vid))
            a.set("class", "video-link")
            a.set("title", VIDEO_LINK_TITLE)
            a.attrib.pop("target", None)
            if video_link_text(a.text_content(), href):
                title = titles(vid) if titles else None
                if title:
                    for child in list(a):
                        a.remove(child)
                    a.text = title
                else:
                    a.set("class", "video-link untitled")
        elif video and not a.get("class"):
            a.set("href", video.href)
            a.set("class", "video-link")
            a.set("title", video.link_title)
            a.attrib.pop("target", None)
        elif read_link and urldefrag(href)[0] != here and looks_like_article(href) and not a.get("class"):
            a.set("href", read_link(href))
            a.set("class", "article-link")
            a.set("title", f"Read here · {host_of(href)}")
            a.attrib.pop("target", None)
        elif not href.startswith("/"):
            a.set("target", "_blank")
    return Markup(_clean(lxml.html.tostring(root, encoding="unicode")))


def excerpt(content_html: str | None, limit: int = 300) -> str:
    """The start of an article's text, for sharing it: whole paragraphs, up
    to about `limit` characters."""
    if not content_html:
        return ""
    out, total = [], 0
    for p in lxml.html.fragment_fromstring(content_html, create_parent="div").iter("p"):
        text = " ".join(p.text_content().split())
        if not text:
            continue
        if total and total + len(text) > limit:
            break
        out.append(text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + "…")
        total += len(text)
    return "\n\n".join(out)


def _clean(html: str) -> str:
    return nh3.clean(html, tags=TAGS, attributes=ALLOWED_ATTRS,
                     url_schemes={"http", "https", "mailto"}, link_rel="noopener noreferrer nofollow",
                     strip_comments=True, filter_style_properties={"text-align"})


# --- extraction ---------------------------------------------------------------

@dataclass
class Extracted:
    title: str | None
    byline: str | None
    site_name: str | None
    published: str | None
    content_html: str
    images: list[str]
    lead_image: str | None
    words: int


def extract(page: bytes | str, url: str) -> Extracted:
    options = dict(url=url, include_images=True, include_links=True, include_formatting=True,
                   include_comments=False, include_tables=True)
    out = trafilatura.extract(page, output_format="html", **options)
    if not out:
        raise ArticleSkipped("no article text found on the page")
    meta = trafilatura.extract_metadata(page, default_url=url)
    title = meta.title if meta else None
    root = lxml.html.fromstring(out)
    body = root.find("body") if root.tag == "html" else root
    body = body if body is not None else root
    words = len(body.text_content().split())
    if words < MIN_WORDS:
        raise ArticleSkipped(f"only {words} words found; a paywall or not an article")
    for h in list(body.iter("h1")):
        # A headline before the text repeats the title, which is shown above the article.
        if h.getprevious() is None or not any(e.tag == "p" for e in h.itersiblings(preceding=True)):
            title = title or " ".join(h.text_content().split())
            h.drop_tree()
        else:
            h.tag = "h2"
    images: list[str] = []
    seen: set[str] = set()
    for img in list(body.iter("img")):
        src = urljoin(url, (img.get("src") or "").strip())
        parsed = urlparse(src)
        key = f"{parsed.netloc}{parsed.path}"  # the same picture at another size
        if parsed.scheme not in ("http", "https") or key in seen:
            img.drop_tree()
            continue
        seen.add(key)
        img.set("src", src)
        images.append(src)
    for a in body.iter("a"):
        if a.get("href"):
            a.set("href", urljoin(url, a.get("href").strip()))
    inner = (body.text or "") + "".join(lxml.html.tostring(c, encoding="unicode") for c in body)
    lead = meta.image if meta and meta.image and not images else None
    lead = urljoin(url, lead) if lead else None
    return Extracted(title, meta.author if meta else None, meta.sitename if meta else None,
                     meta.date if meta else None, _clean(inner), images,
                     lead if lead and urlparse(lead).scheme in ("http", "https") else None, words)


# --- downloading ------------------------------------------------------------

class ArticleFetcher:
    def __init__(self, db: Database, user_agent: str, enabled: bool = True, timeout: float = 30.0,
                 client: httpx.Client | None = None, check_host: bool = True,
                 throttle: HostThrottle | None = None):
        self.db = db
        self.enabled = enabled
        self.check_host = check_host
        self.throttle = throttle or HostThrottle(1.0)
        self.client = client or httpx.Client(
            timeout=timeout, follow_redirects=False,
            headers={"User-Agent": user_agent, "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5"})

    def _download(self, url: str) -> tuple[bytes, str]:
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            if self.check_host:
                _assert_public_host(current)
            host = (urlparse(current).hostname or "").lower()
            self.throttle.wait(host)
            with self.client.stream("GET", current) as resp:
                self.throttle.note(host, resp.status_code, resp.headers)
                if resp.status_code in (301, 302, 303, 307, 308) and "location" in resp.headers:
                    current = urljoin(current, resp.headers["location"])
                    continue
                if resp.status_code == 429:
                    raise RemotePaused(f"{host}: HTTP 429 (too many requests)", self.throttle.paused_for(host))
                if resp.status_code in (401, 402, 403, 451):
                    raise ArticleRejected(f"HTTP {resp.status_code}: the site refused to send the page")
                if resp.status_code in (404, 410):
                    raise ArticleRejected(f"HTTP {resp.status_code}")
                if resp.status_code >= 400:
                    raise httpx.HTTPStatusError(f"HTTP {resp.status_code}", request=resp.request, response=resp)
                ctype = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
                if ctype and ctype not in ("text/html", "application/xhtml+xml"):
                    raise ArticleSkipped(f"not a web page ({ctype})")
                data = bytearray()
                for chunk in resp.iter_bytes(CHUNK):
                    data += chunk
                    if len(data) > MAX_PAGE_BYTES:
                        raise ArticleRejected(f"page too large (> {MAX_PAGE_BYTES // 1_000_000} MB)")
                return bytes(data), current
        raise ArticleRejected("too many redirects")

    def link_card(self, url: str) -> dict[str, Any]:
        """What a link card for `url` shows, as a Bluesky post's does: the
        page's title, description and picture (bytes, type), each None when it
        can't be read. Read once, when you post the link."""
        card: dict[str, Any] = {"title": None, "description": None, "image": None}
        try:
            page, final = self._download(url)
            meta = trafilatura.extract_metadata(page, default_url=final)
        except (MediaRejected, RemotePaused, httpx.HTTPError, OSError, ValueError):
            return card
        if meta is None:
            return card
        card["title"], card["description"] = meta.title, meta.description
        image = urljoin(final, meta.image) if meta.image else None
        if image and urlparse(image).scheme in ("http", "https"):
            try:
                card["image"] = self._download_image(image)
            except (MediaRejected, RemotePaused, httpx.HTTPError, OSError):
                pass
        return card

    def _download_image(self, url: str) -> tuple[bytes, str] | None:
        """A small picture (a link card's): its bytes and type, or None if it's too big or not a picture."""
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            if self.check_host:
                _assert_public_host(current)
            host = (urlparse(current).hostname or "").lower()
            self.throttle.wait(host)
            with self.client.stream("GET", current, headers={"Accept": "image/*"}) as resp:
                self.throttle.note(host, resp.status_code, resp.headers)
                if resp.status_code in (301, 302, 303, 307, 308) and "location" in resp.headers:
                    current = urljoin(current, resp.headers["location"])
                    continue
                ctype = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
                if resp.status_code != 200 or not ctype.startswith("image/"):
                    return None
                data = bytearray()
                for chunk in resp.iter_bytes(CHUNK):
                    data += chunk
                    if len(data) > CARD_IMAGE_BYTES:
                        return None
                return bytes(data), ctype
        return None

    def fetch_pending(self, limit: int = 20) -> int:
        if not self.enabled:
            return 0
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM articles WHERE status='pending' AND (next_attempt_at IS NULL OR next_attempt_at<=?) "
                "ORDER BY id DESC LIMIT ?", (utcnow(), limit)).fetchall()  # newest links first
        for row in rows:
            self.fetch_one(row)
        return len(rows)

    def fetch_one(self, row: Any) -> None:
        now = utcnow()
        page: bytes | None = None
        try:
            page, final_url = self._download(row["url"])
            art = extract(page, final_url)
        except ArticleRejected as exc:
            with self.db.transaction() as conn:
                conn.execute("UPDATE articles SET status=?, error=?, attempts=attempts+1, fetched_at=? WHERE id=?",
                             ("skipped" if isinstance(exc, ArticleSkipped) else "failed", str(exc), now, row["id"]))
                if page is not None:  # read, but not an article: where it lives still tells posts of it apart
                    self._note_page(conn, row["id"], page, final_url)
            return
        except RemotePaused as exc:  # the site asked us to wait: not the page's fault
            with self.db.transaction() as conn:
                conn.execute("UPDATE articles SET error=?, next_attempt_at=? WHERE id=?",
                             (str(exc), _after(now, exc.seconds), row["id"]))
            return
        except (httpx.HTTPError, OSError) as exc:
            attempts = row["attempts"] + 1
            error = f"{type(exc).__name__}: {exc}"
            with self.db.transaction() as conn:
                if attempts >= MAX_ATTEMPTS:
                    conn.execute("UPDATE articles SET status='failed', error=?, attempts=? WHERE id=?",
                                 (error, attempts, row["id"]))
                else:
                    retry = fmt_ts(parse_ts(now) + timedelta(minutes=5 * 3 ** attempts))  # type: ignore[operator]
                    conn.execute("UPDATE articles SET error=?, attempts=?, next_attempt_at=? WHERE id=?",
                                 (error, attempts, retry, row["id"]))
            return
        pics = art.images + ([art.lead_image] if art.lead_image else [])
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE articles SET status='ok', title=?, byline=?, site_name=?, published=?, lead_image_url=?, "
                "content_html=?, images_json=?, word_count=?, fetched_from=?, fetched_at=?, attempts=attempts+1, "
                "error=NULL WHERE id=?",
                (art.title, art.byline, art.site_name, art.published, art.lead_image, art.content_html,
                 json.dumps(pics), art.words, final_url, now, row["id"]))
            attach_pictures(conn, row["id"], pics, now)
            record_links(conn, row["id"], art.content_html)
            self._note_page(conn, row["id"], page, final_url)
            fingerprint(conn, row["id"], art.title, art.site_name, art.content_html)
            for r in conn.execute("SELECT object_id FROM article_refs WHERE article_id=?", (row["id"],)).fetchall():
                register_media(conn, r["object_id"], pics, now, from_article=True)

    @staticmethod
    def _note_page(conn: Conn, article_id: int, page: bytes, final_url: str) -> None:
        """Where the page was read from and where it says it lives, as keys;
        and where its replies and webmentions are, for discussions.py."""
        found = links_mod.page_links(page, final_url)
        conn.execute("UPDATE articles SET fetched_from=?, canonical_url=?, ap_url=?, webmention_url=? WHERE id=?",
                     (final_url, found["canonical"], found["activitypub"], found["webmention"], article_id))
        add_keys(conn, article_id, [final_url, found["canonical"]])



def retry(conn: Conn, article_id: int) -> bool:
    """Try a failed article again (the site may have been down)."""
    return conn.execute("UPDATE articles SET status='pending', attempts=0, next_attempt_at=?, error=NULL "
                        "WHERE id=? AND status='failed'", (utcnow(), article_id)).rowcount > 0
