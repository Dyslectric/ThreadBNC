"""People, feeds and channels you've hidden.

Hiding someone (a post's author, from its ⋯ in a feed) leaves their posts out
of every feed, community page and unread count, and nothing new of theirs is
captured from then on: not by checks, pushes, relays or streams. A post of
theirs you open or keep yourself is still saved, and kept posts stay on the
Kept page. For a feed or a YouTube channel, whose posts are all by it, hiding
hides the feed or channel itself (you stay following it; unfollow it to stop
checking it). Unhiding brings back what's still in the archive; posts not
captured meanwhile aren't fetched again.

Kept by key: an author's canonical id (actors.canonical_ap_id) or a
community's (communities.canonical_ap_id)."""

from __future__ import annotations

from typing import Any

from .db import Conn

KINDS = ("author", "community")


def keys(conn: Conn) -> set[str]:
    return {r[0] for r in conn.execute("SELECT key FROM hidden_sources")}


def listed(conn: Conn) -> list[dict[str, Any]]:
    """Everything hidden, newest first, with the community's id for a feed or channel."""
    return [dict(r) for r in conn.execute(
        "SELECT h.*, c.id AS community_id FROM hidden_sources h "
        "LEFT JOIN communities c ON h.kind='community' AND c.canonical_ap_id=h.key ORDER BY h.hidden_at DESC")]


def hide(conn: Conn, key: str, kind: str, label: str | None, now: str) -> None:
    conn.execute("INSERT INTO hidden_sources(key, kind, label, hidden_at) VALUES (?,?,?,?) "
                 "ON CONFLICT(key) DO UPDATE SET label=excluded.label", (key, kind, (label or "")[:200] or None, now))


def unhide(conn: Conn, key: str) -> None:
    conn.execute("DELETE FROM hidden_sources WHERE key=?", (key,))


def thread_sql(conn: Conn, prefix: str = "") -> str:
    """SQL that's true for a thread (archived_threads, its columns prefixed
    with `prefix`) that isn't by anyone hidden or in a hidden feed."""
    if conn.execute("SELECT 1 FROM hidden_sources LIMIT 1").fetchone() is None:
        return "1=1"
    return ("NOT EXISTS (SELECT 1 FROM hidden_sources h WHERE h.key IN ("
            f"(SELECT ha.canonical_ap_id FROM objects ho JOIN actors ha ON ha.id=ho.author_id "
            f"WHERE ho.id={prefix}root_object_id), "
            f"(SELECT hc.canonical_ap_id FROM communities hc WHERE hc.id={prefix}community_id)))")


def hides(conn: Conn, author: str | None, community: str | None) -> bool:
    """Whether a post by `author` in `community` (canonical ids) isn't wanted."""
    wanted = [k for k in (author, community) if k]
    if not wanted:
        return False
    return conn.execute(f"SELECT 1 FROM hidden_sources WHERE key IN ({','.join('?' * len(wanted))}) LIMIT 1",
                        wanted).fetchone() is not None
