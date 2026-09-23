"""Feed queries: posts from followed communities, built from the local archive."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from . import articles, dupes
from .db import Conn, fmt_ts
from .render import AUDIO_EXTENSIONS, VIDEO_EXTENSIONS, looks_like_audio

# Posts of the same link or text are one feed entry; sorts use the group's totals.
SORTS = {  # NULLS LAST: Postgres otherwise puts NULLs first in DESC order
    "new": "created_at DESC NULLS LAST, id DESC",
    "active": "g_activity DESC NULLS LAST, id DESC",
    "top": "g_score DESC NULLS LAST, created_at DESC NULLS LAST, id DESC",
    "comments": "g_comments DESC, created_at DESC NULLS LAST, id DESC",
}
WINDOWS = {"day": timedelta(days=1), "week": timedelta(days=7), "month": timedelta(days=30), "all": None}
VIEWS = ("list", "pictures", "tiles")  # posts; pictures in one column; a grid of pictures
# "Auto" shows tiles when at least this share of recent posts have an image or video.
TILES_WHEN = 0.6
MEDIA_SAMPLE = 40  # recent posts looked at to decide
TILES_MIN_POSTS = 4  # too few posts to tell: stay a list
_VISUAL = ("m.status='ok' AND (m.content_type LIKE 'image/%' OR m.content_type LIKE 'video/%') "
           "AND m.content_type != 'image/svg+xml'")



def _ext_sql(extensions: tuple[str, ...]) -> str:
    """mm.url ends in one of these, before any query string (looks_like_audio in SQL)."""
    return "(" + " OR ".join(f"LOWER(mm.url) LIKE '%{e}' OR LOWER(mm.url) LIKE '%{e}?%'" for e in extensions) + ")"


# Posts that are a video or a piece of audio (the Kept page's tabs): a video
# archived with the post (its link or embedded), a YouTube video saved when
# kept, or a video link not downloaded yet; audio as the feed's player finds it
# (audio()). o and r are the post and its current revision in _BASE.
_HAS_MEDIA = "EXISTS (SELECT 1 FROM media_refs mr JOIN media mm ON mm.id=mr.media_id WHERE mr.object_id=o.id AND "
MEDIA_KINDS = {
    "video": _HAS_MEDIA + ("mr.from_article=0 AND (mm.content_type LIKE 'video/%' OR mm.kept_only=1 OR "
                           f"(mm.content_type IS NULL AND {_ext_sql(VIDEO_EXTENSIONS)})))"),
    "audio": _HAS_MEDIA + ("mm.url=r.url AND mm.status != 'skipped' AND (mm.content_type LIKE 'audio/%' OR "
                           f"(mm.status != 'ok' AND {_ext_sql(AUDIO_EXTENSIONS)})))"),
}

_IMG = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_MARKS = re.compile(r"(^|\s)(#{1,6}|>+|[-*+]|\d+\.)\s+|[*_`~|]|:::\s*spoiler")


def excerpt(body: str | None, limit: int = 260) -> str:
    if not body:
        return ""
    text = _LINK.sub(r"\1", _IMG.sub("", body))
    text = " ".join(_MARKS.sub(" ", text).split())
    return text if len(text) <= limit else text[: limit - 1].rsplit(" ", 1)[0] + "…"


@dataclass
class FeedPage:
    items: list[dict[str, Any]]
    page: int
    has_more: bool


_BASE = """
SELECT * FROM (
  SELECT feed.*,
         ROW_NUMBER() OVER (PARTITION BY dkey ORDER BY created_at, id) AS g_rank,
         SUM(score) OVER (PARTITION BY dkey) AS g_score,
         SUM(n_comments) OVER (PARTITION BY dkey) AS g_comments,
         SUM(n_new) OVER (PARTITION BY dkey) AS g_new,
         SUM(CASE WHEN last_viewed_at IS NULL THEN 1 ELSE 0 END) OVER (PARTITION BY dkey) AS g_unread,
         MAX(last_activity) OVER (PARTITION BY dkey) AS g_activity
  FROM (
    SELECT t.id, t.retention, t.retained_at, t.promoted_at, t.expires_at, t.last_viewed_at, t.community_id,
           t.source_domain, o.id AS oid, o.created_at, o.score, o.cur_deleted, o.cur_removed, o.cur_locked,
           o.cur_missing, o.revision_count, o.thumbnail_url, o.upvotes, o.downvotes, o.dupe_key,
           COALESCE(o.dupe_key, 'thread:' || t.id) AS dkey,
           r.title, r.body, r.url, r.metadata_json AS rmeta, a.username, a.instance AS a_instance,
           c.name AS cname, c.canonical_ap_id AS c_ap,
           (SELECT COUNT(*) FROM objects x WHERE x.thread_id=t.id AND x.object_type='comment') AS n_comments,
           (SELECT COUNT(*) FROM objects x WHERE x.thread_id=t.id AND x.discovered_late=1
               AND t.last_viewed_at IS NOT NULL AND x.first_seen_at > t.last_viewed_at) AS n_new,
           (SELECT MAX(COALESCE(x.created_at, x.first_seen_at)) FROM objects x WHERE x.thread_id=t.id)
               AS last_activity
    FROM archived_threads t
    JOIN objects o ON o.id=t.root_object_id
    JOIN revisions r ON r.object_id=o.id AND r.seq=o.revision_count
    JOIN communities c ON c.id=t.community_id
    LEFT JOIN actors a ON a.id=o.author_id
    WHERE t.trashed_at IS NULL {scope}
  ) AS feed WHERE 1=1 {filters}
) AS grouped WHERE g_rank=1 {group_filters}
ORDER BY {order}
LIMIT ? OFFSET ?
"""


# The unread filter: posts not opened yet, read posts with new comments, or either.
UNREAD = {"posts": "g_unread > 0", "comments": "g_new > 0", "any": "(g_unread > 0 OR g_new > 0)"}


def unread_mode(value: Any) -> str:
    """The unread filter named by a URL value; "1" (older links) means either."""
    if value in UNREAD:
        return value
    return "any" if value is True or value == "1" else ""


# --- how a feed is shown by default -------------------------------------------------

@dataclass
class FeedSettings:
    """A feed's sort, time range and unread filter: what it shows when the
    address doesn't say."""

    sort: str = "new"
    window: str = "all"
    unread: str = ""

    @classmethod
    def clean(cls, sort: Any, window: Any, unread: Any) -> FeedSettings:
        return cls(sort if sort in SORTS else "new", window if window in WINDOWS else "all", unread_mode(unread))

    def with_url(self, sort: str | None, window: str | None, unread: str | None) -> FeedSettings:
        """These settings, with whatever the address chose instead."""
        return FeedSettings.clean(self.sort if sort is None else sort, self.window if window is None else window,
                                  self.unread if unread is None else unread)


