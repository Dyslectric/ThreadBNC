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

Signed in to a booru (the second half of this file), its own pages and forms are
used as you: comments, votes, tag edits, posting, the queue, reports, bans and
the admin's controls.

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

import httpx

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


# ---- signed in ------------------------------------------------------------------------------
#
# A fedbooru has no accounts of its own: you sign in to its site by sending `!login <code>`
# to its bot as a private message, from an account you already have. ThreadBNC holds your
# accounts, so it does that for you: it starts a sign-in at the booru (begin), sends the
# message as the account you picked, asks the booru who answered (sign_in_state) and, if
# that's the account it sent from, is given the session's token (confirm). The token is
# kept encrypted (the `booru_sessions` table) and sent as a bearer token from then on.
#
# Signed in, the booru's own pages are read as their data (page) and its own forms are
# sent as they are (write): comments, votes, tag edits, uploads, the queue, reports, bans
# and the admin's controls are all what the booru's site does, as you.

SIGN_IN_WAIT = timedelta(minutes=15)  # how long a booru keeps a sign-in open
RENEW_BELOW = 300  # seconds an admin's sign-in still counts as recent, under which it's renewed
MAX_IMAGES = 10  # in one upload, as fedbooru takes them

# The booru's forms that can be sent from here, by their address there.
WRITES = re.compile(
    r"posts/\d+/(?:edit|vote|favourite|unfavourite|report|comment|pool|remove|restore|lock|unlock)"
    r"|posts/\d+/comments/\d+/(?:delete|approve)"
    r"|queue/\d+/(?:approve|reject)"
    r"|submissions/\d+/(?:withdraw|feature|unfeature|lock-comments|unlock-comments)"
    r"|reports/\d+/(?:resolve|dismiss)"
    r"|moderate/(?:tags|ban|unban|trust)"
    r"|admin/(?:role|grant|comments|instance|peer)"
    r"|settings")
_DID = re.compile(r"did:[a-z0-9]+:[A-Za-z0-9._:%-]+")
_POST_LINK = re.compile(r"/posts/(\d+)(#[\w-]+)?")


class Refused(BooruError):
    """The booru said no, and why."""

    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


class SignedOut(BooruError):
    """The session has ended there (it lasts 30 days, or you sent `!logout`)."""


class Stale(BooruError):
    """An admin's action from a sign-in that isn't recent enough: sign in again and resend it."""


def _ask(http: Any, host: str, method: str, path: str, token: str | None = None,
         fields: dict[str, str] | None = None, files: list[tuple[str, tuple[str, bytes, str]]] | None = None,
         params: dict[str, Any] | None = None) -> Any:
    """One request to a booru for JSON, as you if `token` is given. Raises what it refused with."""
    clean = {k: v for k, v in (params or {}).items() if v not in (None, "")}
    url = f"https://{host}{path}" + ("?" + urlencode(clean) if clean else "")
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    content = None
    if method == "POST" and files:
        built = httpx.Request("POST", url, data=fields or {}, files=files)
        content, headers["Content-Type"] = built.read(), built.headers["content-type"]
    elif method == "POST":
        content, headers["Content-Type"] = urlencode(fields or {}).encode(), "application/x-www-form-urlencoded"
    try:
        resp = http.send(method, url, headers=headers, content=content, throttle=False)
    except RemoteError as exc:
        raise BooruError(f"Couldn't reach {host}: {exc}") from exc
    try:
        data = resp.json()
    except ValueError:
        data = None
    said = str(data.get("message") or "") if isinstance(data, dict) else ""
    if resp.status_code == 401 and token:
        raise SignedOut(said or f"Your session on {host} has ended.")
    if isinstance(data, dict) and data.get("stale"):
        raise Stale(said)
    if resp.status_code >= 400:
        raise Refused(said or f"{host} answered HTTP {resp.status_code}.", resp.status_code)
    if data is None:  # a page of its site: a fedbooru from before it answered apps
        raise BooruError(f"{host} didn't answer as a booru that apps can use does.")
    return data


