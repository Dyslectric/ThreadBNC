"""Boorus: fedbooru servers you've added, browsed as they are on their own site.

A fedbooru server (a tagged image board that federates as a Lemmy-style
community) has a public, Danbooru-style API: /posts.json?tags=…, /tags.json
and /pools.json. The Boorus tab reads the server you've picked through it,
each time you open a page there and never on a schedule, and lays the answer
out as the booru's own pages do: tags down the side, a grid of thumbnails,
a post's picture beside what's known of it. Nothing read this way is kept:
only the list of servers is (the `boorus` table).

Several can be browsed at once: the same search is asked of each (side by
side, not one after another) and the answers are put together, newest first.
A fedbooru can show the posts of others it has as peers, so a picture that
two of the boorus ticked both list is shown once (by its file's hash).

Tag categories: the Danbooru-style API only knows Danbooru's five, and counts
any other a booru's staff added (fedbooru lets them: "photographer", say) as
general. So which category a tag is in, and the categories' order and
colours, are read from the booru's own Tags page and its categories
stylesheet, with its tag counts (tag_index).

The peers a booru names (NodeInfo's metadata.peers) are kept with it and
offered in the menu as boorus to add. They're never added by themselves.

Pictures aren't downloaded: your browser loads them from where the booru
says they are (its own server, or the origin of a picture it only links),
so those pages allow pictures from elsewhere (web.py's Content-Security-Policy).
"""

from __future__ import annotations

import contextvars
import html
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from typing import Any, Callable
from urllib.parse import urlencode, urlparse

from .adapters import RemoteError
from .db import Conn, parse_ts

SOFTWARE = "fedbooru"
PER_PAGE = 40
TAGS_PER_PAGE = 100
POOLS_PER_PAGE = 50
SIDEBAR_TAGS = 25
INDEX_SIZE = 1000  # the most used tags of a server, read once for the counts beside tags
INDEX_FOR = 600.0  # seconds that list is believed
CATEGORY_PAGES = 3  # of the booru's own Tags page (100 tags each), read for the tags' categories
RECHECK = timedelta(days=1)  # how long what a server said of itself (its name, its peers) is believed
MAX_PEERS = 50

# Danbooru's category numbers, in the order fedbooru lists them beside posts.
CATEGORIES = {1: "artist", 3: "copyright", 4: "character", 0: "general", 5: "meta"}
_POST_FIELDS = (("artist", "tag_string_artist"), ("copyright", "tag_string_copyright"),
                ("character", "tag_string_character"), ("general", "tag_string_general"), ("meta", "tag_string_meta"))
RATINGS = {"g": "general", "s": "safe", "q": "questionable", "e": "explicit"}

# A category in the booru's /static/categories.css (":root { --tag-artist: #b0471e; ... }", and
# again for dark screens), and a tag on its Tags page.
_CATEGORY = re.compile(r"--tag-([a-z0-9_-]{1,40}):\s*(#[0-9a-fA-F]{3,8})\s*;")
_TAG_ROW = re.compile(r'<td class="tag-([a-z0-9_-]{1,40})"><a href="/posts\?tags=[^"]*">([^<]+)</a>')
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


def _page(http: Any, host: str, path: str, params: dict[str, Any] | None = None) -> str:
    """A page of the booru's own site, as text ("" if it isn't there)."""
    clean = {k: v for k, v in (params or {}).items() if v not in (None, "")}
    resp = http.send("GET", f"https://{host}{path}" + ("?" + urlencode(clean) if clean else ""), throttle=False)
    return resp.text if resp.status_code == 200 else ""


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
            "nsfw": bool(meta.get("nsfw")), "peers": _peers(meta.get("peers"), host)}


def _peers(raw: Any, host: str) -> list[dict[str, str]]:
    """The other boorus a server says it shows the posts of: [{"host", "name"}]."""
    found: dict[str, dict[str, str]] = {}
    for one in raw if isinstance(raw, list) else []:
        domain = (one.get("domain") or one.get("host")) if isinstance(one, dict) else one
        try:
            peer = parse_host(str(domain or ""))
        except BooruError:
            continue
        name = str(one.get("name") or peer)[:100] if isinstance(one, dict) else peer
        if peer != host and len(found) < MAX_PEERS:
            found.setdefault(peer, {"host": peer, "name": name})
    return list(found.values())


# ---- the servers you've added ---------------------------------------------------------------

