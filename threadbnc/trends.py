"""Trending: what's being posted and talked about on Bluesky and Mastodon,
and linked in the archive.

Two streams bring posts as they're made: Bluesky's Jetstream (jetstream.py),
and your Mastodon server's public timeline once you subscribe to it
(mastodon_stream.py). Apart from posts with a followed hashtag, nothing they
bring is saved as a post. They're counted:

- Links. Each post's links (its link card, and the links in its text) are
  counted by the hour under their key (links.py), so a page's different
  addresses count as one, and each account counts once an hour for a page (so
  a bot posting one link over and over doesn't make it trend). The count is
  all that's kept: nothing is read from the pages. Links posted only once in
  their first day, or fewer than FEW_POSTS times in their first week, are
  forgotten; counts go after a month.
- Replies and quotes. A reply counts for the post that started its thread, a
  quote for the post it quotes. On Bluesky, likes can be counted from the
  stream too (roughly five times the traffic: see jetstream.py); otherwise, and
  on Mastodon, which has no stream of likes, the totals for the posts most
  replied to are read now and then (Bluesky's AppView here; your Mastodon
  server in mastodon_stream.py), which is also where what a post says and who
  posted it come from. Posts go after a week, and quiet ones sooner. A post's
  pictures (PICTURES of them, or its link card's or video's cover) are
  downloaded once it's shown on the Trending page, and go with it.
- Hashtags, counted like links: by the hour, each account once an hour for
  a hashtag, and forgotten the same way.

The Trending page ranks them. Articles are ranked by how many posts linked
them in the past day, week or month: on Bluesky, on Mastodon and in the
archive (the passively captured posts, as the Articles page did before), with
links to the same page counted together (their keys, and once an article is
read, where it redirected to and where it says it lives). The TOP_CACHED most
posted of each are read here (articles.py) so they can be read on the page,
and kept while they stay among them; TRENDING_GRACE after they drop out, they
go like any other article nothing links to. A page that turns out not to be an
article when it's read (a feed, a chat invite, too little text) drops out of
the ranking, and the next one is read instead."""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable
from urllib.parse import urlparse

from . import articles, links as links_mod
from . import media as media_mod
from . import thumbs
from .adapters import BSKY_DOMAIN, TAG_DOMAIN, TAG_PREFIX, RemoteError
from .adapters.activitypub import status_id
from .adapters.bluesky import GET_POSTS, POST
from .db import Conn, Database, fmt_ts, parse_ts, utcnow
from .traffic import tagged

log = logging.getLogger(__name__)

SOURCES = {"bluesky": "Bluesky", "mastodon": "Mastodon"}
WINDOWS = {"day": timedelta(days=1), "week": timedelta(days=7), "month": timedelta(days=30)}
POST_WINDOWS = {"day": timedelta(days=1), "week": timedelta(days=7)}
POST_SORTS = ("likes", "replies")
# App setting (JSON): {"bluesky": count what's posted on Bluesky (Jetstream stays connected),
# "bluesky_likes": "appview" (read the totals of the posts most replied to) | "stream" (count every like)}
SETTINGS = "trends"
LIKES_FROM = ("appview", "stream")
HOUR = "%Y-%m-%dT%H"

TOP_CACHED = 12  # the most posted articles of each window read and kept for reading
PICTURES = 4  # a trending post's pictures downloaded, at most
CACHE_EVERY = 900.0  # seconds between choosing them
RANK_KEYS = 3000  # the most posted links in a window looked at when ranking
TIDY_EVERY = 3600.0
DAILY_AFTER = timedelta(days=2)  # hourly link counts older than this are added up by the day
KEEP_COUNTS = timedelta(days=31)
ONCE_AFTER = timedelta(days=1)  # a link posted once in its first day is forgotten after it
FEW_AFTER, FEW_POSTS = timedelta(days=7), 5  # and one posted fewer than 5 times in its first week
KEEP_POSTS = timedelta(days=8)
QUIET_AFTER, QUIET_SEEN = timedelta(hours=6), 3  # a post replied to, quoted or liked fewer than 3 times goes
MAX_POST_AGE = timedelta(days=7)  # replies to and likes of older posts aren't counted

# Reading the totals of the posts most replied to (Bluesky: through the AppView).
CHECK_EVERY = 300.0  # seconds between rounds
CHECK_POSTS = 100  # at most this many posts a round (GET_POSTS to a request)
CHECK_LOOKED_AT = 400  # of the posts most talked about lately
# A post's totals are read again after this long, by its age.
RECHECK = ((timedelta(hours=6), timedelta(minutes=10)), (timedelta(days=1), timedelta(minutes=30)),
           (timedelta(days=3), timedelta(hours=2)), (MAX_POST_AGE, timedelta(hours=12)))

_TID = "234567abcdefghijklmnopqrstuvwxyz"
_EARLIEST = datetime(2022, 11, 1, tzinfo=timezone.utc)