def begin(http: Any, host: str) -> dict[str, Any]:
    """Start a sign-in: {"secret", "code", "bot", "bluesky_bot"}. The bots are who takes
    `!login <code>`, on the fediverse and on Bluesky (None where the booru has none)."""
    try:
        got = _ask(http, host, "POST", "/api/login")
    except Refused as exc:
        if exc.status in (403, 404, 405):  # no such address: a fedbooru from before apps could sign in
            raise BooruError(f"{host} can't be signed in to from here yet: it runs an older fedbooru.") from exc
        raise
    if not isinstance(got, dict) or not got.get("secret") or not got.get("code"):
        raise BooruError(f"{host} didn't start a sign-in.")
    return got


def sign_in_state(http: Any, host: str, secret: str) -> dict[str, Any]:
    """{"state": "waiting" | "claimed" | "gone"}; claimed says who by ("handle", "key")."""
    got = _ask(http, host, "POST", "/api/login/state", fields={"secret": secret})
    return got if isinstance(got, dict) else {"state": "gone"}


def confirm(http: Any, host: str, secret: str) -> str:
    """Take the session of a sign-in that the right account claimed: its token."""
    got = _ask(http, host, "POST", "/api/login/confirm", fields={"secret": secret})
    token = got.get("session") if isinstance(got, dict) else None
    if not isinstance(token, str) or not token:
        raise BooruError(f"{host} didn't give a session.")
    return token


def is_account(claimed: dict[str, Any], actor: str, username: str) -> bool:
    """Whether the account that answered a sign-in is this one of yours (its address, or
    on Bluesky its DID): someone else sending the code mustn't sign you in as them."""
    kind, _, who = str(claimed.get("key") or "").partition(":")
    if kind == "ap":
        return bool(actor) and who.rstrip("/").lower() == actor.rstrip("/").lower()
    if kind == "at":
        did = _DID.search(actor or "")
        if did:
            return did.group(0) == who
        theirs = str(claimed.get("handle") or "").lstrip("@").lower()
        return bool(theirs) and theirs == username.lstrip("@").lower()
    return False


def me(http: Any, host: str, token: str) -> dict[str, Any]:
    """Who you are there and what you may do (the booru's /api/me)."""
    got = _ask(http, host, "GET", "/api/me", token)
    return got if isinstance(got, dict) else {}


def page(http: Any, host: str, path: str, token: str | None = None,
         params: dict[str, Any] | None = None) -> dict[str, Any]:
    """A page of the booru's site as its data: what you'd see there, signed in or not."""
    got = _ask(http, host, "GET", path, token, params=params)
    if not isinstance(got, dict):
        raise BooruError(f"{host} didn't answer as a booru does.")
    return got


def write(http: Any, host: str, token: str, path: str, fields: dict[str, str],
          files: list[tuple[str, tuple[str, bytes, str]]] | None = None) -> str:
    """Send one of the booru's forms as you. Returns what it says it did; raises Refused
    with why not, Stale if it wants a fresh sign-in first, SignedOut if the session's over."""
    got = _ask(http, host, "POST", path, token, fields, files)
    return str(got.get("message") or "Done.") if isinstance(got, dict) else "Done."


def sign_out(http: Any, host: str, token: str) -> None:
    try:
        _ask(http, host, "POST", "/logout", token)
    except BooruError:  # it answers with a redirect, not JSON; and a session already over is over
        pass


def absolute(host: str, url: Any) -> str | None:
    """A picture's address as the booru's page data gives it: on the booru, or elsewhere."""
    if isinstance(url, str) and url.startswith("/") and not url.startswith("//"):
        return f"https://{host}{url}"
    return _picture(url)


def here(bid: int, link: Any) -> str | None:
    """A link to a post on the booru's site, as the page of it here."""
    m = _POST_LINK.fullmatch(link) if isinstance(link, str) else None
    return f"/booru/{bid}/posts/{m.group(1)}{m.group(2) or ''}" if m else None


def page_post(data: dict[str, Any], host: str) -> tuple[dict[str, Any], list[tuple[str, list[dict[str, Any]]]]]:
    """A post's page data as the post and the tags beside it, as posts() and tag_groups() give them."""
    groups = [(str(category), [{"name": t["name"], "category": t["category"], "count": t.get("count")} for t in tags])
              for category, tags in data.get("groups") or []]
    by = {category: [t["name"] for t in tags] for category, tags in groups}
    pid = int(data["id"])
    source = str(data.get("source") or "")
    return {"id": pid, "md5": None, "thumb": None, "file": absolute(host, data.get("file_url")),
            "alt": " ".join(n for names in by.values() for n in names)[:300], "tags": by,
            "rating": data.get("rating") or "unknown", "sensitive": bool(data.get("sensitive")),
            "score": data.get("score"), "favs": data.get("favs"), "created_at": data.get("created"),
            "width": data.get("width") or None, "height": data.get("height") or None,
            "format": data.get("format") or None, "size": None,
            "source": source if source.startswith(("http://", "https://")) else None,
            "parent": data.get("parent"), "has_children": bool(data.get("children")),
            "url": f"https://{host}/posts/{pid}"}, groups


