"""Loops accounts (short videos on the fediverse), followed like communities:
read through the public API Loops's own web pages use, which needs no account.

An account is its ActivityPub address, https://<server>/ap/users/<id>, and is
checked on a schedule: one request lists its latest ten videos, with their
likes and comment counts. A video's post links to its video file, which
plays here and is saved like any other video (when you open or keep it); its
thumbnail is its picture. A post's local id is the video's id; its address
is /v/<shortcode>, and its ActivityPub id .../ap/users/<id>/video/<id>.
Opening a post reads its comments, with the first replies to each. Liking and
replying are done as your Mastodon account (accounts.py).
"""

from __future__ import annotations

import html as htmllib
import re
from typing import Any
from urllib.parse import urlparse

from .. import languages
from .activitypub import title_from
from .base import (
    CommentList,
    CommunityRef,
    NActor,
    NComment,
    NCommunity,
    NPost,
    RemoteNotFound,
    RemoteUnavailable,
    ThreadiverseAdapter,
    ThreadRef,
    host_of,
)
from .rss import html_to_markdown

COMMENT_PAGES = 3  # of 10: the newest 30 comments
REPLIED_COMMENTS = 10  # comments whose replies are read too
REPLY_PAGES = 2  # of 3 replies each
_VIDEO_AP = re.compile(r"^/ap/users/\d+/video/(\d+)/?$")
_TAG = re.compile(r"(?<![\w#])#(\w+)")


def caption_markdown(text: str | None, base: str) -> str:
    """A caption (plain text, with #tags and @mentions) as Markdown: its
    special characters escaped, its lines kept, its hashtags linked."""
    html = htmllib.escape(text or "").replace("\n", "<br>")
    html = _TAG.sub(lambda m: f'<a href="https://{base}/tag/{m.group(1)}">#{m.group(1)}</a>', html)
    return html_to_markdown(html, f"https://{base}/")


