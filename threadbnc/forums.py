"""Forums: trees of communities someone else arranged, added by their link.

On PieFed anyone can arrange communities (from any Lemmy or PieFed server)
into a feed, and feeds into other feeds; an admin arranges them into topics
the same way. Forumverse on piefed.social is one such tree, several levels
deep. Neither kind of nesting federates, so a tree is read from the server it
lives on, through its API: one request lists every public feed (or topic)
there with its communities, and the tree asked for is cut out of that. That's
done when it's added and when you refresh it, never on a schedule.

What that list leaves out of a community (its description, subscribers,
posts) is asked of the same server when a part of the tree listing it is
opened (JOB), at most ASKED at a time, and believed for RECHECK. Pictures
(icons of feeds and communities) are downloaded (media.py) only once they're
on a page you open, like avatars, and kept while the forum is
(forum_media, which media.collect_orphans leaves alone).

A forum is kept as its tree (forums.tree_json): nodes of
{"name", "title", "description", "icon", "banner", "nsfw", "url", "total",
 "children": [node, ...], "communities": [{"ap_id", "name", "title", "icon", "nsfw", "ref"}, ...]}
where "total" counts the different communities in the node and below it, and
"ref" is the community's id on the forum's server, for asking about it.
"""

from __future__ import annotations

import json
import re
import zlib
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Iterable
from urllib.parse import quote, urlparse

from . import media as media_mod
from .adapters import RemoteNotFound
from .db import Conn, parse_ts, utcnow

JOB = "forum_details"
RECHECK = timedelta(days=14)  # how long what a server said of a community is believed
ASKED = 50  # most communities asked about for one page
MAX_COMMUNITIES = 20000  # in one tree; far more than any there is

_HANDLE = re.compile(r"[~!]?([\w.-]+)@([\w-]+(?:\.[\w-]+)+)")


class ForumError(ValueError):
    pass


@dataclass(frozen=True)
class Link:
    """What a forum's link points at: a feed by its name, a topic, or all of a server's topics (name None)."""
    host: str
    kind: str  # feed | topic
    name: str | None


def parse_link(text: str) -> Link:
    """A PieFed feed or topic from its link (https://piefed.social/f/forumverse,
    https://piefed.social/topic/technology, https://piefed.social/topics) or
    a feed's handle (~forumverse@piefed.social)."""
    text = (text or "").strip()
    m = _HANDLE.fullmatch(text)
    if m:
        return Link(m.group(2).lower(), "feed", m.group(1).lower())
    url = urlparse(text if "://" in text else "https://" + text)
    host = (url.hostname or "").lower()
    parts = [p for p in url.path.split("/") if p]
    if url.scheme in ("http", "https") and "." in host:
        if len(parts) >= 2 and parts[0] == "f":
            return Link(host, "feed", parts[1].lower())
        if len(parts) >= 2 and parts[0] == "topic":
            return Link(host, "topic", parts[-1].lower())
        if parts == ["topics"] or parts == ["topic"]:
            return Link(host, "topic", None)
    raise ForumError("Use the link of a PieFed feed (…/f/name) or topic (…/topic/name, or …/topics for all of them).")


def _https(url: Any) -> str | None:
    return url if isinstance(url, str) and url.startswith("https://") else None


def _node(raw: dict[str, Any], link: Link, trail: tuple[str, ...]) -> dict[str, Any]:
    name = str(raw.get("name") or raw.get("id") or "")
    here = (*trail, name)
    url = _https(raw.get("actor_id")) or f"https://{link.host}/topic/" + "/".join(quote(p) for p in here)
    communities = []
    for c in raw.get("communities") or []:
        ap_id = _https(c.get("actor_id")) if isinstance(c, dict) else None
        if not ap_id or c.get("deleted") or c.get("removed"):
            continue
        communities.append({"ap_id": ap_id, "name": str(c.get("name") or ""), "title": c.get("title") or c.get("name"),
                            "icon": _https(c.get("icon")), "nsfw": bool(c.get("nsfw")), "ref": c.get("id")})
    communities.sort(key=lambda c: str(c["title"] or c["name"]).casefold())
    return {"name": name, "title": raw.get("title") or name, "description": str(raw.get("description") or "").strip(),
            "icon": _https(raw.get("icon")), "banner": _https(raw.get("banner")),
            "nsfw": bool(raw.get("nsfw") or raw.get("nsfl")), "url": url,
            "children": [_node(ch, link, here) for ch in raw.get("children") or [] if isinstance(ch, dict)],
            "communities": communities}


