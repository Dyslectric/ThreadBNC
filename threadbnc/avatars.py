"""Pictures of who posted what, for the timeline view and Trending's posts.

Where a post comes from usually says who wrote it with the address of their
picture: Mastodon's API, Bluesky and Lemmy do (NActor.avatar, kept as
actors.avatar_url, with avatar_checked_at saying when it was last known). A
post a hashtag relay or a Lemmy server pushed names only its author, so their
picture is looked up on their profile, as any server showing their posts
would: at most once a month, and only once a post of theirs is on screen (JOB,
run by the bouncer). The picture itself is downloaded (media.py) only then
too, like Trending's pictures, and kept while the author is
(actors.avatar_media_id, which media.collect_orphans leaves alone).

A Trending post's author isn't kept: their picture's address is in its view
("avatar"), and the downloaded picture is kept with its pictures
(stream_post_media, at position AVATAR_POSITION).
"""

from __future__ import annotations

import json
import zlib
from datetime import timedelta
from typing import Any
from urllib.parse import urlparse

from . import media as media_mod
from .adapters import RemoteNotFound, host_of, is_bluesky, is_reddit_host, is_rss
from .db import Conn, parse_ts, utcnow

JOB = "avatar"
RECHECK = timedelta(days=30)  # how long a profile looked up (or said to have no picture) is believed
AVATAR_POSITION = -1  # a Trending post's author's picture, among its pictures (stream_post_media)
ASKED = 60  # most pictures asked about at once


def hue(who: str | None) -> int:
    """One of eight colours for someone's initial while there's no picture of theirs (style.css .av-N)."""
    return zlib.crc32((who or "").encode()) % 8


def lookable(ap_id: str | None) -> bool:
    """Whether someone's profile can be read with ActivityPub, for their picture."""
    parsed = urlparse(ap_id or "")
    return (parsed.scheme == "https" and not is_bluesky(ap_id) and not is_rss(ap_id)
            and not is_reddit_host(parsed.hostname or "") and host_of(ap_id or "") != "www.youtube.com")


def stale(checked_at: str | None, now: str) -> bool:
    checked, moment = parse_ts(checked_at), parse_ts(now)
    return checked is None or moment is None or moment - checked >= RECHECK


def waiting(item: dict[str, Any], now: str, can_look: bool) -> bool:
    """Whether a feed post's author may still get a picture (feed.load_feed's
    avatar columns): one known and not downloaded yet, or none known and
    theirs can be looked up."""
    if item.get("avatar_url"):
        return item.get("avatar_status") in (None, "pending")
    return can_look and lookable(item.get("author_ap")) and stale(item.get("avatar_checked_at"), now)


def image_url(actor: dict[str, Any]) -> str | None:
    """An ActivityPub actor's picture: its icon (an Image, a Link or a list of them)."""
    icon = actor.get("icon")
    for item in icon if isinstance(icon, list) else [icon]:
        url = item.get("url") if isinstance(item, dict) else item
        if isinstance(url, list):
            url = url[0] if url else None
        if isinstance(url, dict):
            url = url.get("href")
        if isinstance(url, str) and url.startswith("https://"):
            return url
    return None


def job_payload(actor_id: int) -> dict[str, Any]:
    return {"actor_id": actor_id}


def looking(conn: Conn, actor_id: int) -> bool:
    """Whether someone's picture is being looked up (JOB queued or running)."""
    return conn.execute("SELECT 1 FROM jobs WHERE kind=? AND status IN ('queued', 'running') AND payload_json=?",
                        (JOB, json.dumps(job_payload(actor_id)))).fetchone() is not None


def want(conn: Conn, keys: list[str], now: str, can_look: bool) -> list[int]:
    """Pictures on screen (app.js): "actor:<id>" for a feed post's author,
    "trend:<source> <ref>" for a Trending post's. Those whose address is
    known are registered to be downloaded; returns the actors whose profile
    is to be looked up (JOB)."""
    lookups: list[int] = []
    for key in keys[:ASKED]:
        kind, _, value = key.partition(":")
        if kind == "actor" and value.isdigit():
            r = conn.execute("SELECT id, canonical_ap_id, avatar_url, avatar_media_id, avatar_checked_at "
                             "FROM actors WHERE id=?", (int(value),)).fetchone()
            if r is None:
                continue
            if r["avatar_url"]:
                if not r["avatar_media_id"]:
                    conn.execute("UPDATE actors SET avatar_media_id=? WHERE id=?",
                                 (media_mod.media_id(conn, r["avatar_url"], now), r["id"]))
            elif can_look and lookable(r["canonical_ap_id"]) and stale(r["avatar_checked_at"], now):
                conn.execute("UPDATE actors SET avatar_checked_at=? WHERE id=?", (now, r["id"]))
                lookups.append(r["id"])
        elif kind == "trend":
            source, _, ref = value.partition(" ")
            row = conn.execute("SELECT view_json FROM stream_posts WHERE source=? AND ref=?", (source, ref)).fetchone()
            try:
                url = json.loads(row["view_json"]).get("avatar") if row and row["view_json"] else None
            except ValueError:
                url = None
            if not (isinstance(url, str) and url.startswith("https://")):
                continue
            mid = media_mod.media_id(conn, url, now)
            conn.execute("DELETE FROM stream_post_media WHERE source=? AND ref=? AND position=? AND media_id<>?",
                         (source, ref, AVATAR_POSITION, mid))  # an older picture of theirs
            conn.execute("INSERT INTO stream_post_media(source, ref, media_id, position) VALUES (?,?,?,?) "
                         "ON CONFLICT(source, ref, media_id) DO NOTHING", (source, ref, mid, AVATAR_POSITION))
    return lookups