def load_defaults(conn: Conn) -> FeedSettings:
    """The main feed's defaults, which community pages use too."""
    row = conn.execute("SELECT value FROM app_settings WHERE key='feed_defaults'").fetchone()
    saved = json.loads(row[0]) if row and row[0] else {}
    return FeedSettings.clean(saved.get("sort"), saved.get("window"), saved.get("unread"))


def save_defaults(conn: Conn, settings: FeedSettings) -> None:
    conn.execute("INSERT INTO app_settings(key, value) VALUES ('feed_defaults', ?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (json.dumps({"sort": settings.sort, "window": settings.window, "unread": settings.unread}),))


# --- your own feeds ------------------------------------------------------------------

def custom_feeds(conn: Conn) -> list[dict[str, Any]]:
    """Every custom feed, with its communities and how many unread posts it has."""
    feeds = [dict(r) for r in conn.execute("SELECT * FROM custom_feeds ORDER BY name, id")]
    members: dict[int, list[int]] = {}
    for r in conn.execute("SELECT feed_id, community_id FROM custom_feed_communities"):
        members.setdefault(r[0], []).append(r[1])
    unread = {r[0]: r[1] for r in conn.execute(
        "SELECT community_id, COUNT(*) FROM archived_threads WHERE trashed_at IS NULL AND last_viewed_at IS NULL "
        "GROUP BY community_id")}
    for f in feeds:
        f["community_ids"] = sorted(members.get(f["id"], []))
        f["unread_count"] = sum(unread.get(c, 0) for c in f["community_ids"])
    return feeds


def custom_feed(conn: Conn, feed_id: int) -> dict[str, Any] | None:
    return next((f for f in custom_feeds(conn) if f["id"] == feed_id), None)


def feed_settings(feed: dict[str, Any], defaults: FeedSettings) -> FeedSettings:
    """A custom feed's own settings, falling back to the main feed's."""
    return FeedSettings.clean(feed["sort"] or defaults.sort, feed["time_window"] or defaults.window,
                              defaults.unread if feed["unread"] is None else feed["unread"])


def save_custom_feed(conn: Conn, feed_id: int | None, name: str, community_ids: list[int], settings: FeedSettings,
                     view: str | None, now: str) -> int:
    view = view if view in VIEWS else None
    if feed_id is None:
        feed_id = conn.execute("INSERT INTO custom_feeds(name, sort, time_window, unread, view_mode, created_at) "
                               "VALUES (?,?,?,?,?,?)", (name, settings.sort, settings.window, settings.unread, view,
                                                        now)).lastrowid
    else:
        conn.execute("UPDATE custom_feeds SET name=?, sort=?, time_window=?, unread=?, view_mode=? WHERE id=?",
                     (name, settings.sort, settings.window, settings.unread, view, feed_id))
        conn.execute("DELETE FROM custom_feed_communities WHERE feed_id=?", (feed_id,))
    for cid in sorted(set(community_ids)):
        conn.execute("INSERT INTO custom_feed_communities(feed_id, community_id) VALUES (?,?)", (feed_id, cid))
    return feed_id  # type: ignore[return-value]


def save_custom_feed_settings(conn: Conn, feed_id: int, settings: FeedSettings) -> None:
    conn.execute("UPDATE custom_feeds SET sort=?, time_window=?, unread=? WHERE id=?",
                 (settings.sort, settings.window, settings.unread, feed_id))


def delete_custom_feed(conn: Conn, feed_id: int) -> None:
    conn.execute("DELETE FROM custom_feed_communities WHERE feed_id=?", (feed_id,))
    conn.execute("DELETE FROM custom_feeds WHERE id=?", (feed_id,))


def _scope(community_id: int | None, community_ids: list[int] | None, column: str) -> tuple[str, list[Any]]:
    """Which communities a feed covers: one, a custom feed's, or every followed one."""
    if community_id is not None:
        return f"{column}=?", [community_id]
    if community_ids is not None:
        if not community_ids:
            return "1=0", []
        return f"{column} IN ({','.join('?' * len(community_ids))})", list(community_ids)
    return f"{column} IN (SELECT community_id FROM community_follows WHERE active=1)", []


def load_feed(conn: Conn, *, community_id: int | None = None, community_ids: list[int] | None = None,
              thread_ids: list[int] | None = None, sort: str = "new", window: str = "all",
              unread: str | bool = "", kept_only: bool = False, media: str = "", page: int = 1,
              per_page: int = 25) -> FeedPage:
    """A page of posts. `thread_ids`: just those (the caller orders them);
    kept_only with no communities given: kept posts from anywhere, followed or not.
    `media`: only posts that are a video or audio (MEDIA_KINDS)."""
    if thread_ids is not None:
        where, args = (f"t.id IN ({','.join('?' * len(thread_ids))})", list(thread_ids)) if thread_ids \
            else ("1=0", [])
    elif kept_only and community_id is None and community_ids is None:
        where, args = "1=1", []
    else:
        where, args = _scope(community_id, community_ids, "t.community_id")
    scope = f"AND {where}"
    if kept_only:
        scope += " AND t.retention='manual'"
    if media in MEDIA_KINDS:
        scope += " AND " + MEDIA_KINDS[media]
    filters = ""
    delta = WINDOWS.get(window)
    if delta is not None:
        filters += " AND created_at >= ?"
        args.append(fmt_ts(datetime.now(timezone.utc) - delta))
    mode = unread_mode(unread)
    group_filters = f" AND {UNREAD[mode]}" if mode else ""
    order = SORTS.get(sort, SORTS["new"])
    page = max(1, page)
    rows = conn.execute(_BASE.format(scope=scope, filters=filters, group_filters=group_filters, order=order),
                        [*args, per_page + 1, (page - 1) * per_page]).fetchall()
    items = [dict(r) for r in rows[:per_page]]
    thumbs = thumbnails(conn, [(i["oid"], i["url"], i["thumbnail_url"]) for i in items])
    copies = dupes.load_copies(conn, [i["dupe_key"] for i in items])
    readable = articles.readable(conn, [i["oid"] for i in items])
    waiting = articles.waiting(conn, [(i["oid"], i["url"]) for i in items])
    sounds = audio(conn, [(i["oid"], i["url"]) for i in items])
    for i in items:
        i["excerpt"] = excerpt(i["body"])
        i["thumb"] = thumbs.get(i["oid"])
        i["article"] = i["oid"] in readable
        i["article_waiting"] = i["oid"] in waiting
        i["audio"] = sounds.get(i["oid"])
        meta = json.loads(i.pop("rmeta") or "{}")
        i["nsfw"], i["spoiler"] = bool(meta.get("nsfw")), bool(meta.get("spoiler"))
        attach_group(i, copies.get(i["dupe_key"]) or [])
    return FeedPage(items, page, len(rows) > per_page)


def count_kept_media(conn: Conn, media: str) -> int:
    """How many kept posts are a video, or audio (MEDIA_KINDS)."""
    return conn.execute(
        "SELECT COUNT(*) FROM archived_threads t JOIN objects o ON o.id=t.root_object_id "
        "JOIN revisions r ON r.object_id=o.id AND r.seq=o.revision_count "
        f"WHERE t.retention='manual' AND t.trashed_at IS NULL AND {MEDIA_KINDS[media]}").fetchone()[0]


def attach_group(item: dict[str, Any], copies: list[dict[str, Any]]) -> None:
    """Fold the other stored copies of a post (anywhere in the archive, not only
    this feed's scope) into its feed entry: who posted it where, combined
    votes with a per-server breakdown, combined comment counts."""
    if not any(c["id"] == item["id"] for c in copies):
        copies = [dict(item, a_ap=None), *copies]
    item["copies"] = copies
    item["group"] = len(copies) > 1
    item["group_ids"] = [c["id"] for c in copies]
    item.update(dupes.sum_votes(copies))
    item["n_comments"] = sum(c["n_comments"] for c in copies)
    item["n_new"] = sum(c["n_new"] for c in copies)
    item["unread"] = any(c["last_viewed_at"] is None for c in copies)
    item["all_kept"] = all(c["retention"] == "manual" for c in copies)
    item["breakdown"] = [dupes.breakdown_row(c["cname"], c["c_ap"], c["source_domain"], c) for c in copies]


def thumbnails(conn: Conn,
               roots: list[tuple[int, str | None, str | None]]) -> dict[int, dict[str, Any]]:
    """Archived image/video for each post: its link if that is media, else the
    server's preview image (article links), else the first embedded image.
    "pics" is every picture to page through: the link, then embedded and gallery
    images in the order they were found, then the linked article's; the preview
    only when there's nothing else, as it is usually a smaller copy of one of them."""
    if not roots:
        return {}
    ids = [oid for oid, _, _ in roots]
    marks = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT r.object_id, r.from_article, m.id, m.url, m.content_type FROM media_refs r "
        f"JOIN media m ON m.id=r.media_id WHERE r.object_id IN ({marks}) AND {_VISUAL} ORDER BY m.id", ids,
    ).fetchall()
    links = {oid: url for oid, url, _ in roots}
    previews = {oid: thumb for oid, _, thumb in roots}
    out: dict[int, dict[str, Any]] = {}
    found: dict[int, list[dict[str, Any]]] = {}
    for r in rows:
        oid = r["object_id"]
        is_link = r["url"] == links.get(oid)
        rank = 0 if is_link else 1 if r["url"] == previews.get(oid) else 3 if r["from_article"] else 2
        pic = {"id": r["id"], "video": r["content_type"].startswith("video/"), "is_link": is_link, "rank": rank}
        found.setdefault(oid, []).append(pic)
        if oid not in out or rank < out[oid]["rank"]:
            out[oid] = dict(pic)
    for oid, thumb in out.items():
        pics = sorted((p for p in found[oid] if p["rank"] != 1), key=lambda p: p["rank"])
        thumb["pics"] = pics or [p for p in found[oid] if p["rank"] == 1]
    return out


