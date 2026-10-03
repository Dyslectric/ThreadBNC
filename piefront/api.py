"""The PieFed server, as the pages need it.

Reads go to the server's API (/api/alpha) and come back as plain dicts shaped
for the templates. Read as nobody, an answer is reused for everyone not
signed in for a little while (Settings.cache_seconds), so the people reading
without an account cost the server one request per page per half minute, not
one each. Read as someone, nothing is reused but who they are and what they
subscribe to (ME_SECONDS), which every page shows.

Acting (signing in, voting, commenting, subscribing, the inbox, messages) is
ThreadBNC's PieFed adapter's: the same calls its accounts make.
"""

from __future__ import annotations

import hashlib
import threading
import time
import zlib
from dataclasses import dataclass, field
from typing import Any, Callable

from threadbnc.adapters import RemoteAuthError, host_of
from threadbnc.adapters.http import HttpClient
from threadbnc.adapters.lemmy import _ts
from threadbnc.adapters.piefed import PieFedAdapter
from threadbnc.forums import excerpt

API = "/api/alpha"
ME_SECONDS = 60  # how long who you are, your subscriptions and your unread count are believed
SITE_SECONDS = 300
PAGE = 25  # posts to a page
COMMENT_PAGE = 100  # comments asked for at a time
COMMENT_PAGES = 5  # and how many times, for one thread

LISTINGS = {"subscribed": "Subscribed", "local": "Local", "popular": "Popular", "all": "All"}
SORTS = {"hot": "Hot", "new": "New", "active": "Active", "top": "Top", "scaled": "Scaled"}
WINDOWS = {"day": "TopDay", "week": "TopWeek", "month": "TopMonth", "year": "TopYear", "all": "TopAll"}
COMMENT_SORTS = {"hot": "Hot", "top": "Top", "new": "New", "old": "Old"}


def hue(key: str | None) -> int:
    """One of eight colours for a letter shown where there's no picture (style.css .av-N)."""
    return zlib.crc32((key or "").encode()) % 8


def _https(url: Any) -> str | None:
    return url if isinstance(url, str) and url.startswith("https://") else None