def _count(node: dict[str, Any]) -> set[str]:
    """Set each node's "total"; returns the communities in it and below it."""
    seen = {c["ap_id"] for c in node["communities"]}
    for child in node["children"]:
        seen |= _count(child)
    node["total"] = len(seen)
    return seen


def _matches(raw: dict[str, Any], name: str) -> bool:
    actor = urlparse(str(raw.get("actor_id") or "")).path.strip("/").split("/")
    return str(raw.get("name") or "").lower() == name or (len(actor) >= 2 and actor[0] == "f" and actor[1].lower() == name)


def _find(nodes: Iterable[Any], name: str) -> tuple[dict[str, Any], tuple[str, ...]] | None:
    """The node called `name` anywhere among `nodes`, with the names of those it's in."""
    for raw in nodes:
        if not isinstance(raw, dict):
            continue
        if _matches(raw, name):
            return raw, ()
        found = _find(raw.get("children") or [], name)
        if found:
            return found[0], (str(raw.get("name") or ""), *found[1])
    return None


def tree_from(data: Any, link: Link) -> dict[str, Any]:
    """The tree `link` points at, from the server's list of feeds or topics."""
    key = "feeds" if link.kind == "feed" else "topics"
    nodes = data.get(key) if isinstance(data, dict) else None
    if not isinstance(nodes, list):
        raise ForumError(f"{link.host} didn't list its {key}; is it a PieFed server?")
    if link.name is None:
        root = _node({"name": "topics", "title": f"Topics on {link.host}"}, link, ())
        root["url"] = f"https://{link.host}/topics"
        root["children"] = [_node(raw, link, ()) for raw in nodes if isinstance(raw, dict)]
    else:
        found = _find(nodes, link.name)
        if not found:
            raise ForumError(f"{link.host} has no public {link.kind} called {link.name}.")
        # a topic's address is the path of names down to it; a feed's is its own
        root = _node(found[0], link, found[1] if link.kind == "topic" else ())
    if len(_count(root)) > MAX_COMMUNITIES:
        raise ForumError("That's too many communities for one forum.")
    return root


def fetch(http: Any, link: Link) -> dict[str, Any]:
    """Read the tree `link` points at from its server: one request, a few MB for a big server."""
    path = "/api/alpha/feed/list" if link.kind == "feed" else "/api/alpha/topic/list"
    try:
        data = http.get_json(link.host, path, {"include_communities": "true"})
    except RemoteNotFound as exc:
        raise ForumError(f"{link.host} doesn't list feeds or topics; is it a PieFed server?") from exc
    return tree_from(data, link)


# ---- kept forums ----------------------------------------------------------------------------

def save(conn: Conn, link: Link, tree: dict[str, Any], now: str) -> int:
    """Keep a forum's tree (a new one, or a fresh copy of one kept already); returns its id."""
    row = conn.execute("SELECT id FROM forums WHERE ap_id=?", (tree["url"],)).fetchone()
    if row:
        conn.execute("UPDATE forums SET title=?, tree_json=?, fetched_at=? WHERE id=?",
                     (tree["title"], json.dumps(tree), now, row["id"]))
        return row["id"]
    position = conn.execute("SELECT COALESCE(MAX(position), 0) + 1 FROM forums").fetchone()[0]
    conn.execute("INSERT INTO forums(ap_id, host, kind, name, title, tree_json, position, added_at, fetched_at) "
                 "VALUES (?,?,?,?,?,?,?,?,?)",
                 (tree["url"], link.host, link.kind, link.name, tree["title"], json.dumps(tree), position, now, now))
    return conn.execute("SELECT id FROM forums WHERE ap_id=?", (tree["url"],)).fetchone()["id"]


