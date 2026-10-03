"""Boorus: fedbooru servers you've added, browsed as they are on their own site.

A fedbooru server (a tagged image board that federates as a Lemmy-style
community) has a public, Danbooru-style API: /posts.json?tags=…, /tags.json
and /pools.json. The Boorus tab reads the server you've picked through it,
each time you open a page there and never on a schedule, and lays the answer
out as the booru's own pages do: tags down the side, a grid of thumbnails,
a post's picture beside what's known of it. Nothing read this way is kept:
only the list of servers is (the `boorus` table).

Pictures aren't downloaded: your browser loads them from where the booru
says they are (its own server, or the origin of a picture it only links),
so those pages allow pictures from elsewhere (web.py's Content-Security-Policy).
"""

from __future__ import annotations

import re
import threading
import time
from typing import Any
from urllib.parse import urlencode, urlparse

from .adapters import RemoteError
from .db import Conn

SOFTWARE = "fedbooru"
PER_PAGE = 40
TAGS_PER_PAGE = 100
POOLS_PER_PAGE = 50
SIDEBAR_TAGS = 25
INDEX_SIZE = 1000  # the most used tags of a server, read once for the counts beside tags
INDEX_FOR = 600.0  # seconds that list is believed

# Danbooru's category numbers, in the order fedbooru lists them beside posts.
CATEGORIES = {1: "artist", 3: "copyright", 4: "character", 0: "general", 5: "meta"}
_POST_FIELDS = (("artist", "tag_string_artist"), ("copyright", "tag_string_copyright"),
                ("character", "tag_string_character"), ("general", "tag_string_general"), ("meta", "tag_string_meta"))
RATINGS = {"g": "general", "s": "safe", "q": "questionable", "e": "explicit"}

_HANDLE = re.compile(r"[!@~]?[\w.-]+@([\w-]+(?:\.[\w-]+)+)")


class BooruError(ValueError):
    pass


def parse_host(text: str) -> str:
    """A booru's server from its address, a link to any of its pages, or its community's handle."""
    text = (text or "").strip()
    m = _HANDLE.fullmatch(text)
    if m:
        return m.group(1).lower()
    url = urlparse(text if "://" in text else "https://" + text)
    host = (url.hostname or "").lower()
    if url.scheme not in ("http", "https") or "." not in host or not re.fullmatch(r"[a-z0-9.-]+", host):
        raise BooruError("Enter the booru's address, like booru.example.org.")
    return host + (f":{url.port}" if url.port and url.port != 443 else "")


def _read(http: Any, host: str, path: str, params: dict[str, Any] | None = None) -> Any:
    """One request to a booru while you wait for the page, as a browser would make it."""
    clean = {k: v for k, v in (params or {}).items() if v not in (None, "")}
    resp = http.send("GET", f"https://{host}{path}" + ("?" + urlencode(clean) if clean else ""), throttle=False)
    if resp.status_code == 400:  # a search it can't read: it says why
        raise BooruError(resp.text.strip()[:300] or "The booru couldn't read that search.")
    if resp.status_code != 200:
        raise BooruError(f"{host} answered HTTP {resp.status_code}.")
    try:
        return resp.json()
    except ValueError as exc:
        raise BooruError(f"{host} didn't answer as a booru does.") from exc


def look_up(http: Any, host: str) -> dict[str, Any]:
    """What a server says of itself (NodeInfo); refuses anything that isn't fedbooru."""
    try:
        links = _read(http, host, "/.well-known/nodeinfo")
        href = next((l.get("href") for l in (links.get("links") if isinstance(links, dict) else None) or []
                     if isinstance(l, dict) and "nodeinfo" in str(l.get("rel", ""))), None)
        where = urlparse(href or "")
        if where.netloc.lower() != host or not where.path.startswith("/"):
            raise BooruError(f"{host} doesn't say what software it runs.")
        info = _read(http, host, where.path)
    except RemoteError as exc:
        raise BooruError(f"Couldn't reach {host}: {exc}") from exc
    software = info.get("software") if isinstance(info, dict) else None
    name = str((software or {}).get("name") or "").lower()
    if name != SOFTWARE:
        raise BooruError(f"{host} runs {name or 'something else'}, not fedbooru. Follow its communities on Subscriptions.")
    meta = info.get("metadata") if isinstance(info.get("metadata"), dict) else {}
    posts = ((info.get("usage") or {}).get("localPosts")) if isinstance(info.get("usage"), dict) else None
    return {"name": str(meta.get("nodeName") or host)[:100], "posts": posts if isinstance(posts, int) else None,
            "nsfw": bool(meta.get("nsfw"))}


# ---- the servers you've added ---------------------------------------------------------------

def save(conn: Conn, host: str, info: dict[str, Any], now: str) -> int:
    row = conn.execute("SELECT id FROM boorus WHERE host=?", (host,)).fetchone()
    if row:
        conn.execute("UPDATE boorus SET name=?, posts=?, nsfw=?, checked_at=? WHERE id=?",
                     (info["name"], info["posts"], int(info["nsfw"]), now, row["id"]))
        return row["id"]
    conn.execute("INSERT INTO boorus(host, name, posts, nsfw, added_at, checked_at) VALUES (?,?,?,?,?,?)",
                 (host, info["name"], info["posts"], int(info["nsfw"]), now, now))
    return conn.execute("SELECT id FROM boorus WHERE host=?", (host,)).fetchone()["id"]