class LoopsAdapter(ThreadiverseAdapter):
    software = "loops"
    poll_page_size = 10  # what Loops gives, whatever's asked
    max_poll_pages = 3

    def __init__(self, domain: str, http: Any):
        super().__init__(domain)
        self.http = http
        self._ids: dict[str, str] = {}  # username -> the account's id
        self._cursors: dict[str, str] = {}  # username -> where the page just read carries on

    # -- accounts ------------------------------------------------------------------
    def actor(self, account_id: str) -> str:
        return f"https://{self.domain}/ap/users/{account_id}"

    def _account(self, username: str) -> dict[str, Any]:
        if "@" in username:
            raise RemoteNotFound(f"@{username} isn't on {self.domain}: follow it on its own server")
        if username.isdigit():  # its ActivityPub address's id (/ap/users/<id>): that says its name
            resp = self.http.send("GET", self.actor(username), headers={"Accept": "application/activity+json"})
            try:
                name = resp.json().get("preferredUsername") if resp.status_code == 200 else None
            except (ValueError, AttributeError):
                name = None
            if not isinstance(name, str) or not name or name.isdigit():
                raise RemoteNotFound(f"No account {self.actor(username)}")
            username = name
        got = self.http.get_json(self.domain, f"/api/v1/account/username/{username}")
        account = got.get("data") if isinstance(got, dict) else None
        if not isinstance(account, dict) or not account.get("id"):
            raise RemoteNotFound(f"No account @{username} on {self.domain}")
        if account.get("local") is False or account.get("remote_url"):
            raise RemoteNotFound(f"@{username} isn't on {self.domain}: follow it on its own server")
        self._ids[username.lower()] = str(account["id"])
        return account

    def _id_of(self, username: str) -> str:
        return self._ids.get(username.lower()) or str(self._account(username)["id"])

    def fetch_community(self, ref: CommunityRef) -> NCommunity:
        a = self._account(ref.name)
        return NCommunity(ap_id=self.actor(str(a["id"])), name=str(a.get("username") or ref.name), domain=self.domain,
                          title=a.get("name") or None, local_id=str(a["id"]),
                          description=caption_markdown(a.get("bio"), self.domain) or None)

    def _person(self, account: dict[str, Any]) -> NActor:
        username = str(account.get("username") or "?")
        if "@" in username:  # someone from elsewhere on the fediverse: name@server
            name, _, server = username.partition("@")
            return NActor(f"https://{server}/@{name}", name, server, account.get("name") or None,
                          account.get("avatar") or "")
        return NActor(self.actor(str(account.get("id") or "")), username, self.domain,
                      account.get("display_name") or account.get("name") or None, account.get("avatar") or "")

    # -- posts ---------------------------------------------------------------------
    def _post(self, v: dict[str, Any]) -> NPost:
        account = v.get("account") or {}
        media = v.get("media") or {}
        caption = (v.get("caption") or "").strip()
        meta: dict[str, Any] = {} if caption else {"untitled": True}
        if v.get("is_sensitive"):
            meta["nsfw"] = True
        if media.get("alt_text"):
            meta["alt_text"] = media["alt_text"]
        likes = v.get("likes") if isinstance(v.get("likes"), int) else None
        return NPost(
            ap_id=f"{self.actor(str(account.get('id') or ''))}/video/{v['id']}",
            local_id=str(v["id"]),
            title=title_from(caption) if caption else "Untitled loop",
            body=caption_markdown(caption, self.domain) or None,
            url=media.get("src_url") or media.get("hls_url") or v.get("url"),
            created_at=v.get("created_at"),
            updated_at=None,
            deleted=False, removed=False, locked=not (v.get("permissions") or {}).get("can_comment", True),
            community=NCommunity(ap_id=self.actor(str(account.get("id") or "")), name=str(account.get("username") or ""),
                                 domain=self.domain, title=account.get("name") or None,
                                 local_id=str(account.get("id") or "") or None),
            author=self._person(account),
            metadata=meta,
            score=likes, upvotes=likes,
            comment_count=v.get("comments") if isinstance(v.get("comments"), int) else None,
            thumbnail_url=media.get("thumbnail") or None,
            featured=bool(v.get("pinned")),
            language=languages.normalize(v.get("lang")),
        )

    def list_community_posts(self, ref: CommunityRef, sort: str = "New", page: int = 1,
                             limit: int = 20) -> list[NPost]:
        """The account's latest videos (its pinned ones first), ten to a page;
        a later page carries on from where the one before ended."""
        params: dict[str, Any] = {}
        if page > 1:
            if ref.name.lower() not in self._cursors:
                return []
            params["cursor"] = self._cursors[ref.name.lower()]
        got = self.http.get_json(self.domain, f"/api/v1/feed/account/{self._id_of(ref.name)}", params=params or None)
        videos = [v for v in (got.get("data") if isinstance(got, dict) else None) or []
                  if isinstance(v, dict) and v.get("id")]
        cursor = ((got.get("meta") or {}).get("next_cursor")) if isinstance(got, dict) else None
        if cursor:
            self._cursors[ref.name.lower()] = cursor
        else:
            self._cursors.pop(ref.name.lower(), None)
        return [self._post(v) for v in videos]

    def fetch_post(self, local_id: str) -> NPost:
        got = self.http.get_json(self.domain, f"/api/v1/video/{local_id}")
        v = got.get("data") if isinstance(got, dict) else None
        if not isinstance(v, dict) or not v.get("id"):
            raise RemoteNotFound(f"Video {local_id} isn't on {self.domain}")
        return self._post(v)

    def resolve_url(self, ref: ThreadRef) -> str:
        """A video's id from its page's shortcode: its ActivityPub object says it."""
        if ref.local_id.isdigit():
            return ref.local_id
        resp = self.http.send("GET", f"https://{self.domain}/v/{ref.local_id}",
                              headers={"Accept": "application/activity+json"})
        if resp.status_code in (404, 410):
            raise RemoteNotFound(f"https://{self.domain}/v/{ref.local_id}: HTTP {resp.status_code}")
        try:
            obj = resp.json()
        except ValueError:
            obj = None
        vid = self.resolve_ap_id(str(obj.get("id") or "")) if isinstance(obj, dict) else None
        if vid is None:
            raise RemoteUnavailable(f"https://{self.domain}/v/{ref.local_id} didn't say which video it is "
                                    f"(HTTP {resp.status_code})")
        return vid

    def resolve_ap_id(self, ap_id: str) -> str | None:
        if host_of(ap_id) != self.domain.split(":")[0]:
            return None
        m = _VIDEO_AP.match(urlparse(ap_id).path or "")
        return m.group(1) if m else None

    # -- comments ------------------------------------------------------------------
    def _comment(self, c: dict[str, Any], parent: str | None, kind: str) -> NComment:
        account = c.get("account") or {}
        author = self._person(account)
        ap_id = c.get("remote_url") or f"{self.actor(str(account.get('id') or ''))}/{kind}/{c['id']}"
        body = caption_markdown(c.get("caption"), self.domain)
        for m in c.get("media") or []:
            url = m.get("url") if isinstance(m, dict) else None
            if url:
                body += f"\n\n![]({url})"
        likes = c.get("likes") if isinstance(c.get("likes"), int) else None
        return NComment(ap_id=ap_id, local_id=str(c["id"]), parent_local_id=parent, body=body or None,
                        created_at=c.get("created_at"), updated_at=None, deleted=bool(c.get("tombstone")),
                        removed=False, author=author, score=likes, upvotes=likes,
                        reply_count=c.get("replies") if isinstance(c.get("replies"), int) else None)

    def _page(self, path: str, params: dict[str, Any]) -> tuple[list[dict[str, Any]], str | None]:
        got = self.http.get_json(self.domain, path, params=params or None)
        data = got.get("data") if isinstance(got, dict) else None
        cursor = ((got.get("meta") or {}).get("next_cursor")) if isinstance(got, dict) else None
        return [c for c in data or [] if isinstance(c, dict) and c.get("id")], cursor

    def fetch_comments(self, post_local_id: str) -> CommentList:
        """The video's newest comments (COMMENT_PAGES pages), and the replies
        to the first REPLIED_COMMENTS that have some (REPLY_PAGES pages each).
        Complete only when nothing was left unread."""
        out = CommentList()
        truncated = False
        top: list[dict[str, Any]] = []
        cursor: str | None = None
        for page in range(COMMENT_PAGES):
            batch, cursor = self._page(f"/api/v1/video/comments/{post_local_id}", {"cursor": cursor} if cursor else {})
            top += batch
            if not cursor:
                break
        truncated = bool(cursor)
        replied = [c for c in top if (c.get("replies") or 0) > 0]
        truncated = truncated or len(replied) > REPLIED_COMMENTS
        for c in top:
            out.append(self._comment(c, None, "comment"))
        for c in replied[:REPLIED_COMMENTS]:
            cursor = None
            for page in range(REPLY_PAGES):
                params: dict[str, Any] = {"cr": c["id"]}
                if cursor:
                    params["cursor"] = cursor
                batch, cursor = self._page(f"/api/v1/video/comments/{post_local_id}/replies", params)
                out.extend(self._comment(r, str(c["id"]), "reply") for r in batch)
                if not cursor:
                    break
            truncated = truncated or bool(cursor)
        out.complete = not truncated
        return out