def load(conn: Conn, forum_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM forums WHERE id=?", (forum_id,)).fetchone()
    if row is None:
        return None
    forum = dict(row)
    forum["tree"] = json.loads(forum.pop("tree_json"))
    return forum


def all_forums(conn: Conn) -> list[dict[str, Any]]:
    out = []
    for row in conn.execute("SELECT * FROM forums ORDER BY position, id").fetchall():
        forum = dict(row)
        forum["tree"] = json.loads(forum.pop("tree_json"))
        out.append(forum)
    return out


def link_of(forum: dict[str, Any]) -> Link:
    return Link(forum["host"], forum["kind"], forum["name"])


def remove(conn: Conn, forum_id: int) -> None:
    """Forget a forum. Its pictures go with the next clean-up (media.collect_orphans)."""
    conn.execute("DELETE FROM forum_media WHERE forum_id=?", (forum_id,))
    conn.execute("DELETE FROM forums WHERE id=?", (forum_id,))


def walk(tree: dict[str, Any], path: list[str]) -> tuple[dict[str, Any], list[tuple[dict[str, Any], list[str]]]] | None:
    """The node at `path` (names below the root), with the nodes above it and their paths."""
    node, trail, here = tree, [], []
    for name in path:
        trail.append((node, list(here)))
        node = next((ch for ch in node["children"] if ch["name"] == name), None)
        if node is None:
            return None
        here.append(name)
    return node, trail


def communities_in(node: dict[str, Any]) -> Iterable[dict[str, Any]]:
    yield from node["communities"]
    for child in node["children"]:
        yield from communities_in(child)


def hue_key(key: str | None) -> int:
    return zlib.crc32((key or "").encode())


def hue(key: str | None) -> int:
    """One of eight colours for a letter shown while there's no picture (style.css .av-N)."""
    return hue_key(key) % 8


def excerpt(text: str | None, limit: int = 180) -> str:
    """A description's first paragraph, as plain text, cut short."""
    for para in re.split(r"\n\s*\n", text or ""):
        plain = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", para)  # pictures
        plain = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", plain)  # links: their words
        plain = re.sub(r"[*_`#>]+", "", plain)
        plain = " ".join(plain.split())
        if plain:
            return plain if len(plain) <= limit else plain[:limit - 1].rsplit(" ", 1)[0] + "…"
    return ""


# ---- forums added by themselves (Settings.default_forums) --------------------------------------

ADD_JOB = "forum_add"
ADDED_KEY = "default_forums_added"  # app_settings: the default forums already added once (JSON list)


def start_defaults(conn: Conn, links: Iterable[str]) -> list[str]:
    """The default forums not added before, now noted as added: they're read
    from their servers (ADD_JOB) the first time the Forums page is opened, once
    each, so a forum you remove stays removed."""
    row = conn.execute("SELECT value FROM app_settings WHERE key=?", (ADDED_KEY,)).fetchone()
    try:
        done = set(json.loads(row["value"])) if row and row["value"] else set()
    except ValueError:
        done = set()
    new = [link for link in links if link not in done]
    if new:
        conn.execute("INSERT INTO app_settings(key, value) VALUES (?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (ADDED_KEY, json.dumps(sorted(done | set(new)))))
    return [link for link in new if not _kept(conn, link)]


def _kept(conn: Conn, link: str) -> bool:
    """Whether the forum a link points at was added already (or the link doesn't point at one)."""
    try:
        where = parse_link(link)
    except ForumError:
        return True
    return conn.execute("SELECT 1 FROM forums WHERE host=? AND kind=? AND COALESCE(name, '')=?",
                        (where.host, where.kind, where.name or "")).fetchone() is not None


def adding(conn: Conn) -> list[Link]:
    """The default forums being read from their servers (ADD_JOB queued or running)."""
    out = []
    for r in conn.execute("SELECT payload_json FROM jobs WHERE kind=? AND status IN ('queued', 'running') ORDER BY id",
                          (ADD_JOB,)).fetchall():
        try:
            out.append(parse_link(json.loads(r["payload_json"])["link"]))
        except (ValueError, KeyError):
            continue
    return out


def add(bouncer: Any, payload: dict[str, Any]) -> dict[str, Any]:
    """ADD_JOB: a default forum, read from its server and kept."""
    link = parse_link(payload["link"])
    tree = fetch(bouncer.http, link)
    with bouncer.db.transaction() as conn:
        return {"forum_id": save(conn, link, tree, utcnow())}


# ---- what's asked of the server, and pictures, for a page you open ----------------------------

def job_payload(forum_id: int, path: list[str]) -> dict[str, Any]:
    return {"forum_id": forum_id, "path": "/".join(path)}


def looking(conn: Conn, forum_id: int, path: list[str]) -> bool:
    """Whether a page's communities are being asked about (JOB queued or running)."""
    return conn.execute("SELECT 1 FROM jobs WHERE kind=? AND status IN ('queued', 'running') AND payload_json=?",
                        (JOB, json.dumps(job_payload(forum_id, path)))).fetchone() is not None


def details(conn: Conn, ap_ids: list[str]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for start in range(0, len(ap_ids), 500):
        chunk = ap_ids[start:start + 500]
        for r in conn.execute(f"SELECT * FROM forum_communities WHERE ap_id IN ({','.join('?' * len(chunk))})",
                              chunk).fetchall():
            out[r["ap_id"]] = dict(r)
    return out


def stale(row: dict[str, Any] | None, now: str) -> bool:
    checked, moment = parse_ts(row["checked_at"] if row else None), parse_ts(now)
    return checked is None or moment is None or moment - checked >= RECHECK


def pictures(conn: Conn, forum_id: int, urls: Iterable[str | None], now: str) -> dict[str, tuple[int, str]]:
    """Pictures on a page you opened, registered to be downloaded and kept with
    the forum: (media id, status) by address."""
    out: dict[str, tuple[int, str]] = {}
    for url in dict.fromkeys(u for u in urls if u):
        mid = media_mod.media_id(conn, url, now)
        conn.execute("INSERT INTO forum_media(forum_id, media_id) VALUES (?,?) ON CONFLICT DO NOTHING",
                     (forum_id, mid))
        status = conn.execute("SELECT status FROM media WHERE id=?", (mid,)).fetchone()["status"]
        out[url] = (mid, status)
    return out


def known_pictures(conn: Conn, urls: Iterable[str | None]) -> dict[str, int]:
    """Pictures already downloaded, by address (for pages that don't download any)."""
    wanted = list(dict.fromkeys(u for u in urls if u))
    out: dict[str, int] = {}
    for start in range(0, len(wanted), 500):
        chunk = wanted[start:start + 500]
        for r in conn.execute(f"SELECT id, url FROM media WHERE status='ok' AND url IN ({','.join('?' * len(chunk))})",
                              chunk).fetchall():
            out[r["url"]] = r["id"]
    return out


def _count_of(counts: dict[str, Any], *keys: str) -> int | None:
    for key in keys:
        if isinstance(counts.get(key), int):
            return counts[key]
    return None


def look_up(bouncer: Any, payload: dict[str, Any]) -> dict[str, Any]:
    """JOB: what the forum's server says of the communities on one of its pages
    (a page listing them was opened): description, subscribers and posts."""
    now = utcnow()
    with bouncer.db.connect() as conn:
        forum = load(conn, payload["forum_id"])
        if forum is None:
            return {"asked": 0}
        found = walk(forum["tree"], [p for p in payload["path"].split("/") if p])
        if found is None:
            return {"asked": 0}
        wanted = [c for c in found[0]["communities"] if c.get("ref") is not None]
        known = details(conn, [c["ap_id"] for c in wanted])
    wanted = [c for c in wanted if stale(known.get(c["ap_id"]), now)][:ASKED]
    for c in wanted:
        try:
            data = bouncer.http.get_json(forum["host"], "/api/alpha/community", {"id": c["ref"]})
        except RemoteNotFound:
            data = {}
        view = data.get("community_view") if isinstance(data, dict) else None
        view = view if isinstance(view, dict) else {}
        community = view.get("community") if isinstance(view.get("community"), dict) else {}
        counts = view.get("counts") if isinstance(view.get("counts"), dict) else {}
        same = community.get("actor_id") == c["ap_id"]
        with bouncer.db.transaction() as conn:
            conn.execute(
                "INSERT INTO forum_communities(ap_id, description, subscribers, posts, active_month, checked_at) "
                "VALUES (?,?,?,?,?,?) ON CONFLICT(ap_id) DO UPDATE SET description=excluded.description, "
                "subscribers=excluded.subscribers, posts=excluded.posts, active_month=excluded.active_month, "
                "checked_at=excluded.checked_at",
                (c["ap_id"], str(community.get("description") or "").strip() or None if same else None,
                 _count_of(counts, "total_subscriptions_count", "subscribers", "subscriptions_count") if same else None,
                 _count_of(counts, "post_count", "posts") if same else None,
                 _count_of(counts, "active_monthly", "users_active_month") if same else None, now))
    return {"asked": len(wanted)}
