"""Reddit, read through its OAuth API (oauth.reddit.com).

Reddit isn't federated, so it's one "server" (REDDIT_DOMAIN) whatever host a
link uses. Ids are Reddit's base-36 ids without the t3_/t1_ prefix, and the
canonical ids stored in the archive are www.reddit.com permalinks, so a post is
the same object however it was linked.

Writes (votes, comments, posts, edits, deletions) happen as the Reddit
account you logged in with, which accounts.Poster uses automatically for
anything on Reddit. The connection holds that sign-in, so the `token` the
write methods are given (the Lemmy-style session token) is ignored.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Protocol
from urllib.parse import urlparse

from ..db import fmt_ts
from .base import (
    REDDIT_DOMAIN,
    CommentList,
    CommunityRef,
    NActor,
    NComment,
    NCommunity,
    NInboxItem,
    NPost,
    RemoteNotFound,
    RemoteRejected,
    ThreadiverseAdapter,
    ThreadRef,
    is_reddit_host,
    parse_thread_url,
)

WWW = "https://www.reddit.com"
# Live-tab sorts (named like Lemmy's) -> Reddit listing and its parameters.
SORTS: dict[str, tuple[str, dict[str, str]]] = {
    "New": ("new", {}), "Hot": ("hot", {}), "Active": ("rising", {}),
    "TopDay": ("top", {"t": "day"}), "TopWeek": ("top", {"t": "week"}), "TopAll": ("top", {"t": "all"}),
}
# Removal reasons Reddit reports that mean the author did it.
_BY_AUTHOR = {"deleted", "author"}
_THING_PATH = re.compile(r"^/r/[^/]+/comments/([a-z0-9]+)(?:/[^/]*/([a-z0-9]+))?", re.I)
_SUB_PATH = re.compile(r"^/r/([A-Za-z0-9_]+)/?$")
# Inbox comment types -> ThreadBNC's kinds.
_INBOX_KINDS = {"comment_reply": "reply", "post_reply": "reply", "username_mention": "mention"}


class RedditReader(Protocol):
    def get(self, path: str, params: dict[str, Any] | None = None) -> Any: ...

    def post(self, path: str, form: dict[str, Any]) -> Any: ...

    def resolve_share(self, path: str) -> str | None: ...


def _ts(epoch: Any) -> str | None:
    if isinstance(epoch, bool) or not isinstance(epoch, (int, float)):
        return None  # Reddit's `edited` is false when never edited
    return fmt_ts(datetime.fromtimestamp(epoch, timezone.utc))


def _abs(url: str | None) -> str | None:
    if url and url.startswith("/"):
        return WWW + url  # crossposts link to the original by path
    return url or None


_MEDIA_ID = re.compile(r"^[A-Za-z0-9]+$")


def gallery(d: dict[str, Any]) -> list[str]:
    """A gallery post's images in order, as their stable i.redd.it URLs (the
    preview URLs Reddit hands out are signed and change)."""
    src = d if d.get("gallery_data") else next(iter(d.get("crosspost_parent_list") or []), {})
    meta = src.get("media_metadata") or {}
    out = []
    for item in (src.get("gallery_data") or {}).get("items") or []:
        mid = str(item.get("media_id") or "")
        m = meta.get(mid) or {}
        if not _MEDIA_ID.match(mid) or m.get("status") != "valid":
            continue
        kind = "gif" if m.get("e") == "AnimatedImage" else (m.get("m") or "").partition("/")[2]
        if kind in ("jpg", "jpeg", "png", "gif", "webp"):
            out.append(f"https://i.redd.it/{mid}.{kind}")
    return out


def actor(name: str | None) -> NActor:
    name = name or "[deleted]"
    return NActor(f"{WWW}/user/{name}", name, REDDIT_DOMAIN)


def subreddit_ap_id(name: str) -> str:
    return f"{WWW}/r/{name}"


class RedditAdapter(ThreadiverseAdapter):
    software = "reddit"
    # Conservative on purpose: Reddit allows about 100 requests a minute per
    # app, shared by everything this ThreadBNC does. The bouncer reads these.
    poll_page_size = 25
    max_poll_pages = 2
    comment_limit = 200  # comments per fetch; the rest of a big thread isn't expanded
    comment_depth = 8

    def __init__(self, reader: RedditReader):
        super().__init__(REDDIT_DOMAIN)
        self.reader = reader
        self._after: dict[tuple[str, str, int, int], str | None] = {}  # listing cursors, by page

    # -- normalizers ----------------------------------------------------------
    def _post(self, d: dict[str, Any]) -> NPost:
        sub, pid = d.get("subreddit") or "?", d["id"]
        removed_by = d.get("removed_by_category")
        body = d.get("selftext") or None
        preview = ((d.get("preview") or {}).get("images") or [{}])[0].get("source") or {}
        thumb = preview.get("url") or d.get("thumbnail")
        meta = {"nsfw": bool(d.get("over_18")), "spoiler": bool(d.get("spoiler"))}
        return NPost(
            ap_id=f"{WWW}/r/{sub}/comments/{pid}/",
            local_id=pid,
            title=d.get("title") or "",
            body=body,
            url=None if d.get("is_self") else _abs(d.get("url_overridden_by_dest") or d.get("url")),
            created_at=_ts(d.get("created_utc")),
            updated_at=_ts(d.get("edited")),
            deleted=removed_by in _BY_AUTHOR or body == "[deleted]",
            removed=bool(removed_by) and removed_by not in _BY_AUTHOR,
            locked=bool(d.get("locked")),
            community=NCommunity(ap_id=subreddit_ap_id(sub), name=sub, domain=REDDIT_DOMAIN,
                                 local_id=d.get("subreddit_id")),
            author=actor(d.get("author")),
            metadata={k: v for k, v in meta.items() if v},
            score=d.get("score"),
            comment_count=d.get("num_comments"),
            thumbnail_url=thumb if (thumb or "").startswith(("http://", "https://")) else None,
            featured=bool(d.get("stickied")),
            gallery=gallery(d),
        )

    def _comment(self, d: dict[str, Any], sub: str, pid: str) -> NComment:
        parent = d.get("parent_id") or ""
        body = d.get("body")
        meta = {"distinguished": d.get("distinguished")}
        return NComment(
            ap_id=f"{WWW}/r/{sub}/comments/{pid}/_/{d['id']}/",
            local_id=d["id"],
            parent_local_id=parent[3:] if parent.startswith("t1_") else None,
            body=body,
            created_at=_ts(d.get("created_utc")),
            updated_at=_ts(d.get("edited")),
            deleted=body == "[deleted]",
            removed=body == "[removed]" or bool(d.get("removed")),
            author=actor(d.get("author")),
            metadata={k: v for k, v in meta.items() if v},
            score=d.get("score"),
        )

    # -- interface ------------------------------------------------------------
    def resolve_url(self, ref: ThreadRef) -> str:
        if ref.kind == "share":
            target = self.reader.resolve_share(ref.local_id)
            if not target:
                raise RemoteNotFound(f"Reddit share link {ref.local_id} didn't lead to a post")
            ref = parse_thread_url(target)
        return ref.local_id

    def resolve_ap_id(self, ap_id: str) -> str | None:
        try:
            ref = parse_thread_url(ap_id)
        except ValueError:
            return None
        return ref.local_id if ref.kind == "post" and is_reddit_host(ref.domain) else None

    def fetch_post(self, local_id: str) -> NPost:
        data = self.reader.get("/api/info", {"id": f"t3_{local_id}"}) or {}
        children = (data.get("data") or {}).get("children") or []
        if not children:
            raise RemoteNotFound(f"reddit post {local_id} not found")
        return self._post(children[0]["data"])

    def fetch_comments(self, post_local_id: str) -> CommentList:
        """The newest comments of a thread, up to comment_limit. Reddit leaves
        the rest behind "load more" stubs, which aren't followed (each costs a
        request); the result is then marked incomplete."""
        data = self.reader.get(f"/comments/{post_local_id}", {
            "limit": self.comment_limit, "depth": self.comment_depth, "sort": "new"})
        out = CommentList()
        if not isinstance(data, list) or len(data) < 2:
            return out
        posts = (data[0].get("data") or {}).get("children") or []
        sub = posts[0]["data"].get("subreddit", "?") if posts else "?"
        stack = list(reversed((data[1].get("data") or {}).get("children") or []))
        while stack:
            child = stack.pop()
            d = child.get("data") or {}
            if child.get("kind") == "more":
                out.complete = False
            elif child.get("kind") == "t1":
                out.append(self._comment(d, sub, post_local_id))
                replies = d.get("replies")
                if isinstance(replies, dict):
                    stack.extend(reversed((replies.get("data") or {}).get("children") or []))
        return out

    def fetch_community(self, ref: CommunityRef) -> NCommunity:
        data = self.reader.get(f"/r/{ref.name}/about") or {}
        d = data.get("data") or {}
        if data.get("kind") != "t5" or not d.get("display_name"):
            raise RemoteNotFound(f"r/{ref.name} doesn't exist")
        return NCommunity(ap_id=subreddit_ap_id(d["display_name"]), name=d["display_name"], domain=REDDIT_DOMAIN,
                          title=d.get("title"), local_id=d.get("name"),
                          description=d.get("public_description") or None)

    def list_community_posts(self, ref: CommunityRef, sort: str = "New", page: int = 1,
                             limit: int = 20) -> list[NPost]:
        """Reddit pages with cursors, not numbers; the cursor for each page is
        remembered, and a page with no known cursor walks there from page 1."""
        listing, extra = SORTS.get(sort, SORTS["New"])
        key = (ref.name.lower(), sort, limit)
        after = None
        if page > 1:
            if (*key, page) not in self._after:
                self.list_community_posts(ref, sort, page - 1, limit)
            after = self._after.get((*key, page))
            if after is None:
                return []  # the listing ended before this page
        data = self.reader.get(f"/r/{ref.name}/{listing}", {"limit": limit, "after": after, **extra}) or {}
        body = data.get("data") or {}
        if len(self._after) > 1000:
            self._after.clear()
        self._after[(*key, page + 1)] = body.get("after")
        return [self._post(c["data"]) for c in body.get("children") or [] if c.get("kind") == "t3"]

    def subscriptions(self) -> list[NCommunity]:
        """Subreddits the connected Reddit account is subscribed to."""
        out: list[NCommunity] = []
        after = None
        for _ in range(10):
            data = self.reader.get("/subreddits/mine/subscriber", {"limit": 100, "after": after}) or {}
            body = data.get("data") or {}
            for c in body.get("children") or []:
                d = c.get("data") or {}
                if d.get("display_name") and d.get("subreddit_type") != "user":
                    out.append(NCommunity(ap_id=subreddit_ap_id(d["display_name"]), name=d["display_name"],
                                          domain=REDDIT_DOMAIN, title=d.get("title"), local_id=d.get("name")))
            after = body.get("after")
            if not after:
                break
        return sorted(out, key=lambda c: c.name.lower())

    # -- acting as the connected Reddit account -----------------------------------------
    @staticmethod
    def _thing(data: Any, kind: str) -> dict[str, Any]:
        things = (((data or {}).get("json") or {}).get("data") or {}).get("things") or []
        found = next((t.get("data") or {} for t in things if t.get("kind") == kind), None)
        if not found:
            raise RemoteRejected("Reddit didn't send back what it did", "no_result")
        return found

    def resolve_as(self, token: str, ap_id: str) -> dict[str, str]:
        """Reddit ids are in the permalinks themselves; nothing to look up."""
        parsed = urlparse(ap_id)
        if not is_reddit_host(parsed.hostname):
            return {}
        m = _THING_PATH.match(parsed.path or "")
        if m:
            out = {"post": m.group(1).lower()}
            if m.group(2):
                out["comment"] = m.group(2).lower()
            return out
        m = _SUB_PATH.match(parsed.path or "")
        return {"community": m.group(1)} if m else {}

    def vote_post(self, token: str, post_id: str, score: int) -> None:
        self.reader.post("/api/vote", {"id": f"t3_{post_id}", "dir": score})

    def vote_comment(self, token: str, comment_id: str, score: int) -> None:
        self.reader.post("/api/vote", {"id": f"t1_{comment_id}", "dir": score})

    def create_comment(self, token: str, post_id: str, body: str, parent_id: str | None = None) -> NComment:
        data = self.reader.post("/api/comment", {"thing_id": f"t1_{parent_id}" if parent_id else f"t3_{post_id}",
                                                 "text": body})
        d = self._thing(data, "t1")
        return self._comment(d, d.get("subreddit") or "?", post_id)

    def edit_comment(self, token: str, comment_id: str, body: str) -> NComment:
        d = self._thing(self.reader.post("/api/editusertext", {"thing_id": f"t1_{comment_id}", "text": body}), "t1")
        return self._comment(d, d.get("subreddit") or "?", (d.get("link_id") or "t3_?")[3:])

    def delete_comment(self, token: str, comment_id: str, deleted: bool = True) -> None:
        if not deleted:
            raise RemoteRejected("Reddit can't bring back a deleted comment.", "cant_restore")
        self.reader.post("/api/del", {"id": f"t1_{comment_id}"})

    def create_post(self, token: str, community_id: str, title: str, body: str | None = None,
                    url: str | None = None) -> NPost:
        """`community_id` is the subreddit's name. A post with both a link and
        text is submitted as a link; subreddits that allow text on link posts
        keep the text too."""
        form: dict[str, Any] = {"sr": community_id, "title": title, "resubmit": "true", "sendreplies": "true"}
        if url:
            form.update(kind="link", url=url, text=body or None)
        else:
            form.update(kind="self", text=body or "")
        data = self.reader.post("/api/submit", form) or {}
        made = (data.get("json") or {}).get("data") or {}
        pid = made.get("id") or (made.get("name") or "")[3:]
        if not pid:
            raise RemoteRejected("Reddit didn't say what it created", "no_result")
        return self.fetch_post(pid)

    def edit_post(self, token: str, post_id: str, title: str, body: str | None = None,
                  url: str | None = None) -> NPost:
        current = self.fetch_post(post_id)
        if title != current.title or (url or None) != current.url:
            raise RemoteRejected("Reddit doesn't allow changing a post's title or link, only its text.",
                                 "cant_edit_title")
        data = self.reader.post("/api/editusertext", {"thing_id": f"t3_{post_id}", "text": body or ""})
        return self._post(self._thing(data, "t3"))

    def delete_post(self, token: str, post_id: str, deleted: bool = True) -> None:
        if not deleted:
            raise RemoteRejected("Reddit can't bring back a deleted post.", "cant_restore")
        self.reader.post("/api/del", {"id": f"t3_{post_id}"})

    # -- the connected account's inbox ----------------------------------------------------
    def _inbox_item(self, child: dict[str, Any]) -> NInboxItem | None:
        d = child.get("data") or {}
        if not d.get("name"):
            return None
        common = {"remote_id": d["name"], "unread": bool(d.get("new")), "body": d.get("body"),
                  "created_at": _ts(d.get("created_utc")), "deleted": d.get("body") in ("[deleted]", "[removed]")}
        if child.get("kind") == "t4":  # a private message (or one from a subreddit's moderators)
            return NInboxItem(kind="message", author=actor(d.get("author") or f"r/{d.get('subreddit') or '?'}"),
                              object_type="message", object_ap_id=f"{WWW}/message/messages/{d.get('id')}",
                              object_local_id=d["name"], subject=d.get("subject"), **common)
        if child.get("kind") != "t1":
            return None
        m = _THING_PATH.match(urlparse(d.get("context") or "").path)
        if not m or not m.group(2):
            return None
        sub, pid, cid = d.get("subreddit") or "?", m.group(1).lower(), m.group(2).lower()
        return NInboxItem(kind=_INBOX_KINDS.get(d.get("type") or "", "reply"), author=actor(d.get("author")),
                          object_type="comment", object_ap_id=f"{WWW}/r/{sub}/comments/{pid}/_/{cid}/",
                          object_local_id=cid, post_ap_id=f"{WWW}/r/{sub}/comments/{pid}/", post_local_id=pid,
                          post_title=d.get("link_title"),
                          community=NCommunity(ap_id=subreddit_ap_id(sub), name=sub, domain=REDDIT_DOMAIN), **common)

    def inbox(self, token: str, me_ap_id: str, limit: int = 50) -> list[NInboxItem]:
        """Comment replies, username mentions and messages: one request."""
        data = self.reader.get("/message/inbox", {"limit": limit}) or {}
        items = (self._inbox_item(c) for c in (data.get("data") or {}).get("children") or [])
        return [i for i in items if i]

    def mark_inbox_read(self, token: str, kind: str, remote_id: str, read: bool = True) -> None:
        self.reader.post("/api/read_message" if read else "/api/unread_message", {"id": remote_id})

    def mark_all_inbox_read(self, token: str, items: list[tuple[str, str]]) -> None:
        ids = [remote_id for _kind, remote_id in items]
        for start in range(0, len(ids), 100):  # Reddit takes a comma-separated list
            self.reader.post("/api/read_message", {"id": ",".join(ids[start:start + 100])})

    def send_message(self, token: str, recipient_local_id: str, body: str, in_reply_to: str | None = None) -> None:
        """Replies to a message, in its conversation."""
        if not in_reply_to:
            raise RemoteRejected("Only replies to Reddit messages are supported.", "no_thread")
        self.reader.post("/api/comment", {"thing_id": in_reply_to, "text": body})