def save(conn: Conn, host: str, info: dict[str, Any], now: str) -> int:
    row = conn.execute("SELECT id FROM boorus WHERE host=?", (host,)).fetchone()
    if row:
        conn.execute("UPDATE boorus SET name=?, posts=?, nsfw=?, peers_json=?, checked_at=? WHERE id=?",
                     (info["name"], info["posts"], int(info["nsfw"]), json.dumps(info["peers"]), now, row["id"]))
        return row["id"]
    conn.execute("INSERT INTO boorus(host, name, posts, nsfw, peers_json, added_at, checked_at) VALUES (?,?,?,?,?,?,?)",
                 (host, info["name"], info["posts"], int(info["nsfw"]), json.dumps(info["peers"]), now, now))
    return conn.execute("SELECT id FROM boorus WHERE host=?", (host,)).fetchone()["id"]


def all_boorus(conn: Conn) -> list[dict[str, Any]]:
    out = []
    for row in conn.execute("SELECT * FROM boorus ORDER BY LOWER(name), id").fetchall():
        booru = dict(row)
        booru["peers"] = json.loads(booru.pop("peers_json") or "[]")
        out.append(booru)
    return out


def stale(booru: dict[str, Any], now: str) -> bool:
    return parse_ts(now) - parse_ts(booru["checked_at"]) > RECHECK