def all_boorus(conn: Conn) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute("SELECT * FROM boorus ORDER BY LOWER(name), id").fetchall()]


def remove(conn: Conn, booru_id: int) -> None:
    conn.execute("DELETE FROM boorus WHERE id=?", (booru_id,))


# ---- reading one ----------------------------------------------------------------------------

_index: dict[str, tuple[float, dict[str, tuple[int, str]]]] = {}
_index_lock = threading.Lock()


def tag_index(http: Any, host: str) -> dict[str, tuple[int, str]]:
    """A server's most used tags as {name: (posts, category)}: what the counts beside
    tags come from, and a tag's category where the API doesn't say. Empty if it can't be read."""
    with _index_lock:
        kept = _index.get(host)
    if kept and time.monotonic() - kept[0] < INDEX_FOR:
        return kept[1]
    try:
        found = {t["name"]: (t["post_count"], t["category"]) for t in tags(http, host, limit=INDEX_SIZE)}
    except (BooruError, RemoteError):
        return kept[1] if kept else {}
    with _index_lock:
        _index[host] = (time.monotonic(), found)
    return found


def _picture(url: Any) -> str | None:
    return url if isinstance(url, str) and url.startswith("https://") else None


def _post(raw: dict[str, Any], host: str) -> dict[str, Any]:
    by = {name: [t for t in str(raw.get(field) or "").split() if t] for name, field in _POST_FIELDS}
    rating = str(raw.get("rating") or "")
    names = str(raw.get("tag_string") or "").split()
    source = str(raw.get("source") or "")
    return {"id": int(raw["id"]), "thumb": _picture(raw.get("preview_file_url")), "file": _picture(raw.get("file_url")),
            "alt": " ".join(names)[:300], "tags": by, "rating": RATINGS.get(rating, rating or "unknown"),
            "sensitive": rating in ("q", "e"), "score": raw.get("score"), "favs": raw.get("fav_count"),
            "created_at": raw.get("created_at"), "width": raw.get("image_width"), "height": raw.get("image_height"),
            "format": raw.get("file_ext"), "size": raw.get("file_size"),
            "source": source if source.startswith(("http://", "https://")) else None,
            "parent": raw.get("parent_id"), "has_children": bool(raw.get("has_children")),
            "url": f"https://{host}/posts/{int(raw['id'])}"}


def posts(http: Any, host: str, query: str = "", page: int = 1, limit: int = PER_PAGE) -> list[dict[str, Any]]:
    data = _read(http, host, "/posts.json", {"tags": query.strip(), "page": max(1, page), "limit": limit})
    if not isinstance(data, list):
        raise BooruError(f"{host} didn't answer as a booru does.")
    return [_post(p, host) for p in data if isinstance(p, dict) and isinstance(p.get("id"), int)]


def post(http: Any, host: str, post_id: int) -> dict[str, Any] | None:
    """One post. The API has no address for a single post, so it's searched for by its number."""
    found = posts(http, host, f"id:{post_id}", limit=1)
    return found[0] if found else None


def tags(http: Any, host: str, search: str = "", page: int = 1, limit: int = TAGS_PER_PAGE) -> list[dict[str, Any]]:
    search = search.strip().lower()
    if search and "*" not in search:  # as the booru's own Tags page: names that start with it
        search += "*"
    data = _read(http, host, "/tags.json", {"search[name_matches]": search, "page": max(1, page), "limit": limit})
    if not isinstance(data, list):
        raise BooruError(f"{host} didn't answer as a booru does.")
    return [{"name": str(t["name"]), "post_count": int(t.get("post_count") or 0),
             "category": CATEGORIES.get(t.get("category"), "general")}
            for t in data if isinstance(t, dict) and t.get("name")]


def pools(http: Any, host: str, page: int = 1, limit: int = POOLS_PER_PAGE) -> list[dict[str, Any]]:
    data = _read(http, host, "/pools.json", {"page": max(1, page), "limit": limit})
    if not isinstance(data, list):
        raise BooruError(f"{host} didn't answer as a booru does.")
    return [{"id": int(p["id"]), "name": str(p.get("name") or f"pool#{p['id']}"),
             "post_count": int(p.get("post_count") or 0)}
            for p in data if isinstance(p, dict) and isinstance(p.get("id"), int)]


def tag_groups(shown: list[dict[str, Any]], index: dict[str, tuple[int, str]],
               limit: int | None = SIDEBAR_TAGS) -> list[tuple[str, list[dict[str, Any]]]]:
    """The tags beside a page of posts, as the booru lists them: the most common among
    the posts shown, under their categories, each with how many posts the server has of it."""
    seen: dict[str, list[Any]] = {}
    for p in shown:
        for category, names in p["tags"].items():
            for name in names:
                seen.setdefault(name, [category, 0])[1] += 1
    order = sorted(seen, key=lambda n: (-seen[n][1], -index.get(n, (0, ""))[0], n))[:limit]
    groups: dict[str, list[dict[str, Any]]] = {c: [] for c in CATEGORIES.values()}
    for name in order:
        groups[seen[name][0]].append({"name": name, "category": seen[name][0],
                                      "count": index[name][0] if name in index else None})
    return [(category, found) for category, found in groups.items() if found]