def audio(conn: Conn, links: list[tuple[int, str | None]]) -> dict[int, dict[str, Any]]:
    """The audio file each of these (object id, current link) posts links to,
    for the player bar in its feed entry: its media row's id, status
    (pending until it's downloaded, which happens once the post is scrolled
    to: Bouncer.fetch_audio) and error. Links that turned out not to be audio,
    or that settings leave out, have none."""
    links = [(oid, url) for oid, url in links if url]
    if not links:
        return {}
    pairs = " OR ".join("(r.object_id=? AND m.url=?)" for _ in links)
    rows = conn.execute(
        f"SELECT r.object_id, m.id, m.url, m.status, m.content_type, m.error FROM media_refs r "
        f"JOIN media m ON m.id=r.media_id WHERE ({pairs}) AND m.status != 'skipped'",
        [v for pair in links for v in pair]).fetchall()
    return {r["object_id"]: {"id": r["id"], "status": r["status"], "error": r["error"]} for r in rows
            if (r["content_type"] or "").startswith("audio/")
            or (r["status"] != "ok" and looks_like_audio(r["url"]))}


def followed_communities(conn: Conn) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT c.id, c.name, c.title, c.canonical_ap_id, f.poll_interval_minutes, f.retention_days,
                  f.last_polled_at, f.last_error, f.source_domain, f.push_state, f.push_error, f.last_push_at,
                  f.polling,
                  (SELECT COUNT(*) FROM archived_threads t WHERE t.community_id=c.id AND t.trashed_at IS NULL
                       AND t.last_viewed_at IS NULL) AS unread,
                  (SELECT COUNT(*) FROM archived_threads t WHERE t.community_id=c.id AND t.trashed_at IS NULL)
                       AS total
           FROM community_follows f JOIN communities c ON c.id=f.community_id
           WHERE f.active=1 ORDER BY c.name, c.canonical_ap_id""").fetchall()
    return [dict(r) for r in rows]


def mark_read(conn: Conn, now: str, community_id: int | None = None,
              community_ids: list[int] | None = None) -> int:
    """Mark a feed read; returns how many posts were unread. `now` becomes
    their last_viewed_at, which is what unmark_read(now) undoes."""
    scope = _scope(community_id, community_ids, "community_id")
    unread = conn.execute(
        f"SELECT COUNT(*) FROM archived_threads WHERE trashed_at IS NULL AND last_viewed_at IS NULL "
        f"AND {scope[0]}", scope[1]).fetchone()[0]
    conn.execute(
        f"UPDATE archived_threads SET prev_viewed_at=last_viewed_at, last_viewed_at=? "
        f"WHERE trashed_at IS NULL AND {scope[0]}", [now, *scope[1]])
    return unread


def unmark_read(conn: Conn, stamp: str) -> None:
    """Undo mark_read(stamp)."""
    conn.execute("UPDATE archived_threads SET last_viewed_at=prev_viewed_at WHERE last_viewed_at=?", (stamp,))


def mark_seen(conn: Conn, now: str, thread_ids: list[int]) -> int:
    """Mark these unread posts read (scrolled past in the feed). Posts already
    read keep their last visit, so their new comments stay highlighted."""
    if not thread_ids:
        return 0
    marks = ",".join("?" * len(thread_ids))
    n = conn.execute(f"SELECT COUNT(*) FROM archived_threads WHERE id IN ({marks}) AND last_viewed_at IS NULL",
                     thread_ids).fetchone()[0]
    conn.execute(f"UPDATE archived_threads SET last_viewed_at=? WHERE id IN ({marks}) AND last_viewed_at IS NULL",
                 [now, *thread_ids])
    return n


def set_read(conn: Conn, now: str, thread_ids: list[int], read: bool = True) -> None:
    """Mark these posts read, as if opened, or unread again (as if never
    opened, so they count as new and none of their comments are highlighted)."""
    if not thread_ids:
        return
    marks = ",".join("?" * len(thread_ids))
    if read:
        conn.execute(f"UPDATE archived_threads SET last_viewed_at=? WHERE id IN ({marks}) AND last_viewed_at IS NULL",
                     [now, *thread_ids])
    else:
        conn.execute(f"UPDATE archived_threads SET prev_viewed_at=last_viewed_at, last_viewed_at=NULL "
                     f"WHERE id IN ({marks}) AND last_viewed_at IS NOT NULL", thread_ids)


def media_share(conn: Conn, community_id: int) -> tuple[int, int]:
    """(posts with an archived image or video, posts) among a community's
    recent stored posts: what "auto" view uses to pick tiles for image-centric
    communities."""
    row = conn.execute(
        f"""SELECT COUNT(*) AS n, SUM(CASE WHEN EXISTS(
                SELECT 1 FROM media_refs r JOIN media m ON m.id=r.media_id
                WHERE r.object_id=x.root_object_id AND r.from_article=0 AND {_VISUAL}) THEN 1 ELSE 0 END) AS visual
            FROM (SELECT root_object_id FROM archived_threads WHERE community_id=? AND trashed_at IS NULL
                  AND root_object_id IS NOT NULL ORDER BY id DESC LIMIT ?) AS x""",
        (community_id, MEDIA_SAMPLE)).fetchone()
    return row["visual"] or 0, row["n"]


def pick_view(chosen: str | None, visual: int, total: int) -> str:
    """The view to show: the one you chose, else tiles for media-heavy feeds."""
    if chosen in VIEWS:
        return chosen
    return "tiles" if total >= TILES_MIN_POSTS and visual >= TILES_WHEN * total else "list"
