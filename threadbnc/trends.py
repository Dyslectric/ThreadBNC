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
import heapq
import json
import logging
import math
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable
from urllib.parse import urlparse

from . import articles, languages, links as links_mod
from . import media as media_mod
from . import thumbs
from .adapters import BSKY_DOMAIN, TAG_DOMAIN, TAG_PREFIX, RemoteError, normalize_tag
from .adapters.activitypub import status_id
from .adapters.bluesky import GET_POSTS, POST
from .db import Conn, Database, fmt_ts, parse_ts, utcnow
from .traffic import tagged

log = logging.getLogger(__name__)

SOURCES = {"bluesky": "Bluesky", "mastodon": "Mastodon"}
ARTICLE_SOURCES = {**SOURCES, "archive": "Archive"}  # articles are also counted from the archive
WINDOWS = {"day": timedelta(days=1), "week": timedelta(days=7), "month": timedelta(days=30)}
POST_WINDOWS = {"hour": timedelta(hours=1), "day": timedelta(days=1), "week": timedelta(days=7)}
RANKINGS = ("rising", *WINDOWS)  # the articles' rankings, and the hashtags'
POST_SORTS = ("likes", "replies", "reposts")  # reposts: boosts, on Mastodon
# App setting (JSON): {"bluesky": count what's posted on Bluesky (Jetstream stays connected),
# "bluesky_likes": "appview" (read the totals of the posts most replied to) | "stream" (count every like)}
SETTINGS = "trends"
LIKES_FROM = ("appview", "stream")
HOUR = "%Y-%m-%dT%H"

TOP_CACHED = 12  # the most posted articles of each window read and kept for reading
RANKED_KEPT = 200  # the most posted of each window kept as ranked (the page shows these, ranked in the background)
RANKING = "trending_rank:"  # app setting, + window: {"at": when, "items": [...]}
STALE_RANKING = timedelta(minutes=45)  # older than this, the page ranks for itself
READ_JOB = "trending_article"
READ_SPACING = timedelta(seconds=20)  # between the articles read, so other work carries on in between
TIDY_BATCH = 2000  # rows forgotten at a time, each lot a short transaction of its own
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
LANG_SHARE = 1 / 3  # a hashtag or link is in your languages unless fewer of its posts were

# Rising: what's posted far more in the past RISING_RECENT than in the week before.
RISING_RECENT = timedelta(hours=6)
RISING_BEFORE = timedelta(days=7)
RISING_MIN = 3  # posts in the past RISING_RECENT, at least
RISING_RATIO = 2.0  # and at least this many times as many as usual
RISING_FLOOR = 10.0  # how little a small count is trusted: see rise()
RISING_BASELINE = 12  # hours counted before RISING_RECENT, at least, for anything to be usual

# History: each day's most used hashtags and most posted links, kept for good.
KEEP_TOP = 1000  # of each network, each day
KEEP_AFTER = timedelta(minutes=5)  # after the day ends, so its last counts are in
HISTORY_THROUGH = "trend_history_through"  # app setting: the last day kept
HOURS_FILLED = "trend_hours_filled"  # app setting: when backfill_hours filled in the hours counted before

# Reading the totals of the posts most replied to (Bluesky: through the AppView).
CHECK_EVERY = 300.0  # seconds between rounds
CHECK_POSTS = 100  # at most this many posts a round (GET_POSTS to a request)
CHECK_LOOKED_AT = 400  # of the posts most talked about lately
CHECK_FRESH = 50  # and of those made in the past hour, which the week's busiest would crowd out
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
    got["bluesky_reposts"] = got.get("bluesky_reposts") is not False  # count reposts from Jetstream (jetstream.py)
    got["mastodon_tags"] = got.get("mastodon_tags") is not False  # ask your servers what's trending on them
    got["fedibuzz"] = got.get("fedibuzz") is True  # count FediBuzz's firehose (fedibuzz.py): ~3 GB a day, so asked for
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
        self.posts: dict[str, list[Any]] = {}  # ref -> [replies, quotes, likes, created_at, reposts]
        self.langs: dict[tuple[str, str, str], int] = {}  # ("tag" | "link", hashtag or link key, language) -> posts
        self.looked: dict[str, int] = {}  # hour -> posts looked at

    def post_tags(self, tags: Iterable[str], author: str | None = None, lang: str | None = None) -> int:
        """Count one post's hashtags (named as followed ones are), each once,
        and once an hour for each `author`, in the post's language `lang`
        (languages.normalize) when it says. Every post the stream brings comes
        through here, so it's also counted as looked at this hour. Returns how
        many hashtags were counted."""
        hour = time.strftime(HOUR, time.gmtime())
        counted = 0
        with self._lock:
            if self._hour != hour:
                self._hour, self._posted = hour, set()
            self.looked[hour] = self.looked.get(hour, 0) + 1
            for tag in dict.fromkeys(tags):
                mark = ("#" + tag, author)
                if not tag or (author and mark in self._posted):
                    continue
                if author:
                    self._posted.add(mark)
                self.tags[(tag, hour)] = self.tags.get((tag, hour), 0) + 1
                if lang:
                    self.langs[("tag", tag, lang)] = self.langs.get(("tag", tag, lang), 0) + 1
                counted += 1
        return counted

    def post_links(self, found: Iterable[tuple[str, str | None, str | None]], author: str | None = None,
                   lang: str | None = None) -> int:
        """Count one post's links, each page once, and once an hour for each
        `author`, in the post's language `lang` when it says. Returns how many
        were counted."""
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
                if lang:
                    self.langs[("link", key, lang)] = self.langs.get(("link", key, lang), 0) + 1
                entry = self.links.get((key, hour))
                if entry is None:
                    self.links[(key, hour)] = [1, url, title, description]
                else:
                    entry[0] += 1
                    entry[2], entry[3] = entry[2] or title, entry[3] or description
        return len(seen)

    def post(self, found: Iterable[tuple[str, str | None, str | None]], tags: Iterable[str],
             author: str | None = None, lang: str | None = None) -> None:
        """Count one post: its links (post_links) and hashtags (post_tags)."""
        self.post_links(found, author, lang)
        self.post_tags(tags, author, lang)

    def _post(self, ref: str, created_at: str | None) -> list[Any]:
        entry = self.posts.get(ref)
        if entry is None:
            entry = self.posts[ref] = [0, 0, 0, created_at, 0]
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

    def repost(self, ref: str, created_at: str | None = None, boost: str | None = None) -> bool:
        """Count one repost (a Mastodon boost) of a post, unless it's over
        MAX_POST_AGE old, or this `boost` (its own id) was counted this hour
        already: the same boost can arrive from FediBuzz and your server's
        timeline. A Mastodon post's `ref` is "<your server>/<its id there>"
        when that's known, else its ActivityPub id (resolved later: see
        remember_refs). Returns whether it was counted."""
        posted = parse_ts(created_at)
        if posted is not None and posted < parse_ts(utcnow()) - MAX_POST_AGE:  # type: ignore[operator]
            return False
        hour = time.strftime(HOUR, time.gmtime())
        with self._lock:
            if self._hour != hour:
                self._hour, self._posted = hour, set()
            if boost:
                if ("boost", boost) in self._posted:
                    return False
                self._posted.add(("boost", boost))
            self._post(ref, created_at)[4] += 1
        return True

    def flush(self, db: Database) -> None:
        with self._lock:
            found, tags, posts, langs, looked = self.links, self.tags, self.posts, self.langs, self.looked
            self._clear()
        if not found and not tags and not posts and not looked:
            return
        now = utcnow()
        # Rows in the same order every time (by their key), so two streams
        # writing at once never wait on each other's rows the other way round.
        tags = dict(sorted(tags.items()))
        found = dict(sorted(found.items()))
        posts = dict(sorted(posts.items()))
        langs = dict(sorted(langs.items()))
        with db.transaction(exclusive=False) as conn:
            if posts:
                posts = _known_refs(conn, self.source, posts)
            conn.executemany(
                "INSERT INTO trend_hours(hour, source, posts) VALUES (?,?,?) ON CONFLICT(hour, source) "
                "DO UPDATE SET posts=trend_hours.posts+excluded.posts",
                [(hour, self.source, n) for hour, n in sorted(looked.items())])
            for kind, table, col in (("tag", "tag_langs", "tag"), ("link", "link_langs", "key")):
                conn.executemany(
                    f"INSERT INTO {table}({col}, lang, posts) VALUES (?,?,?) ON CONFLICT({col}, lang) "
                    f"DO UPDATE SET posts={table}.posts+excluded.posts",
                    [(name, lang, n) for (k, name, lang), n in langs.items() if k == kind])
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
                "likes_seen, reposts_seen) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(source, ref) DO UPDATE SET "
                "replies_seen=stream_posts.replies_seen+excluded.replies_seen, "
                "quotes_seen=stream_posts.quotes_seen+excluded.quotes_seen, "
                "likes_seen=stream_posts.likes_seen+excluded.likes_seen, "
                "reposts_seen=stream_posts.reposts_seen+excluded.reposts_seen, "
                "created_at=COALESCE(stream_posts.created_at, excluded.created_at)",
                [(self.source, ref, e[3], now, e[0], e[1], e[2], e[4]) for ref, e in sorted(posts.items())])


