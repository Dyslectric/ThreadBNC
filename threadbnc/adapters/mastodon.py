"""Acting as your Mastodon account: posting, and liking and replying to fediverse posts.

Posts from hashtags (adapters/activitypub.py) are read with ThreadBNC's own
actor, which can't like or reply as you. Signed in to a Mastodon account (or
GoToSocial, Akkoma, Pleroma: anything with Mastodon's client API), you can
like, unlike and reply to them and to their replies, delete your own
replies, post on your account and send private messages, all through your
account's own server.

Signing in is OAuth, as any Mastodon app does it: ThreadBNC registers itself
as an app on your server (once per server and return address), you approve it
on your server's page, and your server sends you back with a code that's
swapped for an access token (accounts.py keeps the flow; this has its calls).
The "token" kept for the account is JSON: that access token, and the app's
client id and secret, which ending the session (revoking it) needs.

A post or reply is found on your server by its ActivityPub id, through search
with resolve=true, which fetches it over federation if your server hasn't seen
it. Its local id is your server's status id. A reply mentions who it answers,
as Mastodon's own app does: without a mention they're never told of it.
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlencode

from .activitypub import account_avatar, status_post, with_quote
from .base import (NActor, NComment, NInboxItem, NPost, RemoteNotFound, RemoteRejected, ThreadiverseAdapter,
                   UnsupportedSoftware, host_of)
from .rss import html_to_markdown

SCOPES = "read write"
APP_NAME = "ThreadBNC"
# Mastodon's order of visibilities, most public first. A reply is never more public than what it answers.
VISIBILITY = ["public", "unlisted", "private", "direct"]


class MastodonAdapter(ThreadiverseAdapter):
    software = "mastodon"

    def __init__(self, domain: str, http: Any):
        super().__init__(domain)
        self.http = http

    def _call(self, method: str, path: str, session: str | None = None, **body: Any) -> Any:
        """One API call, as the account whose `session` (the kept token) it is, or as nobody."""
        access = json.loads(session)["access"] if session else None
        if method == "GET":
            return self.http.request_json("GET", self.domain, path, params=body or None, token=access,
                                          throttle=False)
        return self.http.request_json(method, self.domain, path, json=body, token=access, throttle=False)

    # -- reading: never done here. Posts from the fediverse come through the tag adapter.
    def _not_read_here(self, *_a: Any, **_k: Any) -> Any:
        raise UnsupportedSoftware("A Mastodon account is only acted as; its server isn't read from")

    resolve_url = resolve_ap_id = fetch_post = fetch_comments = _not_read_here
    fetch_community = list_community_posts = _not_read_here

    # -- signing in ------------------------------------------------------------------
    def check_server(self) -> None:
        """Raise RemoteNotFound unless the server has Mastodon's client API."""
        info = self.http.get_json(self.domain, "/api/v1/instance")
        if not isinstance(info, dict) or not (info.get("uri") or info.get("domain")):
            raise RemoteNotFound(f"{self.domain} doesn't have Mastodon's API")

    def register_app(self, redirect_uri: str) -> tuple[str, str]:
        got = self._call("POST", "/api/v1/apps", client_name=APP_NAME, redirect_uris=redirect_uri, scopes=SCOPES)
        return str(got["client_id"]), str(got["client_secret"])

    def authorize_url(self, client_id: str, redirect_uri: str, state: str, challenge: str) -> str:
        return f"https://{self.domain}/oauth/authorize?" + urlencode({
            "response_type": "code", "client_id": client_id, "redirect_uri": redirect_uri, "scope": SCOPES,
            "state": state, "code_challenge": challenge, "code_challenge_method": "S256"})

    def exchange_code(self, client_id: str, client_secret: str, code: str, redirect_uri: str,
                      verifier: str) -> str:
        """The code the server sent you back with, swapped for the session kept for the account."""
        got = self._call("POST", "/oauth/token", grant_type="authorization_code", code=code, client_id=client_id,
                         client_secret=client_secret, redirect_uri=redirect_uri, scope=SCOPES,
                         code_verifier=verifier)
        return json.dumps({"access": got["access_token"], "client_id": client_id, "client_secret": client_secret})

    def whoami(self, token: str) -> NActor:
        me = self._call("GET", "/api/v1/accounts/verify_credentials", token)
        username = str(me.get("username") or me.get("acct") or "?")
        ap_id = me.get("uri") or f"https://{self.domain}/users/{username}"  # older servers leave out `uri`
        return NActor(ap_id, username, self.domain, me.get("display_name") or None)

    def my_roles(self, token: str) -> dict[str, bool]:
        return {}

    def logout(self, token: str) -> None:
        s = json.loads(token)
        self._call("POST", "/oauth/revoke", client_id=s["client_id"], client_secret=s["client_secret"],
                   token=s["access"])

    # -- finding things ---------------------------------------------------------------
    def resolve_as(self, token: str, ap_id: str) -> dict[str, str]:
        if not ap_id.startswith("https://"):
            return {}
        try:
            got = self._call("GET", "/api/v2/search", token, q=ap_id, type="statuses", resolve="true", limit=1)
        except RemoteNotFound:
            return {}
        status = next(iter(got.get("statuses") or []), None) if isinstance(got, dict) else None
        if not status or not status.get("id"):
            return {}
        return {"post": str(status["id"]), "comment": str(status["id"])}

    def _comment(self, s: dict[str, Any], parent: str | None) -> NComment:
        account = s.get("account") or {}
        uri = account.get("uri") or account.get("url") or ""
        body = html_to_markdown(s.get("content"), s.get("uri"))
        if s.get("spoiler_text"):
            body = f"**CW: {s['spoiler_text']}**\n\n{body}"
        body = with_quote(body, s) or ""
        return NComment(ap_id=s["uri"], local_id=str(s["id"]), parent_local_id=parent, body=body or None,
                        created_at=s.get("created_at"), updated_at=s.get("edited_at"), deleted=False, removed=False,
                        author=NActor(uri, account.get("username") or "?", host_of(uri) or self.domain,
                                      account.get("display_name") or None, account_avatar(account)),
                        score=s.get("favourites_count") or 0, upvotes=s.get("favourites_count") or 0,
                        reply_count=s.get("replies_count") or 0)

    # -- inbox ----------------------------------------------------------------------
    def inbox(self, token: str, me_ap_id: str) -> list[NInboxItem]:
        """Recent mentions, replies and direct statuses addressed to this account."""
        me = self._call("GET", "/api/v1/accounts/verify_credentials", token)
        notifications = self._call("GET", "/api/v1/notifications", token, limit=50)
        try:
            marker = self._call("GET", "/api/v1/markers", token, **{"timeline[]": "notifications"})
        except RemoteNotFound:  # some servers with Mastodon's client API have no markers
            marker = {}
        last_read = str((marker.get("notifications") or {}).get("last_read_id") or "0")
        out = []
        for n in notifications:
            if n.get("type") != "mention" or not isinstance(n.get("status"), dict):
                continue
            s = n["status"]
            actor = n.get("account") or s.get("account") or {}
            uri = actor.get("uri") or actor.get("url") or ""
            sid, nid = str(s.get("id") or ""), str(n.get("id") or "")
            if not sid or not nid or not uri:
                continue
            direct = s.get("visibility") == "direct"
            reply = str(s.get("in_reply_to_account_id") or "") == str(me.get("id") or "")
            kind = "message" if direct else "reply" if reply else "mention"
            body = html_to_markdown(s.get("content"), s.get("uri"))
            if s.get("spoiler_text"):
                body = f"**CW: {s['spoiler_text']}**\n\n{body}"
            body = with_quote(body, s) or ""
            ap_id = s.get("uri") or s.get("url")
            out.append(NInboxItem(
                kind=kind, remote_id=nid, unread=int(nid) > int(last_read),
                author=NActor(uri, actor.get("username") or "?", host_of(uri) or self.domain,
                              actor.get("display_name") or None, account_avatar(actor)),
                body=body, created_at=n.get("created_at") or s.get("created_at"),
                object_type="message" if direct else "post", object_ap_id=ap_id,
                object_local_id=sid, author_local_id=str(actor.get("id") or ""),
                post_ap_id=None if direct else ap_id, post_local_id=sid))
        return out

    def mark_inbox_read(self, token: str, kind: str, remote_id: str, read: bool = True) -> None:
        """Mastodon has a timeline marker, but no per-notification read switch."""

    def mark_all_inbox_read(self, token: str, pairs: list[tuple[str, str]]) -> None:
        if pairs:
            newest = max(int(remote_id) for _, remote_id in pairs)
            try:
                marker = self._call("GET", "/api/v1/markers", token, **{"timeline[]": "notifications"})
                current = int((marker.get("notifications") or {}).get("last_read_id") or 0)
                self._call("POST", "/api/v1/markers", token,
                           notifications={"last_read_id": str(max(newest, current))})
            except RemoteNotFound:
                pass  # the inbox still marks its locally held items read

    # -- writing ---------------------------------------------------------------------
    def create_comment(self, token: str, post_local_id: str, body: str,
                       parent_local_id: str | None = None) -> NComment:
        """Reply to a post, or to a reply to it: mentioning whoever it answers
        (and whoever they mentioned), with its content warning, and no more
        public than it is, as Mastodon's own app does."""
        to = parent_local_id or post_local_id
        parent = self._call("GET", f"/api/v1/statuses/{to}", token)
        me = self._call("GET", "/api/v1/accounts/verify_credentials", token)
        mention = [(parent.get("account") or {}).get("acct")] + [m.get("acct") for m in parent.get("mentions") or []]
        said = body.lower()
        prefix = []
        for acct in dict.fromkeys(a for a in mention if a):
            if acct != me.get("acct") and not re.search(rf"(?<![\w@])@{re.escape(acct.lower())}(?![\w@.-])", said):
                prefix.append(f"@{acct}")
        text = " ".join(prefix + [body]) if prefix else body
        seen = parent.get("visibility")
        visibility = seen if seen in VISIBILITY[1:] else None  # else the account's own default
        s = self._call("POST", "/api/v1/statuses", token, status=text, in_reply_to_id=to, visibility=visibility,
                       spoiler_text=parent.get("spoiler_text") or None, sensitive=bool(parent.get("sensitive")) or None)
        return self._comment(s, parent_local_id)

    def message_person(self, token: str, to: str, body: str, subject: str | None = None) -> str:
        """A private message, as Mastodon has them: a post only the people it
        mentions can see ("direct"), mentioning who it's for. `to` is their
        profile's address or @name@server, found over federation if your
        server hasn't seen them. There's no subject."""
        q = to.strip()
        if not q.startswith(("http://", "https://")):
            q = "@" + q.lstrip("@")
        try:
            got = self._call("GET", "/api/v2/search", token, q=q, type="accounts", resolve="true", limit=1)
        except RemoteNotFound:
            got = {}
        found = next(iter(got.get("accounts") or []), None) if isinstance(got, dict) else None
        if not found or not found.get("acct"):
            raise RemoteNotFound(f"{self.domain} couldn't find {to.strip()}")
        acct = str(found["acct"])
        mentioned = re.search(rf"(?<![\w@])@{re.escape(acct.lower())}(?![\w@.-])", body.lower())
        self._call("POST", "/api/v1/statuses", token, status=body if mentioned else f"@{acct} {body}",
                   visibility="direct")
        return f"@{acct}" if "@" in acct else f"@{acct}@{self.domain}"

    def create_post(self, token: str, community_local_id: str, title: str, body: str | None,
                    url: str | None) -> NPost:
        """A new post on your account, as public as your account posts by
        default. Mastodon posts have no title: the title, the text and the link
        go together, and the server makes the link a card."""
        text = "\n\n".join(x.strip() for x in (title, body or "") if x and x.strip())
        if url and url not in text:
            text += f"\n\n{url}"
        s = self._call("POST", "/api/v1/statuses", token, status=text)
        return status_post(s, (s.get("account") or {}).get("uri") or community_local_id)

    def _favourite(self, token: str, local_id: str, score: int) -> None:
        if score == -1:
            raise RemoteRejected("Mastodon has likes (favourites), not downvotes.", "no_downvotes")
        self._call("POST", f"/api/v1/statuses/{local_id}/{'favourite' if score == 1 else 'unfavourite'}", token)

    def vote_post(self, token: str, local_id: str, score: int) -> None:
        self._favourite(token, local_id, score)

    def reblog(self, token: str, local_id: str, on: bool = True) -> None:
        """Boost a post to your followers, or undo it."""
        self._call("POST", f"/api/v1/statuses/{local_id}/{'reblog' if on else 'unreblog'}", token)

    def vote_comment(self, token: str, local_id: str, score: int) -> None:
        self._favourite(token, local_id, score)

    def _delete(self, token: str, local_id: str, deleted: bool) -> None:
        if not deleted:
            raise RemoteRejected("Mastodon can't bring back a deleted post.", "cant_undelete")
        self._call("DELETE", f"/api/v1/statuses/{local_id}", token)

    def delete_post(self, token: str, local_id: str, deleted: bool = True) -> None:
        self._delete(token, local_id, deleted)

    def delete_comment(self, token: str, local_id: str, deleted: bool = True) -> None:
        self._delete(token, local_id, deleted)
