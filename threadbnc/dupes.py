"""Recognising the same post made more than once: the same link or the same
text, posted by several people or in several communities (crossposts).

Each post gets a `dupe_key`: its link, normalised so trivial differences
(tracking parameters, www., http vs https, a trailing slash, youtu.be vs
youtube.com) don't matter; or, for a text post, a hash of its title and body
with Lemmy's "cross-posted from:" line and quoting stripped. Posts that share a
key are shown together. Comments are squashed at display time (see
comment_key): the same person saying the same thing under any of the copies.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit

from .db import Conn

# Query parameters that only say where a click came from.
_TRACKING = {"fbclid", "gclid", "dclid", "msclkid", "mc_cid", "mc_eid", "igshid", "si", "ref", "ref_src",
             "ref_url", "feature", "share", "smid", "cmpid", "mbid", "_hsenc", "_hsmi", "yclid", "twclid"}
_CROSSPOST_LINE = re.compile(r"^\s*cross-?posted\s+(from|to)\b.*$", re.IGNORECASE | re.MULTILINE)
_QUOTE = re.compile(r"^\s*>+ ?", re.MULTILINE)
_SETTING = "dupe_keys_v1"


def normalize_url(url: str | None) -> str | None:
    if not url or not url.strip():
        return None
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        return None
    host = parts.hostname.lower()
    for prefix in ("www.", "m.", "mobile."):
        if host.startswith(prefix) and host.count(".") > 1:
            host = host[len(prefix):]
    path = parts.path or "/"
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if k.lower() not in _TRACKING and not k.lower().startswith("utm_")]
    if host == "youtu.be" and path.strip("/"):
        host, query, path = "youtube.com", [("v", path.strip("/").split("/")[0])], "/watch"
    elif host in ("youtube.com", "music.youtube.com") and path.startswith("/shorts/") and path.split("/")[2]:
        host, query, path = "youtube.com", [("v", path.split("/")[2])], "/watch"
    elif host == "youtube.com" and path == "/watch":
        query = [(k, v) for k, v in query if k == "v"]
    if len(path) > 1:
        path = path.rstrip("/")
    port = f":{parts.port}" if parts.port and parts.port not in (80, 443) else ""
    q = urlencode(sorted(query))
    return f"{host}{port}{path}" + (f"?{q}" if q else "")


def normalize_text(text: str | None) -> str:
    """Text with crosspost boilerplate, quoting, case and spacing removed."""
    if not text:
        return ""
    text = _QUOTE.sub("", _CROSSPOST_LINE.sub("", text))
    return " ".join(text.lower().split())


def post_key(title: str | None, body: str | None, url: str | None) -> str | None:
    link = normalize_url(url)
    if link:
        return "url:" + link
    t, b = normalize_text(title), normalize_text(body)
    if not b:
        return None  # a bare title ("Weekly thread") is too weak to call two posts the same
    return "text:" + hashlib.sha256(f"{t}\n{b}".encode("utf-8")).hexdigest()


def comment_key(author_ap_id: str | None, body: str | None) -> tuple[str, str] | None:
    """Comments that say the same thing, from the same person."""
    b = normalize_text(body)
    if not author_ap_id or not b:
        return None
    return (author_ap_id, b)


def refresh_key(conn: Conn, object_id: int) -> None:
    """Recompute a post's key from its latest revision."""
    row = conn.execute(
        "SELECT o.object_type, r.title, r.body, r.url FROM objects o "
        "JOIN revisions r ON r.object_id=o.id AND r.seq=o.revision_count WHERE o.id=?", (object_id,)).fetchone()
    if row and row["object_type"] == "post":
        conn.execute("UPDATE objects SET dupe_key=? WHERE id=?",
                     (post_key(row["title"], row["body"], row["url"]), object_id))


def backfill(conn: Conn) -> None:
    """Give posts stored before duplicate recognition existed their keys (once)."""
    if conn.execute("SELECT 1 FROM app_settings WHERE key=?", (_SETTING,)).fetchone():
        return
    rows = conn.execute(
        "SELECT o.id, r.title, r.body, r.url FROM objects o "
        "JOIN revisions r ON r.object_id=o.id AND r.seq=o.revision_count WHERE o.object_type='post'").fetchall()
    for r in rows:
        key = post_key(r["title"], r["body"], r["url"])
        if key:
            conn.execute("UPDATE objects SET dupe_key=? WHERE id=?", (key, r["id"]))
    conn.execute("INSERT INTO app_settings(key, value) VALUES (?, '1')", (_SETTING,))


def load_copies(conn: Conn, keys: list[str]) -> dict[str, list[dict[str, Any]]]:
    """Every stored (untrashed) thread whose post has one of these keys, oldest first."""
    keys = [k for k in dict.fromkeys(keys) if k]
    if not keys:
        return {}
    marks = ",".join("?" * len(keys))
    rows = conn.execute(
        f"""SELECT t.id, t.retention, t.last_viewed_at, t.source_domain, t.community_id,
                   o.id AS oid, o.dupe_key, o.canonical_ap_id, o.created_at, o.score, o.upvotes, o.downvotes,
                   o.cur_deleted, o.cur_removed, o.cur_locked, o.cur_missing,
                   r.title, a.username, a.instance AS a_instance, a.canonical_ap_id AS a_ap,
                   c.name AS cname, c.canonical_ap_id AS c_ap,
                   (SELECT COUNT(*) FROM objects x WHERE x.thread_id=t.id AND x.object_type='comment') AS n_comments,
                   (SELECT COUNT(*) FROM objects x WHERE x.thread_id=t.id AND x.discovered_late=1
                       AND t.last_viewed_at IS NOT NULL AND x.first_seen_at > t.last_viewed_at) AS n_new
            FROM archived_threads t JOIN objects o ON o.id=t.root_object_id
            JOIN revisions r ON r.object_id=o.id AND r.seq=o.revision_count
            JOIN communities c ON c.id=t.community_id
            LEFT JOIN actors a ON a.id=o.author_id
            WHERE t.trashed_at IS NULL AND o.dupe_key IN ({marks})
            ORDER BY o.created_at, t.id""", keys).fetchall()
    out: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        out.setdefault(r["dupe_key"], []).append(dict(r))
    return out


def sum_votes(items: list[Any]) -> dict[str, int | None]:
    """Combined counts; None where no copy reported that count."""
    def total(field: str) -> int | None:
        vals = [i[field] for i in items if i[field] is not None]
        return sum(vals) if vals else None
    return {"upvotes": total("upvotes"), "downvotes": total("downvotes"), "score": total("score")}


def breakdown_row(community_name: str, community_ap_id: str, source_domain: str, item: Any) -> dict[str, Any]:
    """One line of a vote breakdown: a copy and the counts its server reported."""
    return {"community": community_name, "community_host": urlsplit(community_ap_id).hostname or "",
            "server": source_domain, "upvotes": item["upvotes"], "downvotes": item["downvotes"],
            "score": item["score"]}