SEEN = "replies_seen + quotes_seen + likes_seen + reposts_seen"  # how much a stream saw of a post


def _known_refs(conn: Conn, source: str, posts: dict[str, list[Any]]) -> dict[str, list[Any]]:
    """The posts counted, those known by their ActivityPub id counted under
    their ref on your server instead, once that's known (remember_refs)."""
    uris = [ref for ref in posts if ref.startswith(("https://", "http://"))]
    known: dict[str, str] = {}
    for chunk in _chunks(uris):
        known.update((r["uri"], r["ref"]) for r in conn.execute(
            f"SELECT uri, ref FROM stream_refs WHERE source=? AND uri IN ({_marks(chunk)})", (source, *chunk)))
    if not known:
        return posts
    out: dict[str, list[Any]] = {}
    for ref, e in posts.items():
        mine = out.setdefault(known.get(ref, ref), [0, 0, 0, None, 0])
        for i in (0, 1, 2, 4):
            mine[i] += e[i]
        mine[3] = mine[3] or e[3]
    return out


def remember_refs(conn: Conn, source: str, found: dict[str, str]) -> None:
    """Posts' refs on your server ({ActivityPub id: ref}), from what it
    said of them: what was counted under the ActivityPub id is added to it."""
    for uri, ref in found.items():
        if not uri or uri == ref:
            continue
        conn.execute("INSERT INTO stream_refs(source, uri, ref) VALUES (?,?,?) ON CONFLICT(source, uri) "
                     "DO UPDATE SET ref=excluded.ref", (source, uri, ref))
        old = conn.execute("SELECT * FROM stream_posts WHERE source=? AND ref=?", (source, uri)).fetchone()
        if old is None:
            continue
        conn.execute(
            "INSERT INTO stream_posts(source, ref, created_at, first_seen_at, replies_seen, quotes_seen, likes_seen, "
            "reposts_seen) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(source, ref) DO UPDATE SET "
            "replies_seen=stream_posts.replies_seen+excluded.replies_seen, "
            "quotes_seen=stream_posts.quotes_seen+excluded.quotes_seen, "
            "likes_seen=stream_posts.likes_seen+excluded.likes_seen, "
            "reposts_seen=stream_posts.reposts_seen+excluded.reposts_seen, "
            "created_at=COALESCE(stream_posts.created_at, excluded.created_at), "
            "first_seen_at=CASE WHEN excluded.first_seen_at < stream_posts.first_seen_at "
            "THEN excluded.first_seen_at ELSE stream_posts.first_seen_at END",
            (source, ref, old["created_at"], old["first_seen_at"], old["replies_seen"], old["quotes_seen"],
             old["likes_seen"], old["reposts_seen"]))
        conn.execute("DELETE FROM stream_posts WHERE source=? AND ref=?", (source, uri))


def unresolved(conn: Conn, source: str, limit: int, least: int, now: str | None = None) -> list[str]:
    """The posts counted by their ActivityPub id alone (boosts FediBuzz
    carries, say) seen at least `least` times, most seen first: those to ask
    your server for."""
    moment = parse_ts(now or utcnow()) or datetime.now(timezone.utc)
    return [r["ref"] for r in conn.execute(
        f"SELECT ref FROM stream_posts WHERE source=? AND gone=0 AND checked_at IS NULL "
        f"AND (ref LIKE 'https://%' OR ref LIKE 'http://%') AND COALESCE(created_at, first_seen_at) >= ? "
        f"AND {SEEN} >= ? ORDER BY {SEEN} DESC, ref LIMIT ?",
        (source, fmt_ts(moment - MAX_POST_AGE), least, limit))]


def tidy(db: Database, now: str | None = None) -> int:
    """Add up old hourly counts by the day, forget links and hashtags posted
    too rarely to trend, and posts too old or too quiet. Done a little at a
    time, each in a short transaction of its own that holds nothing else up:
    these tables are big (hundreds of thousands of links a day). Returns how
    many of the posts forgotten had pictures here."""
    moment = parse_ts(now or utcnow()) or datetime.now(timezone.utc)
    backfill_hours(db, moment)
    keep_days(db, moment)  # before anything's forgotten
    for counts, seen, langs, key in (("link_counts", "links_seen", "link_langs", "key"),
                                     ("tag_counts", "tags_seen", "tag_langs", "tag")):
        _roll_up(db, moment, counts, key)
        once, few = fmt_ts(moment - ONCE_AFTER), fmt_ts(moment - FEW_AFTER)
        _forget(db, seen, [key], "(posts < 2 AND first_seen_at < ?) OR (posts < ? AND first_seen_at < ?) "
                "OR last_seen_at < ?", (once, FEW_POSTS, few, fmt_ts(moment - KEEP_COUNTS)), also=(langs, counts))
        with db.transaction(exclusive=False) as conn:
            conn.execute(f"DELETE FROM {counts} WHERE hour < ?", ((moment - KEEP_COUNTS).strftime(HOUR),))
    gone = _forget(db, "stream_posts", ["source", "ref"],
                   f"COALESCE(created_at, first_seen_at) < ? OR ({SEEN} < ? "
                   "AND first_seen_at < ? AND checked_at IS NULL)",
                   (fmt_ts(moment - KEEP_POSTS), QUIET_SEEN, fmt_ts(moment - QUIET_AFTER)), also=("stream_post_media",))
    with db.transaction(exclusive=False) as conn:  # (a few hundred: the posts your server was asked about)
        conn.execute("DELETE FROM stream_refs WHERE NOT EXISTS (SELECT 1 FROM stream_posts p "
                     "WHERE p.source=stream_refs.source AND p.ref=stream_refs.ref)")
    return gone