class Cache:
    """Answers kept for a while, the oldest dropped once there are too many."""

    def __init__(self, size: int = 2000):
        self.size = size
        self._items: dict[Any, tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def get(self, key: Any) -> Any:
        with self._lock:
            hit = self._items.get(key)
        return hit[1] if hit and hit[0] > time.monotonic() else None

    def put(self, key: Any, value: Any, seconds: float) -> None:
        if seconds <= 0:
            return
        with self._lock:
            if len(self._items) >= self.size:
                now = time.monotonic()
                for k in [k for k, (until, _) in self._items.items() if until <= now]:
                    del self._items[k]
                for k in list(self._items)[: max(0, len(self._items) - self.size + 1)]:
                    del self._items[k]
            self._items[key] = (time.monotonic() + seconds, value)

    def drop(self, key: Any) -> None:
        with self._lock:
            self._items.pop(key, None)


@dataclass
class Me:
    """Whoever is signed in, as their server describes them."""
    person: dict[str, Any]
    follows: list[dict[str, Any]] = field(default_factory=list)  # communities, by title
    unread: int = 0

    @property
    def followed(self) -> set[str]:
        return {c["ap_id"] for c in self.follows}


class PieFed:
    def __init__(self, server: str, http: HttpClient, cache_seconds: int = 30):
        self.server = server
        self.http = http
        self.cache_seconds = cache_seconds
        self.adapter = PieFedAdapter(server, http)
        self._cache = Cache()
        self._forum_lock = threading.Lock()
        # It's this server's own frontend: what its people ask for isn't spaced
        # out (a server that says "too many requests" is still left alone until it said).
        http.throttle.exempt.add(server)

    # ---- reading -------------------------------------------------------------------------
    def get(self, path: str, token: str | None = None, seconds: float | None = None, **params: Any) -> Any:
        """One read. As nobody, the answer is reused for `seconds` (cache_seconds when not said)."""
        if token:
            return self.http.request_json("GET", self.server, API + path, params=params, token=token, throttle=False)
        key = (path, tuple(sorted((k, str(v)) for k, v in params.items() if v is not None)))
        hit = self._cache.get(key)
        if hit is None:
            hit = self.http.request_json("GET", self.server, API + path, params=params, throttle=False)
            self._cache.put(key, hit, self.cache_seconds if seconds is None else seconds)
        return hit

    def site(self) -> dict[str, Any]:
        site = (self.get("/site", seconds=SITE_SECONDS) or {}).get("site") or {}
        return {"name": site.get("name") or self.server, "description": site.get("description") or "",
                "icon": _https(site.get("icon")), "downvotes": site.get("enable_downvotes", True) is not False}

    def me(self, token: str) -> Me:
        key = ("me", hashlib.sha256(token.encode()).hexdigest())
        hit = self._cache.get(key)
        if hit is not None:
            return hit
        data = self.get("/site", token) or {}
        mine = data.get("my_user") or {}
        person = (mine.get("local_user_view") or {}).get("person")
        if not person:
            raise RemoteAuthError(f"{self.server}: session not accepted")
        if isinstance(mine.get("follows"), list):
            follows = [f.get("community") or {} for f in mine["follows"] if isinstance(f, dict)]
        else:
            follows = [v.get("community") or {} for v in self._subscribed(token)]
        try:
            counts = self.get("/user/unread_count", token) or {}
        except RemoteAuthError:
            raise
        except Exception:  # the count is a nicety: the page is worth showing without it
            counts = {}
        unread = sum(v for v in (counts.get(k) for k in ("replies", "mentions", "private_messages")) if isinstance(v, int))
        me = Me(self.person(person),
                sorted((self.community(c) for c in follows if c.get("actor_id")), key=lambda c: c["title"].casefold()),
                unread)
        self._cache.put(key, me, ME_SECONDS)
        return me

    def _subscribed(self, token: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for page in range(1, 11):
            data = self.get("/community/list", token, type_="Subscribed", limit=50, page=page) or {}
            got = data.get("communities") or []
            out += [v for v in got if isinstance(v, dict)]
            if len(got) < 50:
                break
        return out

    def forget(self, token: str | None) -> None:
        """Something about `token`'s account changed (a subscription, the inbox): ask again."""
        if token:
            self._cache.drop(("me", hashlib.sha256(token.encode()).hexdigest()))

    def posts(self, token: str | None, listing: str, sort: str, window: str, page: int,
              community: str | None = None) -> tuple[list[dict[str, Any]], bool]:
        """A page of posts and whether there are more: of a listing, or of a community (its name here)."""
        order = WINDOWS.get(window, "TopDay") if sort == "top" else SORTS.get(sort, "Hot")
        data = self.get("/post/list", token, type_="All" if community else LISTINGS.get(listing, "Popular"),
                        sort=order, page=page, limit=PAGE, community_name=community) or {}
        views = [v for v in data.get("posts") or [] if isinstance(v, dict) and v.get("post")]
        return [self.post(v) for v in views], bool(data.get("next_page")) and len(views) >= PAGE

    def thread(self, token: str | None, post_id: int) -> dict[str, Any]:
        data = self.get("/post", token, id=post_id) or {}
        post = self.post(data["post_view"])
        community = (data.get("community_view") or {}).get("community") or {}
        post["community"] = {**post["community"], **self.community(community)} if community else post["community"]
        return post

    def comments(self, token: str | None, post_id: int, sort: str) -> tuple[list[dict[str, Any]], int, bool]:
        """A post's comments as a tree, how many were read, and whether there are more than that."""
        views: list[dict[str, Any]] = []
        more = False
        for page in range(1, COMMENT_PAGES + 1):
            data = self.get("/comment/list", token, post_id=post_id, sort=COMMENT_SORTS.get(sort, "Hot"),
                            limit=COMMENT_PAGE, page=page, type_="All") or {}
            got = [v for v in data.get("comments") or [] if isinstance(v, dict) and v.get("comment")]
            views += got
            more = bool(data.get("next_page")) and len(got) >= COMMENT_PAGE
            if not more:
                break
        return tree([self.comment(v) for v in views]), len(views), more

    def community_page(self, token: str | None, name: str) -> dict[str, Any]:
        data = self.get("/community", token, name=name) or {}
        view = data.get("community_view") or {}
        counts = view.get("counts") or {}
        return {**self.community(view.get("community") or {}),
                "subscribed": str(view.get("subscribed") or "NotSubscribed"),
                "subscribers": counts.get("total_subscriptions_count", counts.get("subscriptions_count")),
                "posts": counts.get("post_count"), "active": counts.get("active_monthly"),
                "moderators": [self.person(m.get("moderator") or {}) for m in data.get("moderators") or []
                               if isinstance(m, dict)]}

    def forums(self, kind: str, seconds: float, build: Callable[[Any], dict[str, Any]]) -> dict[str, Any]:
        """Every topic, or every public feed, on the server with the communities
        in each, as `build` makes of it. It's one big list, so what's made of
        it is kept for `seconds`, and only one request reads it at a time."""
        key = ("forums", kind)
        made = self._cache.get(key)
        if made is None:
            with self._forum_lock:
                made = self._cache.get(key)
                if made is None:
                    made = build(self.get("/topic/list" if kind == "topic" else "/feed/list", seconds=0,
                                          include_communities="true"))
                    self._cache.put(key, made, seconds)
        return made

    # ---- what the templates are given ------------------------------------------------------
    def handle(self, name: str, ap_id: str | None) -> str:
        """What a community or person is called in an address here: just the name for one of this server's."""
        host = host_of(ap_id or "") or self.server
        return name if host == self.server else f"{name}@{host}"

    def community(self, c: dict[str, Any]) -> dict[str, Any]:
        ap_id, name = c.get("actor_id") or "", str(c.get("name") or "?")
        title = str(c.get("title") or name)
        return {"id": c.get("id"), "name": name, "title": title, "ap_id": ap_id, "host": host_of(ap_id) or self.server,
                "handle": self.handle(name, ap_id), "nsfw": bool(c.get("nsfw")),
                "icon": _https(c.get("icon")) if not c.get("nsfw") else None, "banner": _https(c.get("banner")),
                "description": c.get("description") or "", "letter": title[:1].upper() or "?", "hue": hue(ap_id),
                "mods_only": bool(c.get("restricted_to_mods"))}

    def person(self, p: dict[str, Any]) -> dict[str, Any]:
        ap_id, name = p.get("actor_id") or "", str(p.get("user_name") or p.get("name") or "?")
        return {"id": p.get("id"), "name": name, "display": str(p.get("title") or name), "ap_id": ap_id,
                "host": host_of(ap_id) or self.server, "handle": self.handle(name, ap_id),
                "avatar": _https(p.get("avatar")), "hue": hue(ap_id), "bot": bool(p.get("bot")),
                "deleted": bool(p.get("deleted"))}

    def post(self, v: dict[str, Any]) -> dict[str, Any]:
        p, counts = v["post"], v.get("counts") or {}
        community = self.community(v.get("community") or {})
        url = p.get("url") if isinstance(p.get("url"), str) and p["url"].startswith(("http://", "https://")) else None
        picture = _https(p.get("thumbnail_url"))
        is_image = p.get("post_type") == "Image" and _https(url) is not None
        return {"id": p["id"], "title": p.get("title") or "", "body": p.get("body") or "", "url": url,
                "ap_id": p.get("ap_id") or "", "kind": p.get("post_type") or "", "created_at": _ts(p.get("published")),
                "edited_at": _ts(p.get("updated")), "nsfw": bool(p.get("nsfw")),
                "veil": "NSFW" if p.get("nsfw") or community["nsfw"] else None,
                "locked": bool(p.get("locked")), "deleted": bool(p.get("deleted")), "removed": bool(p.get("removed")),
                "pinned": bool(p.get("sticky") or p.get("instance_sticky")),
                # the picture a list shows small, a grid or the post's page large, and the whole one it opens
                "thumb": _https(p.get("small_thumbnail_url")) or picture, "picture": picture or (url if is_image else None),
                "full": url if is_image else None, "is_image": is_image,
                "excerpt": excerpt(p.get("body"), 280), "n_comments": counts.get("comments") or 0,
                "upvotes": counts.get("upvotes") or 0, "downvotes": counts.get("downvotes") or 0,
                "score": counts.get("score") or 0, "my_vote": v.get("my_vote") or 0, "read": bool(v.get("read")),
                "saved": bool(v.get("saved")), "community": community, "author": self.person(v.get("creator") or {})}

    def comment(self, v: dict[str, Any]) -> dict[str, Any]:
        c, counts = v["comment"], v.get("counts") or {}
        path = [int(x) for x in str(c.get("path") or "").split(".") if x.isdigit()]
        return {"id": c["id"], "parent": path[-2] if len(path) > 2 else None, "body": c.get("body") or "",
                "ap_id": c.get("ap_id") or "", "created_at": _ts(c.get("published")), "edited_at": _ts(c.get("updated")),
                "deleted": bool(c.get("deleted")), "removed": bool(c.get("removed")),
                "upvotes": counts.get("upvotes") or 0, "downvotes": counts.get("downvotes") or 0,
                "score": counts.get("score") or 0, "my_vote": v.get("my_vote") or 0,
                "author": self.person(v.get("creator") or {}), "mod": bool(v.get("creator_is_moderator")),
                "admin": bool(v.get("creator_is_admin")), "children": [], "descendants": 0}


def tree(comments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Comments under the ones they answer, in the order they came. One whose
    parent wasn't among them (a page further on) stands at the top."""
    by_id = {c["id"]: c for c in comments}
    top = []
    for c in comments:
        parent = by_id.get(c["parent"])
        (parent["children"] if parent else top).append(c)

    def count(c: dict[str, Any]) -> int:
        c["descendants"] = sum(1 + count(child) for child in c["children"])
        return c["descendants"]

    for c in top:
        count(c)
    return top
