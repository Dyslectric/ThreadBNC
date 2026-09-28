"""Pixelfed accounts, followed like communities: their posts (pictures,
albums, videos) read through the public API Pixelfed's own web pages use,
which needs no account.

An account is its ActivityPub address, https://<server>/users/<name>, and is
checked on a schedule: one request lists its latest posts, with their likes
and comment counts, as Mastodon's API has them. Its reposts and replies stay
out. A post's local id is "<username>/<id>", as its address has it
(/p/<username>/<id>). Opening a post reads its comments from its server,
with the first replies to each. Liking and replying are done as your
Mastodon account (accounts.py), which finds the post by its address.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any
from urllib.parse import urlparse

from .activitypub import context_comments, status_post
from .base import (
    PIXELFED_POST,
    CommentList,
    CommunityRef,
    NCommunity,
    NPost,
    RemoteNotFound,
    ThreadiverseAdapter,
    ThreadRef,
    host_of,
)
from .rss import html_to_markdown

COMMENT_PAGES = 3  # of 10: the newest 30 comments
REPLIED_COMMENTS = 10  # comments whose replies are read too (their first 10)


def _real(s: Any) -> bool:
    """A post or comment that's there to read. Pixelfed still lists those of
    deleted accounts, with no author and "/404" for an address."""
    return (isinstance(s, dict) and bool(s.get("id")) and isinstance(s.get("account"), dict)
            and str(s.get("uri") or "").startswith("https://"))


class PixelfedAdapter(ThreadiverseAdapter):
    software = "pixelfed"
    poll_page_size = 20
    max_poll_pages = 3

    def __init__(self, domain: str, http: Any):
        super().__init__(domain)
        self.http = http
        self._ids: dict[str, str] = {}  # username -> the account's id on its server
        self._older: dict[str, str] = {}  # username -> the last post id on the page just read

    # -- accounts ------------------------------------------------------------------
    def _account(self, username: str) -> dict[str, Any]:
        if "@" in username:
            raise RemoteNotFound(f"@{username} isn't on {self.domain}: follow it on its own server")
        got = self.http.get_json(self.domain, "/api/v1/accounts/lookup", params={"acct": username})
        if not isinstance(got, dict) or not got.get("id"):
            raise RemoteNotFound(f"No account @{username} on {self.domain}")
        self._ids[username.lower()] = str(got["id"])
        return got

    def _id_of(self, username: str) -> str:
        return self._ids.get(username.lower()) or str(self._account(username)["id"])

    def actor(self, username: str) -> str:
        return f"https://{self.domain}/users/{username}"

    def _actor_of(self, account: dict[str, Any]) -> str:
        """Someone's ActivityPub address. Pixelfed's API gives their profile
        page: for its own people, their address is /users/<name>."""
        if account.get("local", True) and "@" not in str(account.get("acct") or ""):
            return self.actor(str(account.get("username") or ""))
        return str(account.get("uri") or account.get("url") or "")

    def _community(self, account: dict[str, Any]) -> NCommunity:
        username = str(account.get("username") or "")
        return NCommunity(ap_id=self.actor(username), name=username, domain=self.domain,
                          title=account.get("display_name") or None, local_id=str(account.get("id") or "") or None,
                          description=html_to_markdown(account.get("note"), self.actor(username)) or None)

    def fetch_community(self, ref: CommunityRef) -> NCommunity:
        account = self._account(ref.name)
        if account.get("locked"):
            raise RemoteNotFound(f"@{ref.name}@{self.domain} is private: only their followers see their posts")
        return self._community(account)

    # -- posts ---------------------------------------------------------------------
    def _post(self, s: dict[str, Any]) -> NPost:
        account = dict(s.get("account") or {})
        account["uri"] = self._actor_of(account)
        post = status_post({**s, "account": account, "replies_count": s.get("replies_count", s.get("reply_count"))}, "")
        username = str(account.get("username") or "")
        return replace(post, local_id=f"{username}/{s['id']}", community=self._community(account))

    def list_community_posts(self, ref: CommunityRef, sort: str = "New", page: int = 1,
                             limit: int = 20) -> list[NPost]:
        """The account's latest posts, newest first; a later page carries on
        from the last post of the one before."""
        params: dict[str, Any] = {"limit": min(limit, 40)}
        if page > 1:
            if ref.name.lower() not in self._older:
                return []
            params["max_id"] = self._older[ref.name.lower()]
        got = self.http.get_json(self.domain, f"/api/pixelfed/v1/accounts/{self._id_of(ref.name)}/statuses",
                                 params=params)
        statuses = [s for s in got if _real(s)] if isinstance(got, list) else []
        if statuses:
            self._older[ref.name.lower()] = str(statuses[-1]["id"])
        else:
            self._older.pop(ref.name.lower(), None)
        return [self._post(s) for s in statuses
                if not s.get("reblog") and not s.get("in_reply_to_id")
                and s.get("visibility", "public") in ("public", "unlisted")]

    def fetch_post(self, local_id: str) -> NPost:
        username, _, sid = local_id.partition("/")
        got = self.http.get_json(self.domain, f"/api/v2/profile/{username}/status/{sid}")
        s = got.get("status") if isinstance(got, dict) else None
        if not isinstance(s, dict) or not s.get("uri"):
            raise RemoteNotFound(f"https://{self.domain}/p/{local_id} isn't there")
        return self._post(s)

    def resolve_url(self, ref: ThreadRef) -> str:
        if ref.kind != "post" or "/" not in ref.local_id:
            raise RemoteNotFound("Not a Pixelfed post's address (expected https://server/p/name/123)")
        return ref.local_id

    def resolve_ap_id(self, ap_id: str) -> str | None:
        if host_of(ap_id) != self.domain.split(":")[0]:
            return None
        m = PIXELFED_POST.match(urlparse(ap_id).path or "")
        return f"{m.group(1)}/{m.group(2)}" if m else None

    # -- comments ------------------------------------------------------------------
    def _comments_of(self, account_id: str, status_id: str, page: int = 1) -> tuple[list[dict[str, Any]], bool]:
        """One page of comments on a post or comment, and whether there are more."""
        got = self.http.get_json(self.domain, f"/api/v2/comments/{account_id}/status/{status_id}",
                                 params={"page": page} if page > 1 else None)
        data = got.get("data") if isinstance(got, dict) else None
        pages = ((got.get("meta") or {}).get("pagination") or {}) if isinstance(got, dict) else {}
        more = isinstance(pages.get("total_pages"), int) and pages["total_pages"] > page
        return [c for c in data or [] if _real(c)], more

    def fetch_comments(self, post_local_id: str) -> CommentList:
        """The post's newest comments (COMMENT_PAGES pages) and the first
        replies to the first REPLIED_COMMENTS of them that have some. Complete
        only when nothing was left unread."""
        username, _, sid = post_local_id.partition("/")
        found: list[dict[str, Any]] = []
        truncated = False
        for page in range(1, COMMENT_PAGES + 1):
            batch, more = self._comments_of(self._id_of(username), sid, page)
            found += batch
            if not more:
                break
            truncated = truncated or page == COMMENT_PAGES
        replied = [c for c in found if (c.get("reply_count") or 0) > 0]
        truncated = truncated or len(replied) > REPLIED_COMMENTS
        for c in replied[:REPLIED_COMMENTS]:
            try:
                batch, more = self._comments_of(str((c.get("account") or {}).get("id") or ""), str(c["id"]))
            except RemoteNotFound:
                continue
            found += batch
            truncated = truncated or more or any((r.get("reply_count") or 0) > 0 for r in batch)
        for c in found:  # their authors' ActivityPub addresses, as _post gives them, and Mastodon's names
            c["account"] = {**(c.get("account") or {}), "uri": self._actor_of(c.get("account") or {})}
            c.setdefault("replies_count", c.get("reply_count"))
        out = context_comments({"descendants": found}, sid)
        out.complete = not truncated
        return out