def backfill_hours(db: Database, moment: datetime) -> None:
    """The hours counted before trend_hours was kept, as the counts show
    them: each hour with counts in the past DAILY_AFTER, and every hour of
    the days before that (added up by the day already). Once (HOURS_FILLED):
    not "while it's empty", as the streams write this hour's within seconds of
    starting, well before the first tidy. Hours it has already are left as they are."""
    if db.get_setting(HOURS_FILLED):
        return
    with db.connect() as conn:
        daily = (moment - DAILY_AFTER).strftime(HOUR)
        found: set[tuple[str, str]] = set()
        for counts in ("tag_counts", "link_counts"):
            for r in conn.execute(f"SELECT DISTINCT hour, source FROM {counts}"):
                if r["hour"] >= daily:
                    found.add((r["hour"], r["source"]))
                else:
                    found.update((f"{r['hour'][:10]}T{h:02d}", r["source"]) for h in range(24))
    with db.transaction(exclusive=False) as conn:
        conn.executemany("INSERT INTO trend_hours(hour, source, posts) VALUES (?,?,0) "
                         "ON CONFLICT(hour, source) DO NOTHING", sorted(found))
        conn.execute("INSERT INTO app_settings(key, value) VALUES (?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (HOURS_FILLED, utcnow()))
    log.info("Trending: filled in %d hours counted before they were kept, for Rising", len(found))


def keep_days(db: Database, moment: datetime) -> list[str]:
    """Keep each day that's over (KEEP_AFTER ago) and isn't kept yet in
    History: its KEEP_TOP most used hashtags and most posted links on each
    network. Returns the days kept."""
    last = (moment - KEEP_AFTER).date() - timedelta(days=1)  # the last whole day
    through = db.get_setting(HISTORY_THROUGH)
    with db.connect() as conn:
        found = [conn.execute(f"SELECT MIN(hour) FROM {t}").fetchone() for t in ("tag_counts", "link_counts")]
    earliest = min((r[0] for r in found if r and r[0]), default=None)
    if earliest is None:
        return []
    day = datetime.strptime(earliest[:10], "%Y-%m-%d").date()
    if through:
        day = max(day, datetime.strptime(through, "%Y-%m-%d").date() + timedelta(days=1))
    kept = []
    while day <= last:
        keep_day(db, day.isoformat())
        kept.append(day.isoformat())
        log.info("Trending's History: kept %s", day.isoformat())
        day += timedelta(days=1)
    return kept


def keep_day(db: Database, day: str) -> None:
    """Keep one day (UTC, '2026-09-25') in History, in place of whatever was kept of it before."""
    first, last = f"{day}T00", f"{day}T23"
    with db.transaction(exclusive=False) as conn:
        for counts, history, key, langs in (("tag_counts", "tag_history", "tag", "tag_langs"),
                                            ("link_counts", "link_history", "key", "link_langs")):
            conn.execute(f"DELETE FROM {history} WHERE day=?", (day,))
            names: set[str] = set()
            for source in SOURCES:
                top = conn.execute(
                    f"SELECT {key} AS name, SUM(posts) AS posts FROM {counts} WHERE hour >= ? AND hour <= ? "
                    f"AND source=? GROUP BY {key} ORDER BY SUM(posts) DESC, {key} LIMIT ?",
                    (first, last, source, KEEP_TOP)).fetchall()
                conn.executemany(f"INSERT INTO {history}(day, {key}, source, posts) VALUES (?,?,?,?)",
                                 [(day, r["name"], source, int(r["posts"])) for r in top])
                names.update(r["name"] for r in top)
            found = _langs_of(conn, langs, key, sorted(names))
            if key == "tag":
                conn.executemany("INSERT INTO tags_kept(tag, langs) VALUES (?,?) ON CONFLICT(tag) "
                                 "DO UPDATE SET langs=excluded.langs",
                                 [(n, json.dumps(found.get(n) or {})) for n in sorted(names)])
            else:
                conn.executemany(
                    "INSERT INTO links_kept(key, url, title, description, langs) "
                    "SELECT key, url, title, description, ? FROM links_seen WHERE key=? "
                    "ON CONFLICT(key) DO UPDATE SET langs=excluded.langs, "
                    "title=COALESCE(links_kept.title, excluded.title), "
                    "description=COALESCE(links_kept.description, excluded.description)",
                    [(json.dumps(found.get(n) or {}), n) for n in sorted(names)])
        conn.execute("INSERT INTO app_settings(key, value) VALUES (?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (HISTORY_THROUGH, day))


def _langs_of(conn: Conn, table: str, col: str, names: list[str]) -> dict[str, dict[str, int]]:
    """{hashtag or link key: {language: posts}}, from tag_langs or link_langs."""
    out: dict[str, dict[str, int]] = {}
    for chunk in _chunks(names):
        for r in conn.execute(f"SELECT {col} AS name, lang, posts FROM {table} WHERE {col} IN ({_marks(chunk)})",
                              chunk):
            out.setdefault(r["name"], {})[r["lang"]] = int(r["posts"])
    return out


def in_languages(langs: dict[str, int] | None, codes: list[str] | tuple[str, ...]) -> bool:
    """Whether a hashtag or link whose posts were in `langs` ({language:
    posts}) is shown for `codes` (languages.load; none: every one): unless
    fewer than LANG_SHARE of its posts that say their language were in one of them."""
    if not codes or not langs:
        return True
    known = sum(langs.values())
    return not known or sum(n for lang, n in langs.items() if lang in codes) >= LANG_SHARE * known


def _roll_up(db: Database, moment: datetime, counts: str, key: str) -> None:
    """Hourly counts over DAILY_AFTER old added up by the day (into the day's
    'T00' hour), an hour at a time."""
    daily = (moment - DAILY_AFTER).strftime(HOUR)
    with db.connect() as conn:
        hours = [r[0] for r in conn.execute(
            f"SELECT DISTINCT hour FROM {counts} WHERE hour < ? AND hour NOT LIKE '%T00' ORDER BY hour", (daily,))]
    for hour in hours:
        with db.transaction(exclusive=False) as conn:
            conn.execute(
                f"INSERT INTO {counts}({key}, hour, source, posts) SELECT {key}, ?, source, SUM(posts) FROM {counts} "
                f"WHERE hour=? GROUP BY {key}, source ON CONFLICT({key}, hour, source) "
                f"DO UPDATE SET posts={counts}.posts+excluded.posts", (hour[:10] + "T00", hour))
            conn.execute(f"DELETE FROM {counts} WHERE hour=?", (hour,))


def _forget(db: Database, table: str, keys: list[str], where: str, params: tuple[Any, ...],
            also: tuple[str, ...] = ()) -> int:
    """Delete the rows of `table` matching `where`, and the rows of the `also`
    tables with the same `keys`, TIDY_BATCH at a time. Returns how many rows
    of the last `also` table went."""
    gone = 0
    cols = ", ".join(keys)
    with db.connect() as conn:  # all of them in one look, then deleted a batch at a time
        everything = sorted(tuple(r) for r in conn.execute(f"SELECT {cols} FROM {table} WHERE {where}", params))
    for start in range(0, len(everything), TIDY_BATCH):
        rows = everything[start:start + TIDY_BATCH]
        if len(keys) == 1:
            match, values = f"{keys[0]} IN ({_marks(rows)})", [r[0] for r in rows]
        else:  # (source, ref): by source
            groups: dict[Any, list[Any]] = {}
            for first, second in rows:
                groups.setdefault(first, []).append(second)
            match = " OR ".join(f"({keys[0]}=? AND {keys[1]} IN ({_marks(v)}))" for v in groups.values())
            values = [x for first, v in groups.items() for x in (first, *v)]
        with db.transaction(exclusive=False) as conn:
            for other in also:
                removed = conn.execute(f"DELETE FROM {other} WHERE {match}", values).rowcount
                if other == also[-1]:
                    gone += max(removed or 0, 0)
            conn.execute(f"DELETE FROM {table} WHERE {match}", values)
    return gone


# --- ranking articles ---------------------------------------------------------

def _since(window: str, now: str | None) -> str:
    moment = parse_ts(now or utcnow()) or datetime.now(timezone.utc)
    return fmt_ts(moment - WINDOWS[window])


# --- rising ---------------------------------------------------------------------

def rising_span(conn: Conn, now: str | None = None) -> dict[str, Any]:
    """What Rising compares: the past RISING_RECENT (from the start of its
    first hour) with the RISING_BEFORE before it (from the start of its first
    day, as counts over two days old are kept by the day). For each network,
    how many of those hours it was counting (trend_hours), and `factor`: how
    many posts in the recent hours are as many as usual, for each one posted
    before them. A network counted for fewer than RISING_BASELINE hours
    before has no usual (factor 0), and is `new`."""
    moment = parse_ts(now or utcnow()) or datetime.now(timezone.utc)
    recent_from = (moment - RISING_RECENT).replace(minute=0, second=0, microsecond=0)
    since = (moment - RISING_BEFORE).replace(hour=0, minute=0, second=0, microsecond=0)
    this_hour, recent_hour = moment.strftime(HOUR), recent_from.strftime(HOUR)
    part = max((moment - moment.replace(minute=0, second=0, microsecond=0)).total_seconds() / 3600, 1 / 60)
    hours = {source: [0.0, 0] for source in SOURCES}  # [recent, before]
    for r in conn.execute("SELECT hour, source FROM trend_hours WHERE hour >= ? AND hour <= ?",
                          (since.strftime(HOUR), this_hour)):
        if r["source"] in hours:
            if r["hour"] >= recent_hour:
                hours[r["source"]][0] += part if r["hour"] == this_hour else 1
            else:
                hours[r["source"]][1] += 1
    factor = {source: (recent / before if before >= RISING_BASELINE else 0.0)
              for source, (recent, before) in hours.items()}
    factor["archive"] = (moment - recent_from) / (recent_from - since)
    return {"since": since.strftime(HOUR), "recent_from": recent_hour, "posted_from": fmt_ts(recent_from),
            "factor": factor, "hours": {s: round(h[1]) for s, h in hours.items()},
            "new": [s for s, (recent, before) in hours.items() if recent and before < RISING_BASELINE]}


def rise(recent: float, usual: float) -> float | None:
    """How far `recent` posts (in the past RISING_RECENT) are above `usual`
    (as many as the week before had in as long), in something like standard
    deviations: a small count could be chance, so it's trusted less. None
    unless there are RISING_MIN of them, RISING_RATIO times usual."""
    if recent < RISING_MIN or recent < RISING_RATIO * usual:
        return None
    return (recent - usual) / math.sqrt(usual + RISING_FLOOR)


def ranked_articles(conn: Conn, window: str, now: str | None = None, *,
                    archive_skips: tuple[str, ...] = (), keep: int | None = RANKED_KEPT) -> list[dict[str, Any]]:
    """The articles (or pages not read yet) posted in the window, the most
    posted first; or for "rising", those posted far more than usual in the
    past RISING_RECENT (see rising_span), furthest above usual first. See
    the module's notes. The `keep` at the top of each network's ranking
    (order_articles), or all of them, each with the languages of its posts
    (`langs`). `archive_skips`: source domains whose captured posts aren't
    counted from the archive, because a stream counts them already."""
    span = rising_span(conn, now) if window == "rising" else None
    since = _since(window, now) if span is None else span["since"] + ":00:00.000000Z"
    items = _rank(conn, since, archive_skips, span)
    if keep is not None:
        wanted: set[tuple[Any, ...]] = set()
        for source in (None, *ARTICLE_SOURCES):
            wanted.update(_which(i) for i in order_articles(items, source, span is not None, keep=keep))
        items = [i for i in items if _which(i) in wanted]
    _article_langs(conn, items)
    return items


def order_articles(items: list[dict[str, Any]], source: str | None = None, rising: bool = False,
                   codes: list[str] | tuple[str, ...] = (), keep: int | None = None) -> list[dict[str, Any]]:
    """Ranked articles (ranked_articles) as ranked on one network (`source`,
    in ARTICLE_SOURCES), or all of them: the most posted there, or (`rising`)
    those furthest above usual there, each with its `score`, `recent` posts
    and `usual`. Only those posted mostly in `codes` (in_languages), when given;
    the first `keep`, when given. Unchanged as items are."""
    sources = (source,) if source in ARTICLE_SOURCES else tuple(ARTICLE_SOURCES)
    ranked: list[tuple[float, int, dict[str, Any]]] = []
    for item in items:
        if codes and not in_languages(item.get("langs"), codes):
            continue
        if rising:
            recent = sum(item.get("recent_" + s, 0) for s in sources)
            usual = sum(item.get("usual_" + s, 0.0) for s in sources)
            score = rise(recent, usual)
            if score is not None:
                ranked.append((score, recent, {**item, "score": score, "recent": recent, "usual": usual}))
        else:
            posts = sum(item.get(s, 0) for s in sources)
            if posts:
                ranked.append((posts, item.get("archive", 0), item))
    ordered = [i for *_, i in sorted(ranked, key=lambda r: (r[0], r[1]), reverse=True)]  # stable: newer first within
    return ordered if keep is None else ordered[:keep]


def _which(item: dict[str, Any]) -> tuple[Any, ...]:
    """A ranked article, told apart from the others (order_articles copies them when rising)."""
    return tuple(item["article_ids"]), tuple(item["keys"])


def _article_langs(conn: Conn, items: list[dict[str, Any]]) -> None:
    """Each ranked article's `langs`: the languages of the posts that linked
    it on Bluesky and Mastodon (link_langs), and in the archive."""
    found = _langs_of(conn, "link_langs", "key", sorted({k for i in items for k in i["keys"]}))
    for item in items:
        langs: dict[str, int] = {}
        for key in item["keys"]:
            for lang, n in (found.get(key) or {}).items():
                langs[lang] = langs.get(lang, 0) + n
        for lang, n in item.pop("archive_langs", {}).items():
            langs[lang] = langs.get(lang, 0) + n
        item["langs"] = langs


def _chunks(values: list[Any], size: int = 500) -> Iterable[list[Any]]:
    for i in range(0, len(values), size):
        yield values[i:i + size]


def _rank(conn: Conn, since: str, archive_skips: tuple[str, ...],
          span: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    # The archive's passively captured posts, each counted for its current link.
    skip = "".join(" AND t.source_domain != ?" for _ in archive_skips)
    posts = [dict(r) for r in conn.execute(
        f"""SELECT t.id AS tid, t.community_id, o.id AS object_id, o.language,
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
    # The streams' counts, of the links posted most on each network (and, for
    # Rising, furthest above usual there).
    recent_hour = span["recent_from"] if span else "9999"
    counted = [{"key": r["key"], "bluesky": int(r["bluesky"] or 0), "mastodon": int(r["mastodon"] or 0),
                "recent_bluesky": int(r["recent_bluesky"] or 0), "recent_mastodon": int(r["recent_mastodon"] or 0)}
               for r in conn.execute(
        "SELECT key, SUM(CASE WHEN source='bluesky' THEN posts ELSE 0 END) AS bluesky, "
        "SUM(CASE WHEN source='mastodon' THEN posts ELSE 0 END) AS mastodon, "
        "SUM(CASE WHEN source='bluesky' AND hour >= ? THEN posts ELSE 0 END) AS recent_bluesky, "
        "SUM(CASE WHEN source='mastodon' AND hour >= ? THEN posts ELSE 0 END) AS recent_mastodon "
        "FROM link_counts WHERE hour >= ? GROUP BY key", (recent_hour, recent_hour, since[:13])).fetchall()]
    orders: list[Any] = [lambda c: c["bluesky"] + c["mastodon"], lambda c: c["bluesky"], lambda c: c["mastodon"]]
    if span:
        f = span["factor"]

        def rising_on(*sources: str) -> Any:
            return lambda c: rise(sum(c["recent_" + s] for s in sources),
                                  sum((c[s] - c["recent_" + s]) * f[s] for s in sources)) or 0
        orders = [rising_on("bluesky", "mastodon"), rising_on("bluesky"), rising_on("mastodon")]
    streamed: dict[str, dict[str, int]] = {}
    for order in orders:
        for c in heapq.nlargest(RANK_KEYS, counted, key=order):
            if order(c) <= 0:
                break
            streamed[c["key"]] = c
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
        counts = {"bluesky": 0, "mastodon": 0, "recent_bluesky": 0, "recent_mastodon": 0}
        for key in g["keys"]:
            for column in counts:
                counts[column] += streamed.get(key, {}).get(column, 0)
        linked = g["posts"]
        archive = len({p["tid"] for p in linked})
        total = counts["bluesky"] + counts["mastodon"] + archive
        if not total:
            continue
        if span:
            recent_archive = len({p["tid"] for p in linked if (p["posted_at"] or "") >= span["posted_from"]})
            counts["recent_archive"] = recent_archive
            for s, posts in (("bluesky", counts["bluesky"]), ("mastodon", counts["mastodon"]), ("archive", archive)):
                counts["usual_" + s] = (posts - counts["recent_" + s]) * span["factor"][s]  # type: ignore[assignment]
        archive_langs: dict[str, int] = {}
        for p in {p["tid"]: p for p in linked}.values():
            if lang := languages.normalize(p["language"]):
                archive_langs[lang] = archive_langs.get(lang, 0) + 1
        direct: dict[int, int] = {}
        for p in linked:
            direct[p["article_id"]] = direct.get(p["article_id"], 0) + 1
        choices = [rows[aid] for aid in g["article_ids"] if aid in rows]
        if choices and all(a["status"] == "skipped" for a in choices):
            continue  # read, and not an article: a feed, a chat invite, too little text
        top_key = max(g["keys"], key=lambda k: (_posted(streamed.get(k)), k)) if g["keys"] else None
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
        item.update(counts)
        item.update(article_ids=sorted(g["article_ids"]), keys=sorted(g["keys"]), top_key=top_key,
                    archive=archive, total=total, archive_langs=archive_langs,
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


def _posted(counted: dict[str, Any] | None) -> int:
    return (counted["bluesky"] + counted["mastodon"]) if counted else 0


def _marks(values: list[Any]) -> str:
    return ",".join("?" * len(values))


def store_ranking(db: Database, ranked: dict[str, list[dict[str, Any]]], now: str) -> None:
    """Keep each window's ranking (ranked_articles: the RANKED_KEPT at the top
    on each network) for the page, which reads it rather than ranking while you wait."""
    with db.transaction(exclusive=False) as conn:
        for window, items in ranked.items():
            conn.execute("INSERT INTO app_settings(key, value) VALUES (?, ?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                         (RANKING + window, json.dumps({"at": now, "items": items}, default=str)))


def stored_ranking(conn: Conn, window: str, now: str | None = None) -> list[dict[str, Any]] | None:
    """A window's ranking as last kept (store_ranking), unless it's stale."""
    row = conn.execute("SELECT value FROM app_settings WHERE key=?", (RANKING + window,)).fetchone()
    try:
        kept = json.loads(row[0]) if row and row[0] else None
    except ValueError:
        return None
    at = parse_ts(kept.get("at")) if isinstance(kept, dict) else None
    moment = parse_ts(now or utcnow()) or datetime.now(timezone.utc)
    if at is None or moment - at > STALE_RANKING:
        return None
    return kept.get("items") or []


def trending_articles(conn: Conn, items: list[dict[str, Any]], page: int = 1,
                      per_page: int = 25) -> tuple[list[dict[str, Any]], bool]:
    """A page of `items` (ranked_articles), with what the page shows of each:
    the start of its text and its first picture, when it's been read."""
    page = max(1, page)
    start = (page - 1) * per_page
    shown = [dict(i) for i in items[start:start + per_page]]
    ids = sorted({aid for item in shown for aid in item["article_ids"]})
    if ids:
        now_ = {r["id"]: dict(r) for r in conn.execute(
            f"SELECT id, status, title, byline, site_name, published, word_count, fetched_from, canonical_url, "
            f"fetched_at FROM articles WHERE id IN ({_marks(ids)})", ids)}
        for item in shown:
            if item["id"] in now_:
                fresh = now_[item["id"]]
                item.update({k: v for k, v in fresh.items() if v is not None or k == "status"})
        shown = [i for i in shown if i.get("status") != "skipped"]
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
                  now: str | None = None, source: str | None = None,
                  codes: list[str] | tuple[str, ...] = ()) -> tuple[list[dict[str, Any]], bool]:
    """The hashtags used in the most posts in the window, on Bluesky and
    Mastodon (or ranked by one `source` alone: Bluesky's far bigger stream
    would bury Mastodon's), with the hashtag's community here when it's
    followed. Only those used mostly in `codes` (in_languages), when given.
    The window "rising" is rising_tags'."""
    if window == "rising":
        return rising_tags(conn, page, per_page, now, source, codes)
    since = _since(window, now)
    page = max(1, page)
    rank = f"SUM(CASE WHEN source='{source}' THEN posts ELSE 0 END)" if source in SOURCES else "SUM(posts)"
    skip, want, batch, offset = (page - 1) * per_page, per_page + 1, max(per_page + 1, 500 if codes else 0), 0
    shown: list[dict[str, Any]] = []
    while len(shown) < skip + want:
        rows = conn.execute(
            "SELECT tag, SUM(CASE WHEN source='bluesky' THEN posts ELSE 0 END) AS bluesky, "
            "SUM(CASE WHEN source='mastodon' THEN posts ELSE 0 END) AS mastodon, SUM(posts) AS total "
            f"FROM tag_counts WHERE hour >= ? GROUP BY tag HAVING {rank} > 0 ORDER BY {rank} DESC, SUM(posts) DESC, tag "
            "LIMIT ? OFFSET ?",
            (since[:13], batch if codes else skip + want, offset)).fetchall()
        offset += len(rows)
        found = [{"tag": r["tag"], "bluesky": int(r["bluesky"] or 0), "mastodon": int(r["mastodon"] or 0),
                  "total": int(r["total"] or 0)} for r in rows]
        shown.extend(_in_languages(conn, found, codes))
        if not codes or len(rows) < batch:
            break
    items = shown[skip:skip + per_page]
    return _here(conn, items), len(shown) > skip + per_page


def rising_tags(conn: Conn, page: int = 1, per_page: int = 50, now: str | None = None, source: str | None = None,
                codes: list[str] | tuple[str, ...] = ()) -> tuple[list[dict[str, Any]], bool]:
    """The hashtags used far more than usual in the past RISING_RECENT
    (rising_span, rise), on Bluesky and Mastodon or on one `source`,
    furthest above usual first: {"tag", "bluesky"/"mastodon"/"total" (posts
    in the past RISING_RECENT), "usual", "score"}."""
    span = rising_span(conn, now)
    sources = (source,) if source in SOURCES else tuple(SOURCES)
    recent = " + ".join(f"SUM(CASE WHEN source='{s}' AND hour >= ? THEN posts ELSE 0 END)" for s in sources)
    rows = conn.execute(
        "SELECT tag, SUM(CASE WHEN source='bluesky' AND hour >= ? THEN posts ELSE 0 END) AS recent_bluesky, "
        "SUM(CASE WHEN source='mastodon' AND hour >= ? THEN posts ELSE 0 END) AS recent_mastodon, "
        "SUM(CASE WHEN source='bluesky' AND hour < ? THEN posts ELSE 0 END) AS before_bluesky, "
        "SUM(CASE WHEN source='mastodon' AND hour < ? THEN posts ELSE 0 END) AS before_mastodon "
        f"FROM tag_counts WHERE hour >= ? GROUP BY tag HAVING {recent} >= ?",
        (*[span["recent_from"]] * 4, span["since"], *[span["recent_from"]] * len(sources), RISING_MIN)).fetchall()
    scored = []
    for r in rows:
        posts = {s: int(r["recent_" + s] or 0) for s in SOURCES}
        usual = sum(int(r["before_" + s] or 0) * span["factor"][s] for s in sources)
        count = sum(posts[s] for s in sources)
        score = rise(count, usual)
        if score is not None:
            scored.append({"tag": r["tag"], "bluesky": posts["bluesky"], "mastodon": posts["mastodon"],
                           "total": count, "usual": usual, "score": score})
    scored.sort(key=lambda t: (-t["score"], -t["total"], t["tag"]))
    shown = _in_languages(conn, scored, codes)
    page = max(1, page)
    items = shown[(page - 1) * per_page:page * per_page]
    return _here(conn, items), len(shown) > page * per_page


def _in_languages(conn: Conn, tags: list[dict[str, Any]], codes: list[str] | tuple[str, ...]) -> list[dict[str, Any]]:
    """Those of `tags` ({"tag": ...}) used mostly in `codes` (in_languages)."""
    if not codes:
        return tags
    found = _langs_of(conn, "tag_langs", "tag", [t["tag"] for t in tags])
    return [t for t in tags if in_languages(found.get(t["tag"]), codes)]


def _here(conn: Conn, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Hashtags ({"tag": ...}) with their community here, when there is one,
    and whether it's followed."""
    for item in items:
        item["community_id"], item["following"] = None, False
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
    return items


HEAT_HOURS = 24  # Rising's heat: the hours up to now, the past RISING_RECENT of them marked
HEAT_AROUND = 3  # a History day's heat: the days either side of it
HEAT_LEAST = 4  # a bar's height (of 100) when it has any posts, so it shows


def tag_heat(conn: Conn, tags: list[str], window: str = "day", source: str | None = None,
             now: str | None = None, day: str | None = None) -> dict[str, Any]:
    """How much each of `tags` was used over the time a Tags ranking covers,
    on `source` or both, for a histogram: by the hour for Rising
    (HEAT_HOURS, the past RISING_RECENT `marked`) and the past day, by the
    day for the past week or month, and for a History `day`, the days
    HEAT_AROUND either side of it (it `marked`). {"hourly", "cells":
    [{"at": '2026-09-26T14' or '2026-09-26', "marked", "counted": whether
    the streams were counting then}], "tags": {tag: {"cells": [{"posts",
    "height" (0 to 100, of that tag's busiest)}, ...], "busiest": its
    busiest cell's index, if any}}}."""
    moment = parse_ts(now or utcnow()) or datetime.now(timezone.utc)
    sources = (source,) if source in SOURCES else tuple(SOURCES)
    this_hour = moment.replace(minute=0, second=0, microsecond=0)
    if day:
        middle = datetime.strptime(day, "%Y-%m-%d").date()
        at = [(middle + timedelta(days=d)).isoformat() for d in range(-HEAT_AROUND, HEAT_AROUND + 1)]
        marked = {day}
    elif window in ("rising", "day"):
        first = this_hour - (timedelta(hours=HEAT_HOURS - 1) if window == "rising" else WINDOWS["day"])
        hours = int((this_hour - first) / timedelta(hours=1)) + 1
        at = [(first + timedelta(hours=h)).strftime(HOUR) for h in range(hours)]
        recent = (moment - RISING_RECENT).strftime(HOUR)
        marked = {a for a in at if a >= recent} if window == "rising" else set()
    else:
        today = moment.date()
        at = [(today - timedelta(days=d)).isoformat() for d in range(WINDOWS[window].days - 1, -1, -1)]
        marked = set()
    hourly = len(at[0]) > 10
    width = 13 if hourly else 10
    posts: dict[str, dict[str, int]] = {t: {} for t in tags}
    marks = _marks(list(sources))
    for chunk in _chunks(tags):
        if day:
            rows = conn.execute(
                f"SELECT tag, day AS at, SUM(posts) AS posts FROM tag_history WHERE day >= ? AND day <= ? "
                f"AND source IN ({marks}) AND tag IN ({_marks(chunk)}) GROUP BY tag, day",
                (at[0], at[-1], *sources, *chunk))
        else:
            rows = conn.execute(
                f"SELECT tag, substr(hour, 1, {width}) AS at, SUM(posts) AS posts FROM tag_counts "
                f"WHERE hour >= ? AND source IN ({marks}) AND tag IN ({_marks(chunk)}) GROUP BY tag, at",
                (at[0] if hourly else at[0] + "T00", *sources, *chunk))
        for r in rows:
            posts[r["tag"]][r["at"]] = int(r["posts"] or 0)
    counted = {r["at"] for r in conn.execute(
        f"SELECT DISTINCT substr(hour, 1, {width}) AS at FROM trend_hours WHERE hour >= ? AND hour <= ? "
        f"AND source IN ({marks})", (at[0] if hourly else at[0] + "T00", at[-1] if hourly else at[-1] + "T23",
                                     *sources))}
    out: dict[str, dict[str, Any]] = {}
    for tag in tags:
        counts = [posts[tag].get(a, 0) for a in at]
        busiest = max(counts, default=0)
        out[tag] = {"busiest": counts.index(busiest) if busiest else None,
                    "cells": [{"posts": n, "height": max(HEAT_LEAST, round(100 * n / busiest)) if n else 0}
                              for n in counts]}
    return {"hourly": hourly, "tags": out,
            "cells": [{"at": a, "marked": a in marked, "counted": a in counted} for a in at]}


def server_tags(got: Any) -> list[dict[str, Any]]:
    """The hashtags a Mastodon server says are trending on it
    (/api/v1/trends/tags), in its order: {"tag" (named as followed ones are),
    "name" (as it writes it), "recent"/"week" (posts in the past two days, as
    its own app shows, and seven), "people_recent"/"people_week" (each day's
    people, added up)}. Its history is a day (UTC) at a time, today first."""
    out: list[dict[str, Any]] = []
    for t in got if isinstance(got, list) else []:
        if not isinstance(t, dict):
            continue
        try:
            tag = normalize_tag(str(t.get("name") or ""))
        except ValueError:
            continue
        days = [h for h in t.get("history") or [] if isinstance(h, dict)][:7]
        if tag not in {o["tag"] for o in out}:
            out.append({"tag": tag, "name": str(t["name"])[:100],
                        "recent": sum(_count(d, "uses") for d in days[:2]),
                        "people_recent": sum(_count(d, "accounts") for d in days[:2]),
                        "week": sum(_count(d, "uses") for d in days),
                        "people_week": sum(_count(d, "accounts") for d in days)})
    return out


def _count(day: dict[str, Any], key: str) -> int:
    try:
        return int(day.get(key) or 0)
    except (TypeError, ValueError):
        return 0


def record_server_tags(conn: Conn, domain: str, tags: list[dict[str, Any]], now: str) -> None:
    """What a server said is trending on it, in place of what it said before."""
    conn.execute("DELETE FROM server_trending_tags WHERE domain=?", (domain,))
    conn.executemany(
        "INSERT INTO server_trending_tags(domain, place, tag, name, recent, people_recent, week, people_week, read_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        [(domain, i, t["tag"], t["name"], t["recent"], t["people_recent"], t["week"], t["people_week"], now)
         for i, t in enumerate(tags)])


def forget_server_tags(conn: Conn, keep: list[str]) -> None:
    """Forget what servers you're no longer signed in to said was trending."""
    conn.execute("DELETE FROM server_trending_tags" + (f" WHERE domain NOT IN ({_marks(keep)})" if keep else ""), keep)


def trending_on_servers(conn: Conn) -> list[dict[str, Any]]:
    """What each server you're signed in to last said is trending on it:
    [{"domain", "read_at", "tags": [...]}], in the order the servers were signed in to."""
    rows = conn.execute("SELECT s.* FROM server_trending_tags s LEFT JOIN "
                        "(SELECT domain, MIN(id) AS first FROM accounts WHERE software='mastodon' GROUP BY domain) a "
                        "ON a.domain=s.domain ORDER BY a.first, s.domain, s.place").fetchall()
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        server = out.setdefault(r["domain"], {"domain": r["domain"], "read_at": r["read_at"], "tags": []})
        server["tags"].append({k: r[k] for k in ("tag", "name", "recent", "people_recent", "week", "people_week")})
    for server in out.values():
        _here(conn, server["tags"])
    return list(out.values())


# --- history ------------------------------------------------------------------------

def history_days(conn: Conn) -> tuple[str | None, str | None]:
    """The first and last days kept in History (keep_day), if any."""
    r = conn.execute("SELECT MIN(day) AS first, MAX(day) AS last FROM tag_history").fetchone()
    s = conn.execute("SELECT MIN(day) AS first, MAX(day) AS last FROM link_history").fetchone()
    firsts = [x for x in (r["first"], s["first"]) if x]
    lasts = [x for x in (r["last"], s["last"]) if x]
    return (min(firsts) if firsts else None), (max(lasts) if lasts else None)


def counted_hours(conn: Conn, day: str) -> dict[str, int]:
    """How many of a day's hours each network was counted (trend_hours)."""
    out = {source: 0 for source in SOURCES}
    for r in conn.execute("SELECT source, COUNT(*) AS n FROM trend_hours WHERE hour >= ? AND hour <= ? "
                          "GROUP BY source", (f"{day}T00", f"{day}T23")):
        if r["source"] in out:
            out[r["source"]] = int(r["n"])
    return out


def _by_source(conn: Conn, history: str, key: str, day: str) -> dict[str, dict[str, int]]:
    """{hashtag or link key: {source: posts}} as kept of a day."""
    out: dict[str, dict[str, int]] = {}
    for r in conn.execute(f"SELECT {key} AS name, source, posts FROM {history} WHERE day=?", (day,)):
        out.setdefault(r["name"], {s: 0 for s in SOURCES})[r["source"]] = int(r["posts"])
    return out


def _kept_langs(value: Any) -> dict[str, int]:
    try:
        got = json.loads(value) if value else {}
    except ValueError:
        return {}
    return got if isinstance(got, dict) else {}


def tags_on(conn: Conn, day: str, source: str | None = None, page: int = 1, per_page: int = 50,
            codes: list[str] | tuple[str, ...] = ()) -> tuple[list[dict[str, Any]], bool]:
    """The hashtags used most on a day kept in History, as trending_tags lists them."""
    counted = _by_source(conn, "tag_history", "tag", day)
    langs = {}
    for chunk in _chunks(sorted(counted)):
        langs.update((r["tag"], _kept_langs(r["langs"])) for r in conn.execute(
            f"SELECT tag, langs FROM tags_kept WHERE tag IN ({_marks(chunk)})", chunk))
    sources = (source,) if source in SOURCES else tuple(SOURCES)
    items = [{"tag": tag, "bluesky": n["bluesky"], "mastodon": n["mastodon"], "total": sum(n.values()),
              "ranked": sum(n[s] for s in sources)} for tag, n in counted.items()
             if in_languages(langs.get(tag), codes)]
    items = [i for i in items if i["ranked"]]
    items.sort(key=lambda i: (-i["ranked"], -i["total"], i["tag"]))
    page = max(1, page)
    return _here(conn, items[(page - 1) * per_page:page * per_page]), len(items) > page * per_page


def links_on(conn: Conn, day: str, source: str | None = None,
             codes: list[str] | tuple[str, ...] = ()) -> list[dict[str, Any]]:
    """The links posted most on a day kept in History, as ranked articles
    (for trending_articles): the page as first posted, or the article read
    here from it while it's still here, with links to the same article counted together."""
    counted = _by_source(conn, "link_history", "key", day)
    keys = sorted(counted)
    kept: dict[str, Any] = {}
    article_of: dict[str, int] = {}
    for chunk in _chunks(keys):
        kept.update((r["key"], dict(r)) for r in conn.execute(
            f"SELECT * FROM links_kept WHERE key IN ({_marks(chunk)})", chunk))
        article_of.update((r["key"], r["article_id"]) for r in conn.execute(
            f"SELECT key, MIN(article_id) AS article_id FROM article_keys WHERE key IN ({_marks(chunk)}) GROUP BY key",
            chunk))
    ids = sorted(set(article_of.values()))
    found: dict[int, dict[str, Any]] = {}
    for chunk in _chunks(ids):
        found.update((r["id"], dict(r)) for r in conn.execute(
            f"SELECT id, url, status, title, byline, site_name, published, word_count, fetched_from, canonical_url "
            f"FROM articles WHERE id IN ({_marks(chunk)})", chunk))
    groups: dict[Any, dict[str, Any]] = {}
    for key in keys:
        aid = article_of.get(key) if article_of.get(key) in found else None
        g = groups.setdefault(("a", aid) if aid else ("k", key), {"keys": [], "counts": {s: 0 for s in SOURCES},
                                                                  "langs": {}, "article": found.get(aid)})
        g["keys"].append(key)
        for s, n in counted[key].items():
            g["counts"][s] += n
        for lang, n in _kept_langs((kept.get(key) or {}).get("langs")).items():
            g["langs"][lang] = g["langs"].get(lang, 0) + n
    items = []
    for g in groups.values():
        top = max(g["keys"], key=lambda k: (sum(counted[k].values()), k))
        first = kept.get(top) or {}
        article = g["article"]
        item = dict(article) if article else {
            "id": None, "url": first.get("url") or top, "status": "pending", "title": first.get("title"),
            "byline": None, "site_name": None, "published": None, "word_count": None, "fetched_from": None,
            "canonical_url": None}
        item["title"] = item.get("title") or first.get("title")
        item.update(article_ids=[article["id"]] if article else [], keys=g["keys"], top_key=top,
                    description=first.get("description"), bluesky=g["counts"]["bluesky"],
                    mastodon=g["counts"]["mastodon"], archive=0, total=sum(g["counts"].values()),
                    langs=g["langs"], latest_post=None, latest_posted_at=None, first_seen_at=None)
        items.append(item)
    items.sort(key=lambda i: (i["total"], i["top_key"]), reverse=True)
    return order_articles(items, source, codes=codes)


# --- ranking posts ------------------------------------------------------------

def trending_posts(conn: Conn, window: str = "day", sort: str = "likes", source: str | None = None,
                   page: int = 1, per_page: int = 25, now: str | None = None,
                   codes: list[str] | tuple[str, ...] = (), videos: bool = False) -> tuple[list[dict[str, Any]], bool]:
    """The posts made in the window most liked (or most replied to, or most
    reposted: boosted, on Mastodon) on Bluesky and Mastodon, of those whose
    totals have been read. A total not read yet is what the stream counted. Only those in `codes`, or that don't say, when given.
    `videos`: only those with a video that can play here (the Loops page)."""
    moment = parse_ts(now or utcnow()) or datetime.now(timezone.utc)
    since = fmt_ts(moment - POST_WINDOWS.get(window, POST_WINDOWS["day"]))
    score = {"likes": "COALESCE(likes, likes_seen)", "reposts": "COALESCE(reposts, reposts_seen)"}.get(
        sort, "COALESCE(replies, replies_seen)")
    where, params = "", [since]
    if source in SOURCES:
        where, params = " AND source=?", [since, source]
    shown_lang, lang_params = languages.shown_sql(list(codes), "lang")
    where, params = f"{where} AND {shown_lang}", [*params, *lang_params]
    if videos:
        where, params = where + " AND view_json LIKE ?", [*params, '%"video_src": "%']
    page = max(1, page)
    # The page's posts are chosen from the keys and scores alone, then read whole (each carries its
    # view_json): sorting a day's posts with their text would move megabytes to keep 25 of them.
    rows = conn.execute(
        f"SELECT s.*, t.score FROM (SELECT source, ref, {score} AS score, created_at FROM stream_posts "
        f"WHERE view_json IS NOT NULL AND gone=0 AND created_at >= ?{where} "
        f"ORDER BY score DESC, created_at DESC LIMIT ? OFFSET ?) t "
        f"JOIN stream_posts s ON s.source=t.source AND s.ref=t.ref ORDER BY t.score DESC, s.created_at DESC",
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
        item["reposts"] = r["reposts"] if r["reposts"] is not None else r["reposts_seen"]
        item["key"] = post_key(r["source"], r["ref"])
        out.append(item)
    shown = pictures_of(conn, [(i["source"], i["ref"]) for i in out])
    for item in out:
        item["pictures"], item["pictures_waiting"] = shown.get((item["source"], item["ref"]), ([], False))
        view = item["view"]
        item["pictures_waiting"] = item["pictures_waiting"] or not item["pictures"] and (
            bool(view.get("images")) or (unread_pictures(view)))
    return out, len(rows) > per_page


def unread_pictures(view: dict[str, Any]) -> bool:
    """Whether a post was last read before its pictures' addresses, or the post
    it quotes, were kept (it's read again when it's shown: see the Trending
    page's /trending/pictures)."""
    if view.get("quote") and "quoted" not in view:
        return True
    return "images" not in view and bool(view.get("pictures") or view.get("video") or view.get("link"))


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
                f"JOIN media m ON m.id=s.media_id WHERE ({where}) AND s.position >= 0 ORDER BY s.position",
                [v for pair in chunk for v in pair]).fetchall():
            ready, waiting = out.get((r["source"], r["ref"]), ([], False))
            if r["status"] == "ok" and (r["content_type"] or "").startswith("image/"):
                ready = [*ready, r["id"]]
            waiting = waiting or r["status"] == "pending"
            out[(r["source"], r["ref"])] = (ready, waiting)
    return out


def due_checks(conn: Conn, source: str, limit: int, now: str | None = None, prefix: str = "") -> list[Any]:
    """The posts, of those talked about most lately, whose totals are due to
    be read (again): see RECHECK. Only those whose ref starts with `prefix`
    (a Mastodon server's: "<server>/"), when given. The past hour's most
    talked about (CHECK_FRESH) are looked at too, for Trending's Hour, and
    have up to a quarter of the `limit` first."""
    moment = parse_ts(now or utcnow()) or datetime.now(timezone.utc)

    def busiest(since: timedelta, most: int) -> list[Any]:
        return conn.execute(
            "SELECT * FROM stream_posts WHERE source=? AND gone=0 AND COALESCE(created_at, first_seen_at) >= ? "
            f"AND substr(ref, 1, ?) = ? ORDER BY {SEEN} + COALESCE(likes, 0) DESC LIMIT ?",
            (source, fmt_ts(moment - since), len(prefix), prefix, most)).fetchall()

    def is_due(r: Any) -> bool:
        age = moment - (parse_ts(r["created_at"] or r["first_seen_at"]) or moment)
        every = next((e for limit_age, e in RECHECK if age < limit_age), RECHECK[-1][1])
        checked = parse_ts(r["checked_at"])
        # a video read before its file's address was kept (video_src) is read again now, for Loops
        unplayable = '"video": true' in (r["view_json"] or "") and '"video_src"' not in r["view_json"]
        # and so is one that quotes a post, read before that post was kept
        unquoted = '"quote": true' in (r["view_json"] or "") and '"quoted"' not in r["view_json"]
        return checked is None or unplayable or unquoted or moment - checked >= every

    rows = busiest(MAX_POST_AGE, CHECK_LOOKED_AT)
    looked = {r["ref"] for r in rows}
    fresh = [r for r in busiest(POST_WINDOWS["hour"], CHECK_FRESH) if r["ref"] not in looked and is_due(r)]
    first = fresh[:max(1, limit // 4)]
    due = [*first, *(r for r in rows if is_due(r)), *fresh[len(first):]]
    return due[:limit]


def record_totals(conn: Conn, source: str, found: dict[str, dict[str, Any]], asked: list[str], now: str) -> None:
    """Totals read for posts: {ref: {likes, replies, reposts, created_at, view}}. Those asked for and
    not found are gone (deleted, or hidden)."""
    for ref, t in found.items():
        conn.execute(
            "INSERT INTO stream_posts(source, ref, created_at, first_seen_at, likes, replies, reposts, checked_at, "
            "view_json, lang) VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(source, ref) DO UPDATE SET "
            "likes=excluded.likes, replies=excluded.replies, reposts=excluded.reposts, "
            "checked_at=excluded.checked_at, view_json=excluded.view_json, lang=excluded.lang, gone=0, "
            "created_at=COALESCE(stream_posts.created_at, excluded.created_at)",
            (source, ref, t.get("created_at"), now, t.get("likes"), t.get("replies"), t.get("reposts"), now,
             json.dumps(t["view"]), languages.normalize(t["view"].get("lang"))))
    if source == "mastodon":
        remember_refs(conn, source, {t["view"].get("uri"): ref for ref, t in found.items() if t["view"].get("uri")})
    missing = [ref for ref in asked if ref not in found]
    for chunk in _chunks(missing):
        conn.execute(f"UPDATE stream_posts SET gone=1, checked_at=? WHERE source=? AND ref IN ({_marks(chunk)})",
                     (now, source, *chunk))


VIDEO_CID = re.compile(r"[A-Za-z0-9]{10,120}")
VIDEO_DID = re.compile(r"did:(?:plc:[a-z0-9]{5,40}|web:[A-Za-z0-9.-]{1,253})")


def bluesky_quoted_view(rec: Any) -> dict[str, Any] | None:
    """The post a post quotes, as the Trending page shows it (None when it
    quotes nothing; {"gone": True} when it can't be seen)."""
    from .adapters.bluesky import _Content, web_url

    if rec is None:
        return None
    if not isinstance(rec, dict) or not isinstance(rec.get("value"), dict) or not rec.get("uri"):
        return {"gone": True}
    author = rec.get("author") or {}
    embeds = rec.get("embeds") or []
    inner = _Content(rec["value"], embeds[0] if embeds else None)
    return {"url": web_url(rec["uri"]), "text": inner.text[:1000], "handle": author.get("handle") or author.get("did"),
            "name": author.get("displayName") or None,
            "author_url": f"https://{BSKY_DOMAIN}/profile/{author.get('did') or author.get('handle')}",
            "link_title": inner.link_title, "pictures": len(inner.pictures), "video": inner.video}


def bluesky_view(view: dict[str, Any]) -> dict[str, Any]:
    """What the Trending page shows of a Bluesky post, from its AppView view."""
    from .adapters.bluesky import NSFW_LABELS, _Content, web_url

    record = view.get("record") if isinstance(view.get("record"), dict) else {}
    content = _Content(record, view.get("embed"))
    author = view.get("author") or {}
    labels = {str(x.get("val")) for x in view.get("labels") or [] if isinstance(x, dict)}
    return {"url": web_url(view["uri"]), "text": content.text[:3000], "sensitive": bool(labels & NSFW_LABELS),
            "handle": author.get("handle") or author.get("did"), "name": author.get("displayName") or None,
            "author_url": f"https://{BSKY_DOMAIN}/profile/{author.get('did') or author.get('handle')}",
            "avatar": author.get("avatar") if isinstance(author.get("avatar"), str) else None,
            "link": content.link, "link_title": content.link_title, "pictures": len(content.pictures),
            "video": content.video, "video_src": bluesky_video_src(view, record) if content.video else None,
            "quote": content.quote is not None, "quoted": bluesky_quoted_view(content.quote),
            "lang": record_lang(record),
            "images": (content.pictures or ([content.cover] if content.cover else []))[:PICTURES]}


def bluesky_video_src(view: dict[str, Any], record: dict[str, Any]) -> str | None:
    """Where a Bluesky post's video plays from: /trending/video, which sends the browser to the file on its
    author's server (the AppView only gives an HLS playlist, which most browsers can't play on their own)."""
    embed = record.get("embed") if isinstance(record.get("embed"), dict) else {}
    media = embed.get("media") if isinstance(embed.get("media"), dict) else embed
    blob = media.get("video") if isinstance(media.get("video"), dict) else {}
    ref = blob.get("ref") if isinstance(blob.get("ref"), dict) else {}
    cid, did = ref.get("$link"), (view.get("author") or {}).get("did")
    if isinstance(cid, str) and isinstance(did, str) and VIDEO_CID.fullmatch(cid) and VIDEO_DID.fullmatch(did):
        return f"/trending/video?did={did}&cid={cid}"
    return None


def record_lang(record: dict[str, Any]) -> str | None:
    """A Bluesky post record's language: its first "langs"."""
    langs = record.get("langs")
    return languages.normalize(langs[0]) if isinstance(langs, list) and langs else None


# --- upkeep -----------------------------------------------------------------------

class Trends:
    """The bouncer's side of trending: tidying the counts, reading the totals
    of the Bluesky posts most talked about, and reading (and keeping, while
    they stay there) the most posted articles. A bouncer hook."""

    def __init__(self, bouncer: Any, archive_skips: Any = None):
        self.bouncer, self.db = bouncer, bouncer.db
        # () -> the source domains a stream counts already (see ranked_articles).
        self.archive_skips = archive_skips or (lambda: ())
        self._tidied = self._checked = self._cached = float("-inf")
        self._tidying = threading.Lock()
        bouncer.hooks.append(self.upkeep)
        bouncer.job_handlers[READ_JOB] = self.read_article

    def upkeep(self) -> None:
        now = time.monotonic()
        if now - self._tidied >= TIDY_EVERY:
            self._tidied = now
            # A few seconds of database work an hour: on a thread of its own, so the
            # bouncer's one worker (opening posts, reading comments) isn't held up.
            threading.Thread(target=self.tidy_now, name="trends-tidy", daemon=True).start()
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

    def tidy_now(self) -> None:
        """tidy(), and the pictures of the posts it forgot. One at a time."""
        if not self._tidying.acquire(blocking=False):
            return
        try:
            if tidy(self.db):  # posts with pictures went: their pictures go too
                with self.db.transaction() as conn:
                    files = media_mod.collect_orphans(conn, self.bouncer.media_dir)
                for f in files:  # only after that committed
                    f.unlink(missing_ok=True)
                    thumbs.remove_for(self.bouncer.media_dir, f.stem)
        except Exception:  # tried again in an hour
            log.exception("tidying the trends' counts failed")
        finally:
            self._tidying.release()

    def check_bluesky(self, now: str | None = None) -> int:
        """Read the totals of the Bluesky posts most talked about lately, and
        who posted them and what they say, from the AppView."""
        with self.db.connect() as conn:
            due = [r["ref"] for r in due_checks(conn, "bluesky", CHECK_POSTS, now)]
        self.read_bluesky(due, now)
        return len(due)

    def read_bluesky(self, refs: list[str], now: str | None = None) -> None:
        """Read these Bluesky posts (at:// addresses) from the AppView: their
        totals, and who posted them and what they say and show."""
        adapter = self.bouncer.bluesky_adapter
        for chunk in _chunks(refs, GET_POSTS):
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
            with self.db.transaction(exclusive=False) as conn:
                record_totals(conn, "bluesky", found, chunk, now or utcnow())

    def cache_articles(self, now: str | None = None) -> list[int]:
        """Rank the articles of each window, and keep the ranking for the page
        (store_ranking). Mark the TOP_CACHED most posted of each as trending so
        they're kept, and read the ones not read yet: each a job of its own,
        READ_SPACING apart, so opening posts and the like go on meanwhile. A
        page read that isn't an article drops out of the next ranking, and the
        next one in is read then."""
        stamp = now or utcnow()
        wanted: dict[int, Any] = {}
        skips = tuple(self.archive_skips())
        with self.db.connect() as conn:
            ranked = {window: ranked_articles(conn, window, now, archive_skips=skips) for window in RANKINGS}
        store_ranking(self.db, ranked, stamp)
        tops = [order_articles(items, source, window == "rising", keep=TOP_CACHED)
                for window, items in ranked.items() for source in (None, *ARTICLE_SOURCES)]
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
            pending = [aid for aid in wanted if conn.execute(
                "SELECT 1 FROM articles WHERE id=? AND status='pending'", (aid,)).fetchone()]
            queued = {json.loads(r[0]).get("article_id") for r in conn.execute(
                "SELECT payload_json FROM jobs WHERE kind=? AND status IN ('queued', 'running')", (READ_JOB,))}
        if self.bouncer.articles.enabled:
            for i, aid in enumerate(a for a in pending if a not in queued):
                self.bouncer.enqueue(READ_JOB, {"article_id": aid}, delay=READ_SPACING * i if i else None)
        return sorted(wanted)

    def read_article(self, payload: dict[str, Any]) -> dict[str, Any]:
        """A job: read one of the most posted articles, if it's still waiting to be."""
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM articles WHERE id=?", (payload["article_id"],)).fetchone()
        if row is None or row["status"] != "pending" or (row["next_attempt_at"] or "") > utcnow():
            return {"skipped": "read already, or not due"}
        self.bouncer.articles.fetch_one(row)
        return {"article_id": row["id"]}


def archive_skips(bluesky_counted: bool, mastodon_counted: bool) -> tuple[str, ...]:
    """The archive's own posts that a stream counts already: Bluesky's (all of
    it is in Jetstream) and, with your Mastodon server's public timeline or
    FediBuzz's firehose, hashtags' (they come from there, or from FediBuzz's relays)."""
    return tuple(d for d, on in ((BSKY_DOMAIN, bluesky_counted), (TAG_DOMAIN, mastodon_counted)) if on)