def settings(db: Database) -> dict[str, Any]:
    raw = db.get_setting(SETTINGS)
    try:
        got = json.loads(raw) if raw else {}
    except ValueError:
        got = {}
    got = got if isinstance(got, dict) else {}
    if got.get("bluesky_likes") not in LIKES_FROM:
        got["bluesky_likes"] = "appview"
    got["bluesky"] = got.get("bluesky") is not False
    return got


def save_settings(db: Database, **changes: Any) -> dict[str, Any]:
    value = settings(db) | changes
    with db.transaction() as conn:
        conn.execute("INSERT INTO app_settings(key, value) VALUES (?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (SETTINGS, json.dumps(value)))
    return value


# --- reading posts ------------------------------------------------------------

def tid_time(rkey: str) -> str | None:
    """When a Bluesky record was made, from its key (a TID: microseconds since
    1970, then a clock id, in base 32). None for a key that isn't one."""
    if len(rkey) != 13 or rkey[0] not in _TID[:16]:
        return None
    n = 0
    for ch in rkey:
        i = _TID.find(ch)
        if i < 0:
            return None
        n = n * 32 + i
    try:
        at = datetime.fromtimestamp((n >> 10) / 1_000_000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    if at < _EARLIEST or at > datetime.now(timezone.utc) + timedelta(days=1):
        return None
    return fmt_ts(at)


def post_time(uri: str) -> str | None:
    """When a Bluesky post was made, from its at:// address."""
    parts = uri.split("/")
    return tid_time(parts[4]) if len(parts) == 5 and parts[3] == POST else None


def countable(url: Any) -> str | None:
    """A link's key, when it's to a page that might be an article: not a
    picture, a video, social media or another post (articles.candidate), nor a
    fediverse post or account."""
    if not isinstance(url, str) or not articles.candidate(url):
        return None
    path = urlparse(url).path or ""
    if status_id(url) or (path.startswith("/@") and path.count("/") <= 2):
        return None
    return links_mod.key(url)


def bluesky_links(record: dict[str, Any]) -> list[tuple[str, str | None, str | None]]:
    """(link, title, description) for each page a post record links to: its
    link card first, then the links in its text."""
    out = []
    embed = record.get("embed") if isinstance(record.get("embed"), dict) else {}
    media = embed.get("media") if isinstance(embed.get("media"), dict) else embed
    card = media.get("external") if isinstance(media.get("external"), dict) else None
    if card and isinstance(card.get("uri"), str):
        out.append((card["uri"], card.get("title") or None, card.get("description") or None))
    for f in record.get("facets") if isinstance(record.get("facets"), list) else []:
        for feature in (f.get("features") or []) if isinstance(f, dict) else []:
            if isinstance(feature, dict) and str(feature.get("$type") or "").endswith("#link"):
                out.append((str(feature.get("uri") or ""), None, None))
    return out


def bluesky_quoted(record: dict[str, Any]) -> str | None:
    """The at:// address of the post a post record quotes, if it does."""
    embed = record.get("embed") if isinstance(record.get("embed"), dict) else {}
    inner = embed.get("record") if isinstance(embed.get("record"), dict) else {}
    if isinstance(inner.get("record"), dict):  # recordWithMedia
        inner = inner["record"]
    uri = inner.get("uri")
    return uri if isinstance(uri, str) and f"/{POST}/" in uri else None


# --- counting -------------------------------------------------------------------

class Tally:
    """What one stream has counted since it last wrote to the database. The
    stream's thread adds to it, and flushes it now and then."""

    def __init__(self, source: str):
        self.source = source
        self._lock = threading.Lock()
        self._hour, self._posted = "", set()  # (link key, author) counted this hour
        self._clear()

    def _clear(self) -> None:
        self.links: dict[tuple[str, str], list[Any]] = {}  # (key, hour) -> [posts, link, title, description]
        self.tags: dict[tuple[str, str], int] = {}  # (hashtag, hour) -> posts
        self.posts: dict[str, list[Any]] = {}  # ref -> [replies, quotes, likes, created_at]

    def post_tags(self, tags: Iterable[str], author: str | None = None) -> int:
        """Count one post's hashtags (named as followed ones are), each once,
        and once an hour for each `author`. Returns how many were counted."""
        hour = time.strftime(HOUR, time.gmtime())
        counted = 0
        with self._lock:
            if self._hour != hour:
                self._hour, self._posted = hour, set()
            for tag in dict.fromkeys(tags):
                mark = ("#" + tag, author)
                if not tag or (author and mark in self._posted):
                    continue
                if author:
                    self._posted.add(mark)
                self.tags[(tag, hour)] = self.tags.get((tag, hour), 0) + 1
                counted += 1
        return counted

    def post_links(self, found: Iterable[tuple[str, str | None, str | None]], author: str | None = None) -> int:
        """Count one post's links, each page once, and once an hour for each
        `author`. Returns how many were counted."""
        hour = time.strftime(HOUR, time.gmtime())
        seen: set[str] = set()
        with self._lock:
            if self._hour != hour:
                self._hour, self._posted = hour, set()
            for url, title, description in found:
                key = countable(url)
                if key is None or key in seen or (author and (key, author) in self._posted):
                    continue
                seen.add(key)
                if author:
                    self._posted.add((key, author))
                entry = self.links.get((key, hour))
                if entry is None:
                    self.links[(key, hour)] = [1, url, title, description]
                else:
                    entry[0] += 1
                    entry[2], entry[3] = entry[2] or title, entry[3] or description
        return len(seen)

    def _post(self, ref: str, created_at: str | None) -> list[Any]:
        entry = self.posts.get(ref)
        if entry is None:
            entry = self.posts[ref] = [0, 0, 0, created_at]
        elif created_at and not entry[3]:
            entry[3] = created_at
        return entry

    def reply(self, ref: str, created_at: str | None = None) -> None:
        with self._lock:
            self._post(ref, created_at)[0] += 1

    def quote(self, ref: str, created_at: str | None = None) -> None:
        with self._lock:
            self._post(ref, created_at)[1] += 1

    def like(self, ref: str, created_at: str | None = None) -> None:
        with self._lock:
            self._post(ref, created_at)[2] += 1

    def flush(self, db: Database) -> None:
        with self._lock:
            found, tags, posts = self.links, self.tags, self.posts
            self._clear()
        if not found and not tags and not posts:
            return
        now = utcnow()
        with db.transaction() as conn:
            conn.executemany(
                "INSERT INTO tag_counts(tag, hour, source, posts) VALUES (?,?,?,?) ON CONFLICT(tag, hour, source) "
                "DO UPDATE SET posts=tag_counts.posts+excluded.posts",
                [(tag, hour, self.source, n) for (tag, hour), n in tags.items()])
            totals: dict[str, int] = {}
            for (tag, _hour), n in tags.items():
                totals[tag] = totals.get(tag, 0) + n
            conn.executemany(
                "INSERT INTO tags_seen(tag, first_seen_at, last_seen_at, posts) VALUES (?,?,?,?) ON CONFLICT(tag) "
                "DO UPDATE SET last_seen_at=excluded.last_seen_at, posts=tags_seen.posts+excluded.posts",
                [(tag, now, now, n) for tag, n in totals.items()])
            conn.executemany(
                "INSERT INTO link_counts(key, hour, source, posts) VALUES (?,?,?,?) ON CONFLICT(key, hour, source) "
                "DO UPDATE SET posts=link_counts.posts+excluded.posts",
                [(key, hour, self.source, e[0]) for (key, hour), e in found.items()])
            conn.executemany(
                "INSERT INTO links_seen(key, url, title, description, first_seen_at, last_seen_at, posts) "
                "VALUES (?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET last_seen_at=excluded.last_seen_at, "
                "posts=links_seen.posts+excluded.posts, title=COALESCE(links_seen.title, excluded.title), "
                "description=COALESCE(links_seen.description, excluded.description)",
                [(key, e[1][:2000], (e[2] or "")[:500] or None, (e[3] or "")[:1000] or None, now, now, e[0])
                 for (key, _hour), e in found.items()])
            conn.executemany(
                "INSERT INTO stream_posts(source, ref, created_at, first_seen_at, replies_seen, quotes_seen, "
                "likes_seen) VALUES (?,?,?,?,?,?,?) ON CONFLICT(source, ref) DO UPDATE SET "
                "replies_seen=stream_posts.replies_seen+excluded.replies_seen, "
                "quotes_seen=stream_posts.quotes_seen+excluded.quotes_seen, "
                "likes_seen=stream_posts.likes_seen+excluded.likes_seen, "
                "created_at=COALESCE(stream_posts.created_at, excluded.created_at)",
                [(self.source, ref, e[3], now, e[0], e[1], e[2]) for ref, e in posts.items()])


def tidy(conn: Conn, now: str | None = None) -> None:
    """Add up old hourly counts by the day, forget links and hashtags posted
    too rarely to trend, and posts too old or too quiet."""
    moment = parse_ts(now or utcnow()) or datetime.now(timezone.utc)
    for counts, seen, key in (("link_counts", "links_seen", "key"), ("tag_counts", "tags_seen", "tag")):
        _tidy_counts(conn, moment, counts, seen, key)
    conn.execute("DELETE FROM stream_posts WHERE COALESCE(created_at, first_seen_at) < ? OR "
                 "(replies_seen + quotes_seen + likes_seen < ? AND first_seen_at < ? AND checked_at IS NULL)",
                 (fmt_ts(moment - KEEP_POSTS), QUIET_SEEN, fmt_ts(moment - QUIET_AFTER)))
    conn.execute("DELETE FROM stream_post_media WHERE NOT EXISTS (SELECT 1 FROM stream_posts p "
                 "WHERE p.source=stream_post_media.source AND p.ref=stream_post_media.ref)")


def _tidy_counts(conn: Conn, moment: datetime, counts: str, seen: str, key: str) -> None:
    """tidy() for one kind of count: `counts` by the hour, `seen` their totals, both by `key`."""
    daily = (moment - DAILY_AFTER).strftime(HOUR)
    rows = conn.execute(
        f"SELECT {key} AS k, substr(hour, 1, 10) AS day, source, SUM(posts) AS n FROM {counts} "
        f"WHERE hour < ? AND hour NOT LIKE '%T00' GROUP BY {key}, substr(hour, 1, 10), source", (daily,)).fetchall()
    if rows:
        conn.execute(f"DELETE FROM {counts} WHERE hour < ? AND hour NOT LIKE '%T00'", (daily,))
        conn.executemany(
            f"INSERT INTO {counts}({key}, hour, source, posts) VALUES (?,?,?,?) ON CONFLICT({key}, hour, source) "
            f"DO UPDATE SET posts={counts}.posts+excluded.posts",
            [(r["k"], r["day"] + "T00", r["source"], r["n"]) for r in rows])
    once, few = fmt_ts(moment - ONCE_AFTER), fmt_ts(moment - FEW_AFTER)
    forgotten = "(posts < 2 AND first_seen_at < ?) OR (posts < ? AND first_seen_at < ?) OR last_seen_at < ?"
    params = (once, FEW_POSTS, few, fmt_ts(moment - KEEP_COUNTS))
    conn.execute(f"DELETE FROM {counts} WHERE {key} IN (SELECT {key} FROM {seen} WHERE {forgotten})", params)
    conn.execute(f"DELETE FROM {seen} WHERE {forgotten}", params)
    conn.execute(f"DELETE FROM {counts} WHERE hour < ?", ((moment - KEEP_COUNTS).strftime(HOUR),))


# --- ranking articles ---------------------------------------------------------

def _since(window: str, now: str | None) -> str:
    moment = parse_ts(now or utcnow()) or datetime.now(timezone.utc)
    return fmt_ts(moment - WINDOWS[window])


def ranked_articles(conn: Conn, window: str, now: str | None = None, *,
                    archive_skips: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    """Every article (or page not read yet) posted in the window, the most
    posted first. See the module's notes. `archive_skips`: source domains
    whose captured posts aren't counted from the archive, because a stream
    counts them already."""
    return _rank(conn, _since(window, now), archive_skips)


def _chunks(values: list[Any], size: int = 500) -> Iterable[list[Any]]:
    for i in range(0, len(values), size):
        yield values[i:i + size]


def _rank(conn: Conn, since: str, archive_skips: tuple[str, ...]) -> list[dict[str, Any]]:
    # The archive's passively captured posts, each counted for its current link.
    skip = "".join(" AND t.source_domain != ?" for _ in archive_skips)
    posts = [dict(r) for r in conn.execute(
        f"""SELECT t.id AS tid, t.community_id, o.id AS object_id,
                   COALESCE(o.created_at, o.first_seen_at) AS posted_at,
                   r.title AS post_title, a.id AS article_id,
                   c.name AS cname, c.canonical_ap_id AS c_ap
            FROM archived_threads t
            JOIN objects o ON o.id=t.root_object_id
            JOIN revisions r ON r.object_id=o.id AND r.seq=o.revision_count
            JOIN articles a ON a.url=r.url
            JOIN article_refs ar ON ar.object_id=o.id AND ar.article_id=a.id
            JOIN communities c ON c.id=t.community_id
            WHERE t.trashed_at IS NULL
              AND (t.retention='auto' OR t.promoted_at IS NOT NULL)
              AND COALESCE(o.created_at, o.first_seen_at) >= ? {skip}""",
        (since, *archive_skips)).fetchall()]
    # The streams' counts, of the links posted most.
    streamed = {r["key"]: {"bluesky": int(r["bluesky"] or 0), "mastodon": int(r["mastodon"] or 0)} for r in conn.execute(
        "SELECT key, SUM(CASE WHEN source='bluesky' THEN posts ELSE 0 END) AS bluesky, "
        "SUM(CASE WHEN source='mastodon' THEN posts ELSE 0 END) AS mastodon "
        "FROM link_counts WHERE hour >= ? GROUP BY key ORDER BY SUM(posts) DESC LIMIT ?",
        (since[:13], RANK_KEYS)).fetchall()}
    if not posts and not streamed:
        return []

    # Group the article rows and links known by a common address.
    parent: dict[tuple[str, Any], tuple[str, Any]] = {}

    def root(node: tuple[str, Any]) -> tuple[str, Any]:
        parent.setdefault(node, node)
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(a: tuple[str, Any], b: tuple[str, Any]) -> None:
        left, right = root(a), root(b)
        if left != right:
            parent[right] = left

    archive_ids = sorted({p["article_id"] for p in posts})
    for ids in _chunks(archive_ids):
        for k in conn.execute(f"SELECT article_id, key FROM article_keys WHERE article_id IN ({_marks(ids)})", ids):
            union(("a", k["article_id"]), ("k", k["key"]))
    for aid in archive_ids:
        root(("a", aid))
    keys = list(streamed)
    for chunk in _chunks(keys):
        for k in conn.execute(f"SELECT article_id, key FROM article_keys WHERE key IN ({_marks(chunk)})", chunk):
            union(("k", k["key"]), ("a", k["article_id"]))
    for key in keys:
        root(("k", key))

    groups: dict[tuple[str, Any], dict[str, Any]] = {}
    for node in list(parent):
        g = groups.setdefault(root(node), {"article_ids": set(), "keys": set(), "posts": []})
        g["article_ids" if node[0] == "a" else "keys"].add(node[1])
    for p in posts:
        groups[root(("a", p["article_id"]))]["posts"].append(p)

    all_ids = sorted({aid for g in groups.values() for aid in g["article_ids"]})
    rows: dict[int, dict[str, Any]] = {}
    for ids in _chunks(all_ids):
        rows.update((r["id"], dict(r)) for r in conn.execute(
            f"""SELECT id, url, status, title, byline, site_name, published, word_count, fetched_from,
                       canonical_url, first_seen_at, fetched_at, trending_at
                FROM articles WHERE id IN ({_marks(ids)})""", ids).fetchall())

    items: list[dict[str, Any]] = []
    for g in groups.values():
        counts = {"bluesky": 0, "mastodon": 0}
        for key in g["keys"]:
            for source, n in streamed.get(key, {}).items():
                counts[source] += n
        linked = g["posts"]
        archive = len({p["tid"] for p in linked})
        total = counts["bluesky"] + counts["mastodon"] + archive
        if not total:
            continue
        direct: dict[int, int] = {}
        for p in linked:
            direct[p["article_id"]] = direct.get(p["article_id"], 0) + 1
        choices = [rows[aid] for aid in g["article_ids"] if aid in rows]
        if choices and all(a["status"] == "skipped" for a in choices):
            continue  # read, and not an article: a feed, a chat invite, too little text
        top_key = max(g["keys"], key=lambda k: (sum(streamed.get(k, {}).values()), k)) if g["keys"] else None
        item: dict[str, Any]
        if choices:
            item = dict(max(choices, key=lambda a: (
                a["status"] == "ok", bool(a["title"]), direct.get(a["id"], 0),
                a["fetched_at"] or a["first_seen_at"], -a["id"])))
        else:
            item = {"id": None, "url": None, "status": "pending", "title": None, "byline": None,
                    "site_name": None, "published": None, "word_count": None, "fetched_from": None,
                    "canonical_url": None, "first_seen_at": None, "fetched_at": None, "trending_at": None}
        latest = max(linked, key=lambda p: (p["posted_at"] or "", p["tid"])) if linked else None
        item.update(article_ids=sorted(g["article_ids"]), keys=sorted(g["keys"]), top_key=top_key,
                    bluesky=counts["bluesky"], mastodon=counts["mastodon"], archive=archive, total=total,
                    post_count=archive, community_count=len({p["community_id"] for p in linked}),
                    latest_post=latest, latest_posted_at=latest["posted_at"] if latest else None)
        if not item["title"] and latest:
            item["title"] = latest["post_title"]
        items.append(item)

    # The page for links no article row has yet: as first posted, with its card's title.
    bare = [i["top_key"] for i in items if i["top_key"] and (i["id"] is None or not i["title"])]
    seen: dict[str, Any] = {}
    for chunk in _chunks(bare):
        seen.update((r["key"], r) for r in conn.execute(
            f"SELECT key, url, title, description FROM links_seen WHERE key IN ({_marks(chunk)})", chunk))
    for item in items:
        s = seen.get(item["top_key"])
        if s is not None:
            item["url"] = item["url"] or s["url"]
            item["title"] = item["title"] or s["title"]
            item["description"] = s["description"]
    items = [i for i in items if i["url"]]
    # Stable sorts: newer first within equal counts.
    items.sort(key=lambda a: (a["latest_posted_at"] or a["first_seen_at"] or "", a["id"] or 0), reverse=True)
    items.sort(key=lambda a: (a["total"], a["archive"]), reverse=True)
    return items


def _marks(values: list[Any]) -> str:
    return ",".join("?" * len(values))


def trending_articles(conn: Conn, items: list[dict[str, Any]], page: int = 1,
                      per_page: int = 25) -> tuple[list[dict[str, Any]], bool]:
    """A page of `items` (ranked_articles), with what the page shows of each:
    the start of its text and its first picture, when it's been read."""
    page = max(1, page)
    start = (page - 1) * per_page
    shown = [dict(i) for i in items[start:start + per_page]]
    ids = sorted({aid for item in shown for aid in item["article_ids"]})
    if ids:
        content = {r["id"]: r["content_html"] for r in conn.execute(
            f"SELECT id, content_html FROM articles WHERE id IN ({_marks(ids)})", ids)}
        pictures = {r["article_id"]: r["mid"] for r in conn.execute(
            f"""SELECT am.article_id, MIN(m.id) AS mid
                FROM article_media am JOIN media m ON m.id=am.media_id
                WHERE am.article_id IN ({_marks(ids)}) AND m.status='ok'
                  AND m.content_type LIKE 'image/%' AND m.content_type != 'image/svg+xml'
                GROUP BY am.article_id""", ids)}
        for item in shown:
            item["content_html"] = content.get(item["id"]) if item["id"] else None
            item["picture"] = next((pictures[aid] for aid in [item["id"], *item["article_ids"]]
                                    if aid in pictures), None)
    return shown, len(items) > start + per_page


# --- ranking hashtags ------------------------------------------------------------

def trending_tags(conn: Conn, window: str = "day", page: int = 1, per_page: int = 50,
                  now: str | None = None) -> tuple[list[dict[str, Any]], bool]:
    """The hashtags used in the most posts in the window, on Bluesky and
    Mastodon, with the hashtag's community here when it's followed."""
    since = _since(window, now)
    page = max(1, page)
    rows = conn.execute(
        "SELECT tag, SUM(CASE WHEN source='bluesky' THEN posts ELSE 0 END) AS bluesky, "
        "SUM(CASE WHEN source='mastodon' THEN posts ELSE 0 END) AS mastodon, SUM(posts) AS total "
        "FROM tag_counts WHERE hour >= ? GROUP BY tag ORDER BY SUM(posts) DESC, tag LIMIT ? OFFSET ?",
        (since[:13], per_page + 1, (page - 1) * per_page)).fetchall()
    items = [{"tag": r["tag"], "bluesky": int(r["bluesky"] or 0), "mastodon": int(r["mastodon"] or 0),
              "total": int(r["total"] or 0), "community_id": None, "following": False} for r in rows[:per_page]]
    if items:
        names = [TAG_PREFIX + i["tag"] for i in items]
        here = {r["canonical_ap_id"][len(TAG_PREFIX):]: r for r in conn.execute(
            f"SELECT c.id, c.canonical_ap_id, f.active FROM communities c "
            f"LEFT JOIN community_follows f ON f.community_id=c.id WHERE c.canonical_ap_id IN ({_marks(names)})",
            names)}
        for item in items:
            r = here.get(item["tag"])
            if r is not None:
                item["community_id"], item["following"] = r["id"], bool(r["active"])
    return items, len(rows) > per_page


# --- ranking posts ------------------------------------------------------------

def trending_posts(conn: Conn, window: str = "day", sort: str = "likes", source: str | None = None,
                   page: int = 1, per_page: int = 25, now: str | None = None) -> tuple[list[dict[str, Any]], bool]:
    """The posts made in the window most liked (or most replied to) on Bluesky
    and Mastodon, of those whose totals have been read. A total not read yet
    is what the stream counted."""
    moment = parse_ts(now or utcnow()) or datetime.now(timezone.utc)
    since = fmt_ts(moment - POST_WINDOWS.get(window, POST_WINDOWS["day"]))
    score = ("COALESCE(likes, likes_seen)" if sort == "likes"
             else "COALESCE(replies, replies_seen)")
    where, params = "", [since]
    if source in SOURCES:
        where, params = " AND source=?", [since, source]
    page = max(1, page)
    rows = conn.execute(
        f"SELECT *, {score} AS score FROM stream_posts WHERE gone=0 AND view_json IS NOT NULL "
        f"AND COALESCE(created_at, first_seen_at) >= ?{where} ORDER BY score DESC, created_at DESC LIMIT ? OFFSET ?",
        (*params, per_page + 1, (page - 1) * per_page)).fetchall()
    out = []
    for r in rows[:per_page]:
        item = dict(r)
        try:
            item["view"] = json.loads(r["view_json"])
        except ValueError:
            continue
        item["likes"] = r["likes"] if r["likes"] is not None else r["likes_seen"]
        item["replies"] = r["replies"] if r["replies"] is not None else r["replies_seen"]
        item["key"] = post_key(r["source"], r["ref"])
        out.append(item)
    shown = pictures_of(conn, [(i["source"], i["ref"]) for i in out])
    for item in out:
        item["pictures"], item["pictures_waiting"] = shown.get((item["source"], item["ref"]), ([], False))
        item["pictures_waiting"] = item["pictures_waiting"] or (bool(item["view"].get("images"))
                                                                and not item["pictures"])
    return out, len(rows) > per_page


def post_key(source: str, ref: str) -> str:
    """A short name for a trending post, for the page to ask about it by."""
    return hashlib.sha1(f"{source} {ref}".encode()).hexdigest()[:12]


def want_pictures(conn: Conn, source: str, ref: str, now: str) -> bool:
    """Register a trending post's pictures to be downloaded (it's being shown).
    Returns whether it has any."""
    row = conn.execute("SELECT view_json FROM stream_posts WHERE source=? AND ref=?", (source, ref)).fetchone()
    try:
        urls = json.loads(row["view_json"]).get("images") or [] if row and row["view_json"] else []
    except ValueError:
        urls = []
    for position, url in enumerate(u for u in urls[:PICTURES] if isinstance(u, str) and u.startswith("https://")):
        conn.execute("INSERT INTO stream_post_media(source, ref, media_id, position) VALUES (?,?,?,?) "
                     "ON CONFLICT(source, ref, media_id) DO NOTHING",
                     (source, ref, media_mod.media_id(conn, url, now), position))
    return bool(urls)


def pictures_of(conn: Conn, posts: list[tuple[str, str]]) -> dict[tuple[str, str], tuple[list[int], bool]]:
    """Each trending post's downloaded pictures (media ids, in order), and
    whether any are still to download."""
    out: dict[tuple[str, str], tuple[list[int], bool]] = {}
    for chunk in _chunks(posts, 200):
        where = " OR ".join("(s.source=? AND s.ref=?)" for _ in chunk)
        for r in conn.execute(
                f"SELECT s.source, s.ref, m.id, m.status, m.content_type FROM stream_post_media s "
                f"JOIN media m ON m.id=s.media_id WHERE {where} ORDER BY s.position",
                [v for pair in chunk for v in pair]).fetchall():
            ready, waiting = out.get((r["source"], r["ref"]), ([], False))
            if r["status"] == "ok" and (r["content_type"] or "").startswith("image/"):
                ready = [*ready, r["id"]]
            waiting = waiting or r["status"] == "pending"
            out[(r["source"], r["ref"])] = (ready, waiting)
    return out


def due_checks(conn: Conn, source: str, limit: int, now: str | None = None) -> list[Any]:
    """The posts, of those talked about most lately, whose totals are due to
    be read (again): see RECHECK."""
    moment = parse_ts(now or utcnow()) or datetime.now(timezone.utc)
    rows = conn.execute(
        "SELECT * FROM stream_posts WHERE source=? AND gone=0 AND COALESCE(created_at, first_seen_at) >= ? "
        "ORDER BY replies_seen + quotes_seen + likes_seen + COALESCE(likes, 0) DESC LIMIT ?",
        (source, fmt_ts(moment - MAX_POST_AGE), CHECK_LOOKED_AT)).fetchall()
    due = []
    for r in rows:
        age = moment - (parse_ts(r["created_at"] or r["first_seen_at"]) or moment)
        every = next((e for limit_age, e in RECHECK if age < limit_age), RECHECK[-1][1])
        checked = parse_ts(r["checked_at"])
        if checked is None or moment - checked >= every:
            due.append(r)
        if len(due) >= limit:
            break
    return due


def record_totals(conn: Conn, source: str, found: dict[str, dict[str, Any]], asked: list[str], now: str) -> None:
    """Totals read for posts: {ref: {likes, replies, reposts, created_at, view}}. Those asked for and
    not found are gone (deleted, or hidden)."""
    for ref, t in found.items():
        conn.execute(
            "INSERT INTO stream_posts(source, ref, created_at, first_seen_at, likes, replies, reposts, checked_at, "
            "view_json) VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(source, ref) DO UPDATE SET likes=excluded.likes, "
            "replies=excluded.replies, reposts=excluded.reposts, checked_at=excluded.checked_at, "
            "view_json=excluded.view_json, gone=0, "
            "created_at=COALESCE(stream_posts.created_at, excluded.created_at)",
            (source, ref, t.get("created_at"), now, t.get("likes"), t.get("replies"), t.get("reposts"), now,
             json.dumps(t["view"])))
    missing = [ref for ref in asked if ref not in found]
    for chunk in _chunks(missing):
        conn.execute(f"UPDATE stream_posts SET gone=1, checked_at=? WHERE source=? AND ref IN ({_marks(chunk)})",
                     (now, source, *chunk))


def bluesky_view(view: dict[str, Any]) -> dict[str, Any]:
    """What the Trending page shows of a Bluesky post, from its AppView view."""
    from .adapters.bluesky import _Content, web_url

    record = view.get("record") if isinstance(view.get("record"), dict) else {}
    content = _Content(record, view.get("embed"))
    author = view.get("author") or {}
    return {"url": web_url(view["uri"]), "text": content.text[:3000],
            "handle": author.get("handle") or author.get("did"), "name": author.get("displayName") or None,
            "author_url": f"https://{BSKY_DOMAIN}/profile/{author.get('did') or author.get('handle')}",
            "link": content.link, "link_title": content.link_title, "pictures": len(content.pictures),
            "video": content.video, "quote": content.quote is not None,
            "images": (content.pictures or ([content.cover] if content.cover else []))[:PICTURES]}


# --- upkeep -----------------------------------------------------------------------

class Trends:
    """The bouncer's side of trending: tidying the counts, reading the totals
    of the Bluesky posts most talked about, and reading (and keeping, while
    they stay there) the most posted articles. A bouncer hook."""

    def __init__(self, bouncer: Any, archive_skips: Any = None):
        self.bouncer, self.db = bouncer, bouncer.db
        # () -> the source domains a stream counts already (see ranked_articles).
        self.archive_skips = archive_skips or (lambda: ())
        self._tidied = self._checked = self._cached = 0.0
        bouncer.hooks.append(self.upkeep)

    def upkeep(self) -> None:
        now = time.monotonic()
        if now - self._tidied >= TIDY_EVERY:
            self._tidied = now
            with self.db.transaction() as conn:
                tidy(conn)
                files = media_mod.collect_orphans(conn, self.bouncer.media_dir)  # the pictures of posts gone
            for f in files:  # only after that committed
                f.unlink(missing_ok=True)
                thumbs.remove_for(self.bouncer.media_dir, f.stem)
        if now - self._checked >= CHECK_EVERY:
            self._checked = now
            with tagged("trends"):
                try:
                    self.check_bluesky()
                except RemoteError as exc:
                    log.warning("reading Bluesky posts' totals failed: %s", exc)
        if now - self._cached >= CACHE_EVERY:
            self._cached = now
            with tagged("trends"):
                self.cache_articles()

    def check_bluesky(self, now: str | None = None) -> int:
        """Read the totals of the Bluesky posts most talked about lately, and
        who posted them and what they say, from the AppView."""
        with self.db.connect() as conn:
            due = [r["ref"] for r in due_checks(conn, "bluesky", CHECK_POSTS, now)]
        adapter = self.bouncer.bluesky_adapter
        for chunk in _chunks(due, GET_POSTS):
            views = adapter._get("app.bsky.feed.getPosts", uris=chunk).get("posts") or []
            found = {}
            for v in views:
                if not isinstance(v, dict) or v.get("uri") not in chunk:
                    continue
                record = v.get("record") if isinstance(v.get("record"), dict) else {}
                created = parse_ts(record.get("createdAt")) if isinstance(record.get("createdAt"), str) else None
                found[v["uri"]] = {"likes": v.get("likeCount"), "replies": v.get("replyCount"),
                                   "reposts": v.get("repostCount"),
                                   "created_at": fmt_ts(created) if created else post_time(v["uri"]),
                                   "view": bluesky_view(v)}
            with self.db.transaction() as conn:
                record_totals(conn, "bluesky", found, chunk, now or utcnow())
        return len(due)

    def cache_articles(self, now: str | None = None, rounds: int = 3) -> list[int]:
        """Read the TOP_CACHED most posted articles of each window, if they
        aren't already, and mark them as trending so they're kept. Pages read
        that turn out not to be articles drop out of the ranking, so the ones
        that take their place are read too, for up to `rounds` rounds."""
        wanted: set[int] = set()
        for _ in range(rounds):
            marked, read = self._cache_round(now)
            wanted |= marked
            if not read:
                break
        return sorted(wanted)

    def _cache_round(self, now: str | None) -> tuple[set[int], bool]:
        """One round of cache_articles: (the ids marked, whether any had to be read)."""
        stamp = now or utcnow()
        wanted: dict[int, Any] = {}
        skips = tuple(self.archive_skips())
        with self.db.connect() as conn:
            tops = [ranked_articles(conn, window, now, archive_skips=skips)[:TOP_CACHED] for window in WINDOWS]
        with self.db.transaction() as conn:
            for top in tops:
                for item in top:
                    aid = item["id"]
                    if aid is None:
                        url = articles.candidate(item["url"])
                        if not url:
                            continue
                        conn.execute("INSERT INTO articles(url, first_seen_at, next_attempt_at) VALUES (?,?,?) "
                                     "ON CONFLICT(url) DO NOTHING", (url, stamp, stamp))
                        aid = conn.execute("SELECT id FROM articles WHERE url=?", (url,)).fetchone()["id"]
                        articles.add_keys(conn, aid, [url])
                    conn.execute("UPDATE articles SET trending_at=? WHERE id=?", (stamp, aid))
                    wanted[aid] = True
            pending = [conn.execute("SELECT * FROM articles WHERE id=?", (aid,)).fetchone() for aid in wanted]
        fetcher = self.bouncer.articles
        read = False
        if fetcher.enabled:
            for row in pending:
                if row is not None and row["status"] == "pending" and (row["next_attempt_at"] or "") <= stamp:
                    fetcher.fetch_one(row)
                    read = True
        return set(wanted), read


def archive_skips(bluesky_counted: bool, mastodon_counted: bool) -> tuple[str, ...]:
    """The archive's own posts that a stream counts already: Bluesky's (all of
    it is in Jetstream) and, with your Mastodon server's public timeline,
    hashtags' (they come from it then)."""
    return tuple(d for d, on in ((BSKY_DOMAIN, bluesky_counted), (TAG_DOMAIN, mastodon_counted)) if on)