def ready(conn: Conn, keys: list[str]) -> tuple[dict[str, int], list[str]]:
    """The pictures downloaded (media ids by key), and the keys still to come."""
    got: dict[str, int] = {}
    pending: list[str] = []
    for key in keys[:ASKED]:
        kind, _, value = key.partition(":")
        if kind == "actor" and value.isdigit():
            r = conn.execute("SELECT a.avatar_url, m.id AS mid, m.status FROM actors a "
                             "LEFT JOIN media m ON m.id=a.avatar_media_id WHERE a.id=?", (int(value),)).fetchone()
            if r is None:
                continue
            if r["status"] == "ok":
                got[key] = r["mid"]
            elif (r["avatar_url"] and r["status"] in (None, "pending")) or looking(conn, int(value)):
                pending.append(key)  # downloading, or being looked up
        elif kind == "trend":
            source, _, ref = value.partition(" ")
            r = conn.execute("SELECT m.id, m.status FROM stream_post_media s JOIN media m ON m.id=s.media_id "
                             "WHERE s.source=? AND s.ref=? AND s.position=?",
                             (source, ref, AVATAR_POSITION)).fetchone()
            if r is not None and r["status"] == "ok":
                got[key] = r["id"]
            elif r is not None and r["status"] == "pending":
                pending.append(key)
    return got, pending


def trend_avatars(conn: Conn, posts: list[tuple[str, str]]) -> dict[tuple[str, str], int]:
    """Trending posts' authors' pictures, downloaded: media ids by (source, ref)."""
    out: dict[tuple[str, str], int] = {}
    for start in range(0, len(posts), 200):
        chunk = posts[start:start + 200]
        where = " OR ".join("(s.source=? AND s.ref=?)" for _ in chunk)
        for r in conn.execute(
                f"SELECT s.source, s.ref, m.id FROM stream_post_media s JOIN media m ON m.id=s.media_id "
                f"WHERE s.position=? AND m.status='ok' AND ({where})",
                [AVATAR_POSITION, *(v for pair in chunk for v in pair)]).fetchall():
            out[(r["source"], r["ref"])] = r["id"]
    return out


def look_up(bouncer: Any, payload: dict[str, Any]) -> dict[str, Any]:
    """JOB: someone's picture, from their ActivityPub profile, registered to be
    downloaded (a post of theirs is on screen)."""
    now = utcnow()
    with bouncer.db.connect() as conn:
        row = conn.execute("SELECT canonical_ap_id FROM actors WHERE id=?", (payload["actor_id"],)).fetchone()
    if row is None or bouncer.actor is None or not lookable(row["canonical_ap_id"]):
        return {"avatar": None}
    ap_id = row["canonical_ap_id"]
    try:
        doc = bouncer.actor.fetch(ap_id)
    except RemoteNotFound:
        doc = {}
    same = isinstance(doc.get("id"), str) and host_of(doc["id"]) == host_of(ap_id)
    url = image_url(doc) if same else None
    with bouncer.db.transaction() as conn:
        conn.execute("UPDATE actors SET avatar_url=?, avatar_checked_at=?, avatar_media_id=? WHERE id=?",
                     (url, now, media_mod.media_id(conn, url, now) if url else None, payload["actor_id"]))
    bouncer.wake.set()
    return {"avatar": url}


def remember(conn: Conn, actor_id: int, url: str, now: str) -> None:
    """What a post's source said of its author's picture (store.upsert_actor;
    "" for none): a new one is downloaded when a post of theirs is next on screen."""
    url_or_none = url if url.startswith("https://") else None
    conn.execute("UPDATE actors SET avatar_media_id=CASE WHEN COALESCE(avatar_url, '')=? THEN avatar_media_id "
                 "ELSE NULL END, avatar_url=?, avatar_checked_at=? WHERE id=?",
                 (url_or_none or "", url_or_none, now, actor_id))