def suggestions(boorus: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Boorus that yours federate with and you haven't added, each with the one that named it."""
    have = {b["host"] for b in boorus}
    found: dict[str, dict[str, str]] = {}
    for b in boorus:
        for peer in b["peers"]:
            if peer["host"] not in have:
                found.setdefault(peer["host"], {**peer, "via": b["name"]})
    return sorted(found.values(), key=lambda p: p["name"].casefold())


def chosen(boorus: list[dict[str, Any]], kept: str | None) -> list[dict[str, Any]]:
    """Those ticked in the menu (`kept`: their ids, comma-separated); all of them when none is."""
    ids = {part for part in (kept or "").split(",") if part}
    return [b for b in boorus if str(b["id"]) in ids] or boorus


def remove(conn: Conn, booru_id: int) -> None:
    conn.execute("DELETE FROM boorus WHERE id=?", (booru_id,))


# ---- reading one ----------------------------------------------------------------------------

_index: dict[str, tuple[float, dict[str, tuple[int, str]]]] = {}
_categories: dict[str, list[tuple[str, str, str]]] = {}  # host -> [(name, colour, colour on dark)], in its order
_index_lock = threading.Lock()


def tag_index(http: Any, host: str) -> dict[str, tuple[int, str]]:
    """A server's most used tags as {name: (posts, category)}: what the counts beside
    tags come from, and their categories. Empty if it can't be read."""
    with _index_lock:
        kept = _index.get(host)
    if kept and time.monotonic() - kept[0] < INDEX_FOR:
        return kept[1]
    try:
        found = {t["name"]: (t["post_count"], t["category"]) for t in tags(http, host, limit=INDEX_SIZE)}
        own, order = _own_categories(http, host)
    except (BooruError, RemoteError):
        return kept[1] if kept else {}
    for name, category in own.items():
        if name in found:
            found[name] = (found[name][0], category)
    with _index_lock:
        _index[host] = (time.monotonic(), found)
        _categories[host] = order
    return found


def _own_categories(http: Any, host: str) -> tuple[dict[str, str], list[tuple[str, str, str]]]:
    """The categories of a booru's most used tags as its own Tags page has them, and its
    categories with their colours. Nothing where its pages aren't laid out as fedbooru's."""
    light, _, dark = _page(http, host, "/static/categories.css").partition("@media")
    on_dark = dict(_CATEGORY.findall(dark))
    order = [(name, colour, on_dark.get(name, colour)) for name, colour in _CATEGORY.findall(light)]
    own: dict[str, str] = {}
    if any(name not in CATEGORIES.values() for name, _, _ in order):  # only a booru with categories of its own
        for page in range(1, CATEGORY_PAGES + 1):
            rows = _TAG_ROW.findall(_page(http, host, "/tags", {"page": page}))
            own.update({html.unescape(name): category for category, name in rows})
            if len(rows) < 100:
                break
    return own, order


def categories(hosts: list[str]) -> list[tuple[str, str, str]]:
    """The categories of these boorus, as last read: Danbooru's five first, in fedbooru's
    order, unless a booru orders them (and names others)."""
    out: dict[str, tuple[str, str, str]] = {}
    with _index_lock:
        for host in hosts:
            for one in _categories.get(host, []):
                out.setdefault(one[0], one)
    for name in CATEGORIES.values():
        out.setdefault(name, (name, "", ""))
    return list(out.values())


def category_styles() -> str:
    """A stylesheet colouring the tags of categories boorus added themselves (the five
    usual ones are in style.css), from what's been read of them."""
    with _index_lock:
        known = {one[0]: one for order in _categories.values() for one in order}
    return "".join(
        f":is(.booru-taglist, .booru-table) .tag-{name}, :is(.booru-taglist, .booru-table) .tag-{name} > a "
        f"{{ color: light-dark({colour}, {dark}); }}\n"
        for name, colour, dark in known.values() if name not in CATEGORIES.values())


def each(boorus: list[dict[str, Any]],
         read: Callable[[dict[str, Any]], Any]) -> tuple[list[tuple[dict[str, Any], Any]], list[str]]:
    """Ask several boorus the same thing at once. Returns what those that answered
    said, as (booru, answer), and why the others didn't."""
    def one(booru: dict[str, Any]) -> Any:
        try:
            return read(booru)
        except (BooruError, RemoteError) as exc:
            return exc

    if len(boorus) <= 1:
        answers = [one(b) for b in boorus]
    else:  # each in a copy of this context, so its requests are counted for what this one is (traffic.py)
        with ThreadPoolExecutor(max_workers=min(8, len(boorus))) as pool:
            answers = list(pool.map(lambda b: contextvars.copy_context().run(one, b), boorus))
    several = len(boorus) > 1
    found = [(b, a) for b, a in zip(boorus, answers) if not isinstance(a, Exception)]
    errors = [f"{b['name']}: {a}" if several else str(a) for b, a in zip(boorus, answers) if isinstance(a, Exception)]
    return found, errors


def together(found: list[tuple[dict[str, Any], list[dict[str, Any]]]], query: str = "") -> list[dict[str, Any]]:
    """Several boorus' posts as one list: newest first (a search that asks for another
    order takes them in turn instead), a picture two of them list shown once."""
    for booru, theirs in found:
        for p in theirs:
            p["booru"] = booru
    if "order:" in query.lower():
        longest = max((len(theirs) for _, theirs in found), default=0)
        merged = [theirs[i] for i in range(longest) for _, theirs in found if i < len(theirs)]
    else:
        merged = sorted((p for _, theirs in found for p in theirs), key=lambda p: p["created_at"] or "", reverse=True)
    seen: set[str] = set()
    out = []
    for p in merged:
        key = p["md5"] or p["file"] or f"{p['booru']['id']}:{p['id']}"
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out


def merged_index(indexes: list[dict[str, tuple[int, str]]]) -> dict[str, tuple[int, str]]:
    """Several boorus' tag counts added up."""
    if len(indexes) == 1:
        return indexes[0]
    out: dict[str, tuple[int, str]] = {}
    for index in indexes:
        for name, (count, category) in index.items():
            out[name] = (out[name][0] + count, out[name][1]) if name in out else (count, category)
    return out


def _picture(url: Any) -> str | None:
    return url if isinstance(url, str) and url.startswith("https://") else None


def _post(raw: dict[str, Any], host: str) -> dict[str, Any]:
    by = {name: [t for t in str(raw.get(field) or "").split() if t] for name, field in _POST_FIELDS}
    rating = str(raw.get("rating") or "")
    names = str(raw.get("tag_string") or "").split()
    source = str(raw.get("source") or "")
    md5 = raw.get("md5")
    return {"id": int(raw["id"]), "md5": md5 if isinstance(md5, str) and md5 else None, "thumb": _picture(raw.get("preview_file_url")), "file": _picture(raw.get("file_url")),
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


def tag_groups(shown: list[dict[str, Any]], index: dict[str, tuple[int, str]], limit: int | None = SIDEBAR_TAGS,
               order: list[tuple[str, str, str]] | None = None) -> list[tuple[str, list[dict[str, Any]]]]:
    """The tags beside a page of posts, as the booru lists them: the most common among
    the posts shown, under their categories (in `order`, from categories()), each with
    how many posts the server has of it."""
    seen: dict[str, list[Any]] = {}
    for p in shown:
        for category, names in p["tags"].items():
            for name in names:  # the API only knows Danbooru's categories: the index knows the booru's own
                seen.setdefault(name, [index[name][1] if name in index else category, 0])[1] += 1
    top = sorted(seen, key=lambda n: (-seen[n][1], -index.get(n, (0, ""))[0], n))[:limit]
    groups: dict[str, list[dict[str, Any]]] = {name: [] for name, _, _ in order or categories([])}
    for name in top:
        groups.setdefault(seen[name][0], []).append({"name": name, "category": seen[name][0],
                                                     "count": index[name][0] if name in index else None})
    return [(category, found) for category, found in groups.items() if found]
