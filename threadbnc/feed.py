"""Feed queries: posts from followed communities, built from the local archive."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .db import Conn, fmt_ts

SORTS = {  # NULLS LAST: Postgres otherwise puts NULLs first in DESC order
    "new": "created_at DESC NULLS LAST, id DESC",
    "active": "last_activity DESC NULLS LAST, id DESC",
    "top": "score DESC NULLS LAST, created_at DESC NULLS LAST, id DESC",
    "comments": "n_comments DESC, created_at DESC NULLS LAST, id DESC",
}
WINDOWS = {"day": timedelta(days=1), "week": timedelta(days=7), "month": timedelta(days=30), "all": None}

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
  SELECT t.id, t.retention, t.retained_at, t.promoted_at, t.expires_at, t.last_viewed_at, t.community_id,
         o.id AS oid, o.created_at, o.score, o.cur_deleted, o.cur_removed, o.cur_locked, o.cur_missing,
         o.revision_count, o.thumbnail_url, o.upvotes, o.downvotes, r.title, r.body, r.url, a.username, a.instance AS a_instance,
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
ORDER BY {order}
LIMIT ? OFFSET ?
"""


def load_feed(conn: Conn, *, community_id: int | None = None, sort: str = "new",
              window: str = "all", unread: bool = False, kept_only: bool = False, page: int = 1,
              per_page: int = 25) -> FeedPage:
    args: list[Any] = []
    if community_id is not None:
        scope = "AND t.community_id=?"
        args.append(community_id)
    else:
        scope = "AND t.community_id IN (SELECT community_id FROM community_follows WHERE active=1)"
    if kept_only:
        scope += " AND t.retention='manual'"
    filters = ""
    delta = WINDOWS.get(window)
    if delta is not None:
        filters += " AND created_at >= ?"
        args.append(fmt_ts(datetime.now(timezone.utc) - delta))
    if unread:
        filters += " AND (last_viewed_at IS NULL OR n_new > 0)"
    order = SORTS.get(sort, SORTS["new"])
    page = max(1, page)
    rows = conn.execute(_BASE.format(scope=scope, filters=filters, order=order),
                        [*args, per_page + 1, (page - 1) * per_page]).fetchall()
    items = [dict(r) for r in rows[:per_page]]
    thumbs = _thumbnails(conn, [(i["oid"], i["url"], i["thumbnail_url"]) for i in items])
    for i in items:
        i["excerpt"] = excerpt(i["body"])
        i["thumb"] = thumbs.get(i["oid"])
        i["unread"] = i["last_viewed_at"] is None
    return FeedPage(items, page, len(rows) > per_page)


def _thumbnails(conn: Conn,
                roots: list[tuple[int, str | None, str | None]]) -> dict[int, dict[str, Any]]:
    """Archived image/video for each post: its link if that is media, else the
    server's preview image (article links), else the first embedded image."""
    if not roots:
        return {}
    ids = [oid for oid, _, _ in roots]
    marks = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT r.object_id, m.id, m.url, m.content_type FROM media_refs r JOIN media m ON m.id=r.media_id "
        f"WHERE r.object_id IN ({marks}) AND m.status='ok' AND (m.content_type LIKE 'image/%' "
        f"OR m.content_type LIKE 'video/%') AND m.content_type != 'image/svg+xml' ORDER BY m.id", ids,
    ).fetchall()
    links = {oid: url for oid, url, _ in roots}
    previews = {oid: thumb for oid, _, thumb in roots}
    out: dict[int, dict[str, Any]] = {}
    for r in rows:
        oid = r["object_id"]
        is_link = r["url"] == links.get(oid)
        rank = 0 if is_link else 1 if r["url"] == previews.get(oid) else 2
        if oid not in out or rank < out[oid]["rank"]:
            out[oid] = {"id": r["id"], "video": r["content_type"].startswith("video/"),
                        "is_link": is_link, "rank": rank}
    return out


def followed_communities(conn: Conn) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT c.id, c.name, c.title, c.canonical_ap_id, f.poll_interval_minutes, f.retention_days,
                  f.last_polled_at, f.last_error, f.source_domain,
                  (SELECT COUNT(*) FROM archived_threads t WHERE t.community_id=c.id AND t.trashed_at IS NULL
                       AND t.last_viewed_at IS NULL) AS unread,
                  (SELECT COUNT(*) FROM archived_threads t WHERE t.community_id=c.id AND t.trashed_at IS NULL)
                       AS total
           FROM community_follows f JOIN communities c ON c.id=f.community_id
           WHERE f.active=1 ORDER BY c.name, c.canonical_ap_id""").fetchall()
    return [dict(r) for r in rows]


def mark_read(conn: Conn, now: str, community_id: int | None = None) -> None:
    scope = ("community_id=?", [community_id]) if community_id is not None else (
        "community_id IN (SELECT community_id FROM community_follows WHERE active=1)", [])
    conn.execute(
        f"UPDATE archived_threads SET prev_viewed_at=last_viewed_at, last_viewed_at=? "
        f"WHERE trashed_at IS NULL AND {scope[0]}", [now, *scope[1]])