def upload_fields(title: str, tags: str, rating: str, source: str, links: str) -> dict[str, str]:
    """What an upload's form says of all its images, with its links (one a line) if it's by link."""
    fields = {"title": title.strip(), "tags": tags.strip(), "rating": rating.strip(), "source": source.strip()}
    given = [line.strip() for line in links.splitlines() if line.strip()]
    if len(given) > MAX_IMAGES:
        raise BooruError(f"At most {MAX_IMAGES} images at a time.")
    if given:
        fields["mode"] = "link"
        fields.update({f"link{n}": link for n, link in enumerate(given, 1)})
    return {k: v for k, v in fields.items() if v}


# -- your sessions, as kept ---------------------------------------------------------------

def sessions(conn: Conn) -> dict[int, dict[str, Any]]:
    """Your session on each booru you've signed in to (or are signing in to), by booru."""
    out = {}
    for row in conn.execute("SELECT * FROM booru_sessions").fetchall():
        one = dict(row)
        one["me"] = json.loads(one.pop("me_json") or "{}")
        one["retry"] = json.loads(one.pop("retry_json") or "null")
        out[one["booru_id"]] = one
    return out


def keep_pending(conn: Conn, booru_id: int, secret_enc: str, account_id: int, now: str,
                 retry: dict[str, Any] | None = None) -> None:
    """A sign-in under way. A session there already stays as it is until this one's done."""
    conn.execute("INSERT INTO booru_sessions(booru_id, pending_enc, pending_account_id, pending_at, retry_json) "
                 "VALUES (?,?,?,?,?) ON CONFLICT(booru_id) DO UPDATE SET pending_enc=excluded.pending_enc, "
                 "pending_account_id=excluded.pending_account_id, pending_at=excluded.pending_at, "
                 "retry_json=excluded.retry_json",
                 (booru_id, secret_enc, account_id, now, json.dumps(retry) if retry else None))


def _who(said: dict[str, Any]) -> dict[str, Any]:
    return {"name": said.get("name"), "staff": bool(said.get("staff")), "admin": bool(said.get("admin"))}


def keep_session(conn: Conn, booru_id: int, token_enc: str, account_id: int, who: dict[str, Any], now: str) -> None:
    """The session a sign-in gave: it replaces the one before, and the sign-in is over.
    A form held for it (`retry`) is the caller's to send."""
    conn.execute("UPDATE booru_sessions SET token_enc=?, account_id=?, handle=?, me_json=?, signed_in_at=?, "
                 "pending_enc=NULL, pending_account_id=NULL, pending_at=NULL, retry_json=NULL WHERE booru_id=?",
                 (token_enc, account_id, who.get("handle") or who.get("name"), json.dumps(_who(who)), now, booru_id))


def keep_me(conn: Conn, booru_id: int, said: Any) -> None:
    """What a page just said of you (its `me`): your name there, and whether you're staff."""
    if isinstance(said, dict) and said.get("name"):
        conn.execute("UPDATE booru_sessions SET me_json=? WHERE booru_id=?", (json.dumps(_who(said)), booru_id))


def drop_pending(conn: Conn, booru_id: int) -> None:
    conn.execute("UPDATE booru_sessions SET pending_enc=NULL, pending_account_id=NULL, pending_at=NULL, "
                 "retry_json=NULL WHERE booru_id=?", (booru_id,))
    conn.execute("DELETE FROM booru_sessions WHERE booru_id=? AND token_enc IS NULL", (booru_id,))


def forget(conn: Conn, booru_id: int) -> None:
    conn.execute("DELETE FROM booru_sessions WHERE booru_id=?", (booru_id,))


def waited_too_long(session: dict[str, Any], now: str) -> bool:
    started = parse_ts(session.get("pending_at"))
    return started is None or parse_ts(now) - started > SIGN_IN_WAIT
