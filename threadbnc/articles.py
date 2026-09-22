"""Linked articles: a readable copy of the web page a post links to.

A post's link is registered when its revision is recorded (like media) and the
bouncer later downloads the page and pulls the article out of it with
trafilatura: the text, headings, lists, quotes, links and pictures, without the
site's navigation, ads and scripts. The pictures are registered as the post's
media, so they follow its community's media settings and go when it's purged.

Not every link can be read. Links to pictures, videos, social media, home pages
and other threadiverse posts aren't tried; paywalls, sites that refuse the
bouncer and pages with too little text are given up on, and the post just links
to the original as before.
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

from .adapters.base import host_of, is_reddit_host, parse_thread_url
from .adapters.http import HostThrottle
from .db import Conn, Database, fmt_ts, parse_ts, utcnow
from .media import MAX_ATTEMPTS, MAX_REDIRECTS, MediaRejected, _assert_public_host
from .media import media_id
from .media import register as register_media
from .render import ALLOWED_ATTRS, ALLOWED_TAGS, MediaLookup, _media_html, looks_like_media

MAX_PAGE_BYTES = 5_000_000
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
    if not url or looks_like_media(url):
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
    conn.execute(f"DELETE FROM article_media WHERE article_id IN (SELECT id FROM articles WHERE {unwanted})",
                 (cutoff,))
    return conn.execute(f"DELETE FROM articles WHERE {unwanted}", (cutoff,)).rowcount


def ensure(conn: Conn, url: str, now: str) -> Any:
    """The article for a link (registered to be read if it's new), for reading
    one on demand: a link inside another article."""
    conn.execute("INSERT INTO articles(url, first_seen_at, next_attempt_at) VALUES (?,?,?) "
                 "ON CONFLICT(url) DO NOTHING", (url, now, now))
    conn.execute("UPDATE articles SET opened_at=? WHERE url=?", (now, url))
    return conn.execute("SELECT * FROM articles WHERE url=?", (url,)).fetchone()


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


def render(content_html: str | None, lookup: MediaLookup, page_url: str | None = None,
           read_link: Callable[[str], str] | None = None) -> Markup:
    """Stored article HTML with its pictures swapped for the archived copies.
    With `read_link`, links to other articles go to reading them here (marked
    with the article-link class); other links open the site in a new tab."""
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
        if read_link and urldefrag(href)[0] != here and looks_like_article(href) and not a.get("class"):
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
            self.throttle.wait((urlparse(current).hostname or "").lower())
            with self.client.stream("GET", current) as resp:
                if resp.status_code in (301, 302, 303, 307, 308) and "location" in resp.headers:
                    current = urljoin(current, resp.headers["location"])
                    continue
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
        try:
            page, final_url = self._download(row["url"])
            art = extract(page, final_url)
        except ArticleRejected as exc:
            with self.db.transaction() as conn:
                conn.execute("UPDATE articles SET status=?, error=?, attempts=attempts+1, fetched_at=? WHERE id=?",
                             ("skipped" if isinstance(exc, ArticleSkipped) else "failed", str(exc), now, row["id"]))
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
            for r in conn.execute("SELECT object_id FROM article_refs WHERE article_id=?", (row["id"],)).fetchall():
                register_media(conn, r["object_id"], pics, now, from_article=True)



def retry(conn: Conn, article_id: int) -> bool:
    """Try a failed article again (the site may have been down)."""
    return conn.execute("UPDATE articles SET status='pending', attempts=0, next_attempt_at=?, error=NULL "
                        "WHERE id=? AND status='failed'", (utcnow(), article_id)).rowcount > 0
