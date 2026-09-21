"""Lemmy 1.0 adapter (HTTP API v4).

Lemmy 1.0 still answers a slice of API v3 (reading, posting, voting, login), but
not the moderation and admin calls, so servers running 1.0 get this adapter.
The main differences from v3:

- counts live on the post/comment itself; timestamps end in ``_at``
- lists are ``{"items": [...], "next_page": cursor}`` and page by cursor
- sort and listing names are lowercase (``new``, ``all``)
- votes are ``is_upvote`` true/false (omitted to clear); deletes use DELETE
- ``resolve_object`` needs a URL and returns one flat view tagged with ``type_``
- the modlog is one combined list; a restore is the same kind with ``is_revert``
- private communities: join requests are approved or denied by moderators
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Iterator

from .base import (
    CommunityRef,
    ModAction,
    NActor,
    NComment,
    NCommunity,
    NPost,
    RemoteAuthError,
    RemoteError,
    RemoteNotFound,
    ThreadRef,
    host_of,
    parse_thread_url,
)
from .lemmy import COMMENT_PAGE_LIMIT, MAX_COMMENT_PAGES, LemmyAdapter, _ts

# Old (v3) sort names used around ThreadBNC -> (v4 sort, time range in seconds).
_DAY = 86400
POST_SORTS: dict[str, tuple[str, int | None]] = {
    "Hot": ("hot", None), "New": ("new", None), "Old": ("old", None), "Active": ("active", None),
    "Controversial": ("controversial", None), "Scaled": ("scaled", None),
    "MostComments": ("most_comments", None), "NewComments": ("new_comments", None),
    "TopHour": ("top", 3600), "TopSixHour": ("top", 6 * 3600), "TopTwelveHour": ("top", 12 * 3600),
    "TopDay": ("top", _DAY), "TopWeek": ("top", 7 * _DAY), "TopMonth": ("top", 30 * _DAY),
    "TopThreeMonths": ("top", 91 * _DAY), "TopSixMonths": ("top", 182 * _DAY),
    "TopNineMonths": ("top", 273 * _DAY), "TopYear": ("top", 365 * _DAY), "TopAll": ("top", None),
}
# v4 registration modes <-> the v3 names the admin page uses.
REGISTRATION_TO_V4 = {"Closed": "closed", "RequireApplication": "require_application", "Open": "open",
                      "RequireInvitation": "require_invitation"}
REGISTRATION_FROM_V4 = {v: k for k, v in REGISTRATION_TO_V4.items()}
MOD_KINDS = {"mod_remove_post": ("remove_post", "post"), "mod_lock_post": ("lock_post", "post"),
             "mod_remove_comment": ("remove_comment", "comment")}
MAX_CURSOR_PAGES = 50  # safety net for cursor walks (pending requests, blocklists)


@dataclass
class JoinRequest:
    """Someone asking to join a private community."""

    person_local_id: str
    person: NActor
    community_local_id: str
    community_ap_id: str
    state: str  # approval_required | accepted | denied | pending


def _version_tuple(version: str | None) -> tuple[int, int]:
    parts = (version or "").split("-")[0].split(".")
    try:
        return int(parts[0]), int(parts[1]) if len(parts) > 1 else 0
    except (ValueError, IndexError):
        return (0, 0)


def speaks_v4(version: str | None) -> bool:
    """Lemmy 1.0 (developed as 0.20) switched the API to v4."""
    return _version_tuple(version) >= (0, 20)


class Lemmy1Adapter(LemmyAdapter):
    software = "lemmy"
    api_base = "/api/v4"
    supports_private_communities = True

    # -- normalizers -------------------------------------------------------
    def _community(self, c: dict[str, Any], moderators: list[str] | None = None) -> NCommunity:
        community = super()._community(c, moderators)
        community.description = c.get("sidebar") or c.get("summary")
        community.visibility = c.get("visibility")
        return community

    def _post(self, pv: dict[str, Any]) -> NPost:
        p = pv["post"]
        meta = {"nsfw": bool(p.get("nsfw")), "language_id": p.get("language_id"), "alt_text": p.get("alt_text")}
        return NPost(
            ap_id=p.get("ap_id") or f"https://{self.domain}/post/{p['id']}",
            local_id=str(p["id"]),
            title=p.get("name") or "",
            body=p.get("body"),
            url=p.get("url"),
            created_at=_ts(p.get("published_at")),
            updated_at=_ts(p.get("updated_at")),
            deleted=bool(p.get("deleted")),
            removed=bool(p.get("removed")),
            locked=bool(p.get("locked")),
            community=self._community(pv.get("community") or {}),
            author=self._actor(pv.get("creator")),
            metadata={k: v for k, v in meta.items() if v not in (None, False, "")},
            score=p.get("score"),
            comment_count=p.get("comments"),
            thumbnail_url=p.get("thumbnail_url"),
            newest_comment_at=_ts(p.get("newest_comment_time_at")),
            featured=bool(p.get("featured_community") or p.get("featured_local")),
            upvotes=p.get("upvotes"),
            downvotes=p.get("downvotes"),
        )

    def _comment(self, cv: dict[str, Any]) -> NComment:
        c = cv["comment"]
        parts = [x for x in str(c.get("path") or "").split(".") if x]
        meta = {"distinguished": bool(c.get("distinguished")), "language_id": c.get("language_id")}
        return NComment(
            ap_id=c.get("ap_id") or f"https://{self.domain}/comment/{c['id']}",
            local_id=str(c["id"]),
            parent_local_id=parts[-2] if len(parts) >= 3 else None,  # path = "0.<ancestors>.<own id>"
            body=c.get("content"),
            created_at=_ts(c.get("published_at")),
            updated_at=_ts(c.get("updated_at")),
            deleted=bool(c.get("deleted")),
            removed=bool(c.get("removed")),
            author=self._actor(cv.get("creator")),
            metadata={k: v for k, v in meta.items() if v not in (None, False, "")},
            score=c.get("score"),
            reply_count=c.get("child_count"),
            upvotes=c.get("upvotes"),
            downvotes=c.get("downvotes"),
        )

    def _moderators(self, data: dict[str, Any]) -> list[NActor]:
        return [self._actor(m.get("moderator")) for m in data.get("moderators") or []]

    # -- low level -----------------------------------------------------------
    def _call(self, method: str, path: str, token: str | None, body: dict[str, Any] | None = None,
              **params: Any) -> Any:
        return super()._call(method, path, token or self._read_token, body, **params)

    def _pages(self, path: str, token: str | None = None, max_pages: int = MAX_CURSOR_PAGES,
               **params: Any) -> Iterator[list[dict[str, Any]]]:
        """Each page of a cursor-paged list."""
        cursor = None
        for _ in range(max_pages):
            data = self._call("GET", path, token, page_cursor=cursor, **params) or {}
            yield data.get("items") or []
            cursor = data.get("next_page")
            if not cursor:
                return

    def _resolve(self, token: str | None, q: str) -> dict[str, Any]:
        return self._call("GET", "/resolve_object", token, q=q) or {}

    def _actor_url(self, handle: str) -> str:
        """user@host -> the person's ActivityPub URL, via WebFinger. v4's
        resolve_object only accepts URLs."""
        name, _, host = handle.lstrip("@").partition("@")
        host = host or self.domain
        try:
            data = self.http.get_json(host, "/.well-known/webfinger", {"resource": f"acct:{name}@{host}"})
        except RemoteError:
            data = {}
        for link in (data or {}).get("links") or []:
            if link.get("rel") == "self" and "activity" in str(link.get("type", "")) and link.get("href"):
                return str(link["href"])
        return f"https://{host}/u/{name}"  # Lemmy and PieFed's layout

    # -- reading -------------------------------------------------------------
    def resolve_url(self, ref: ThreadRef) -> str:
        if ref.kind == "post":
            return ref.local_id
        data = self._get("/comment", id=ref.local_id)
        return str(data["comment_view"]["comment"]["post_id"])

    def resolve_ap_id(self, ap_id: str) -> str | None:
        if host_of(ap_id) == self.domain:
            try:
                ref = parse_thread_url(ap_id)
                if ref.kind == "post":
                    return ref.local_id
            except ValueError:
                pass
        try:
            data = self._resolve(None, ap_id)
        except RemoteError:
            return None
        return str(data["post"]["id"]) if data.get("type_") == "post" and data.get("post") else None

    def fetch_post(self, local_id: str) -> NPost:
        data = self._call("GET", "/post", None, id=local_id) or {}
        pv = data.get("post_view")
        if not pv:
            raise RemoteNotFound(f"post {local_id} missing from response")
        post = self._post(pv)
        if data.get("moderators") is not None:
            post.community.moderator_ap_ids = [m.ap_id for m in self._moderators(data)]
        return post

    def fetch_comments(self, post_local_id: str) -> list[NComment]:
        out: dict[str, NComment] = {}
        for batch in self._pages("/comment/list", None, MAX_COMMENT_PAGES, post_id=post_local_id,
                                 type_="all", sort="old", limit=COMMENT_PAGE_LIMIT):
            for cv in batch:
                c = self._comment(cv)
                out[c.local_id] = c
        return list(out.values())

    def fetch_community(self, ref: CommunityRef) -> NCommunity:
        data = self._call("GET", "/community", None, name=ref.qualified) or {}
        return self._community(data["community_view"]["community"], [m.ap_id for m in self._moderators(data)])

    def list_community_posts(self, ref: CommunityRef, sort: str = "New", page: int = 1,
                             limit: int = 20) -> list[NPost]:
        v4_sort, time_range = POST_SORTS.get(sort, (sort.lower(), None))
        for n, batch in enumerate(self._pages("/post/list", None, max(1, page), community_name=ref.qualified,
                                              sort=v4_sort, time_range_seconds=time_range, limit=limit,
                                              type_="all"), start=1):
            if n == page:
                return [self._post(pv) for pv in batch]
        return []  # ran out of pages before reaching `page`

    # -- acting as an account -------------------------------------------------
    def login(self, username: str, password: str, totp: str | None = None) -> str:
        body: dict[str, Any] = {"username_or_email": username, "password": password,
                                "totp_2fa_token": totp or None}
        data = self._call("POST", "/account/auth/login", None, body) or {}
        if not data.get("jwt"):
            raise RemoteAuthError(f"{self.domain}: login returned no session (email verification or "
                                  "registration approval may be pending)")
        return str(data["jwt"])

    def _me(self, token: str) -> dict[str, Any]:
        view = (self._call("GET", "/account", token) or {}).get("local_user_view") or {}
        if not view.get("person"):
            raise RemoteAuthError(f"{self.domain}: session not accepted")
        return view

    def whoami(self, token: str) -> NActor:
        return self._actor(self._me(token)["person"])

    def my_roles(self, token: str) -> dict[str, bool]:
        return {"admin": bool((self._me(token).get("local_user") or {}).get("admin"))}

    def logout(self, token: str) -> None:
        self._call("POST", "/account/auth/logout", token, {})

    def create_community(self, token: str, name: str, title: str, description: str | None = None,
                         nsfw: bool = False, mods_only: bool = False) -> NCommunity:
        data = self._call("POST", "/community", token, {
            "name": name, "title": title, "sidebar": description or None, "nsfw": nsfw,
            "posting_restricted_to_mods": mods_only,
        })
        return self._community(data["community_view"]["community"])

    def resolve_person(self, token: str, ref: str) -> tuple[str, NActor]:
        url = ref if ref.startswith(("http://", "https://")) else self._actor_url(ref)
        try:
            data = self._resolve(token, url)
        except RemoteError as exc:
            raise RemoteNotFound(f"{self.domain} couldn't find {ref}") from exc
        if data.get("type_") != "person" or not data.get("person"):
            raise RemoteNotFound(f"{self.domain} couldn't find {ref}")
        return str(data["person"]["id"]), self._actor(data["person"])

    def resolve_as(self, token: str, ap_id: str) -> dict[str, str]:
        data = self._resolve(token, ap_id)
        kind, out = data.get("type_"), {}
        if kind == "post":
            out["post"] = str(data["post"]["id"])
        elif kind == "comment":
            out["comment"] = str(data["comment"]["id"])
            out["post"] = str(data["comment"]["post_id"])
        elif kind == "community":
            out["community"] = str(data["community"]["id"])
        return out

    def create_post(self, token: str, community_id: str, title: str, body: str | None = None,
                    url: str | None = None) -> NPost:
        data = self._call("POST", "/post", token, {"name": title, "community_id": int(community_id),
                                                   "body": body or None, "url": url or None})
        return self._post(data["post_view"])

    def edit_post(self, token: str, post_id: str, title: str, body: str | None = None,
                  url: str | None = None) -> NPost:
        data = self._call("PUT", "/post", token, {"post_id": int(post_id), "name": title, "body": body or "",
                                                  "url": url or None})
        return self._post(data["post_view"])

    def delete_post(self, token: str, post_id: str, deleted: bool = True) -> NPost:
        data = self._call("DELETE", "/post", token, {"post_id": int(post_id), "deleted": deleted})
        return self._post(data["post_view"])

    @staticmethod
    def _vote(score: int) -> bool | None:
        return None if score == 0 else score > 0  # None is dropped from the body: clears the vote

    def vote_post(self, token: str, post_id: str, score: int) -> NPost:
        data = self._call("POST", "/post/like", token, {"post_id": int(post_id), "is_upvote": self._vote(score)})
        return self._post(data["post_view"])

    def create_comment(self, token: str, post_id: str, body: str, parent_id: str | None = None) -> NComment:
        data = self._call("POST", "/comment", token, {"content": body, "post_id": int(post_id),
                                                      "parent_id": int(parent_id) if parent_id else None})
        return self._comment(data["comment_view"])

    def edit_comment(self, token: str, comment_id: str, body: str) -> NComment:
        data = self._call("PUT", "/comment", token, {"comment_id": int(comment_id), "content": body})
        return self._comment(data["comment_view"])

    def delete_comment(self, token: str, comment_id: str, deleted: bool = True) -> NComment:
        data = self._call("DELETE", "/comment", token, {"comment_id": int(comment_id), "deleted": deleted})
        return self._comment(data["comment_view"])

    def vote_comment(self, token: str, comment_id: str, score: int) -> NComment:
        data = self._call("POST", "/comment/like", token, {"comment_id": int(comment_id),
                                                           "is_upvote": self._vote(score)})
        return self._comment(data["comment_view"])

    # -- moderation ------------------------------------------------------------
    def community_moderators(self, token: str, community_id: str) -> list[NActor]:
        return self._moderators(self._call("GET", "/community", token, id=int(community_id)) or {})

    def set_moderator(self, token: str, community_id: str, person_id: str, added: bool) -> list[NActor]:
        return self._moderators(self._call("POST", "/community/mod", token, {
            "community_id": int(community_id), "person_id": int(person_id), "added": added}) or {})

    def ban_from_community(self, token: str, community_id: str, person_id: str, ban: bool,
                           reason: str | None = None, days: int | None = None, remove_data: bool = False) -> None:
        self._call("POST", "/community/ban_user", token, {
            "community_id": int(community_id), "person_id": int(person_id), "ban": ban,
            "remove_or_restore_data": remove_data, "reason": reason or "",
            "expires_at": int(time.time()) + days * _DAY if ban and days else None})

    def community_bans(self, token: str, community_id: str) -> list[NActor] | None:
        return None  # no API lists a community's bans

    def remove_post(self, token: str, post_id: str, removed: bool, reason: str | None = None) -> None:
        self._call("POST", "/post/remove", token, {"post_id": int(post_id), "removed": removed,
                                                   "reason": reason or ""})

    def remove_comment(self, token: str, comment_id: str, removed: bool, reason: str | None = None) -> None:
        self._call("POST", "/comment/remove", token, {"comment_id": int(comment_id), "removed": removed,
                                                      "reason": reason or ""})

    def lock_post(self, token: str, post_id: str, locked: bool) -> None:
        self._call("POST", "/post/lock", token, {"post_id": int(post_id), "locked": locked, "reason": ""})

    def feature_post(self, token: str, post_id: str, featured: bool) -> None:
        self._call("POST", "/post/feature", token, {"post_id": int(post_id), "featured": featured,
                                                    "feature_type": "community"})

    def site_ban(self, token: str, person_id: str, ban: bool, reason: str | None = None,
                 days: int | None = None, remove_data: bool = False) -> None:
        self._call("POST", "/admin/ban", token, {
            "person_id": int(person_id), "ban": ban, "remove_or_restore_data": remove_data,
            "reason": reason or "", "expires_at": int(time.time()) + days * _DAY if ban and days else None})

    def site_banned(self, token: str) -> list[NActor] | None:
        return None  # 1.0 has no API listing banned people

    def admin_settings(self, token: str) -> dict[str, Any]:
        site = self._call("GET", "/site", token) or {}
        mode = ((site.get("site_view") or {}).get("local_site") or {}).get("registration_mode")
        blocked = [(i.get("instance") or {}).get("domain")
                   for batch in self._pages("/federated_instances", token, kind="blocked", limit=50)
                   for i in batch]
        return {
            "registration_mode": REGISTRATION_FROM_V4.get(mode, mode),
            "blocked_instances": sorted(d for d in blocked if d),
            "blocked_urls": sorted(u.get("url") for u in site.get("blocked_urls") or [] if u.get("url")),
            "supports_blocklists": True,
        }

    def update_site(self, token: str, **fields: Any) -> None:
        # Instances are blocked one at a time in 1.0; everything else is a site edit.
        wanted = fields.pop("blocked_instances", None)
        if wanted is not None:
            current = set(self.admin_settings(token)["blocked_instances"])
            for domain in sorted(set(wanted) ^ current):
                self._call("POST", "/admin/instance/block", token, {
                    "instance": domain, "block": domain in wanted, "reason": "Set from ThreadBNC"})
        if "registration_mode" in fields:
            fields["registration_mode"] = REGISTRATION_TO_V4.get(fields["registration_mode"],
                                                                 fields["registration_mode"])
        if fields:
            self._call("PUT", "/site", token, fields)

    def _site_admins(self) -> set[str]:
        if self._admins is None:
            try:
                self._admins = {self._actor(a.get("person")).ap_id for a in self._get("/site").get("admins") or []}
            except RemoteError:
                self._admins = set()
        return self._admins

    def fetch_moderation_state(
        self, *, post_local_id: str | None = None, comment_local_id: str | None = None,
        community: NCommunity | None = None,
    ) -> list[ModAction]:
        if comment_local_id:
            target, want, params = "comment", comment_local_id, {"comment_id": comment_local_id}
        elif post_local_id:
            target, want, params = "post", post_local_id, {"post_id": post_local_id}
        else:
            return []
        actions: list[ModAction] = []
        for entry in self._get("/modlog", limit=50, **params).get("items") or []:
            log = entry.get("modlog") or {}
            kind, about = MOD_KINDS.get(log.get("kind", ""), (None, None))
            if about != target or str((entry.get(f"target_{target}") or {}).get("id")) != want:
                continue
            mod = self._actor(entry["moderator"]) if entry.get("moderator") else None
            actions.append(ModAction(kind=kind, active=not log.get("is_revert"), when=_ts(log.get("published_at")),
                                     reason=log.get("reason") or None, moderator=mod,
                                     attribution=self._attribution(mod, community)))
        actions.sort(key=lambda a: a.when or "")
        return actions

    # -- private communities ----------------------------------------------------
    def community_by_id(self, token: str, community_id: str) -> NCommunity:
        data = self._call("GET", "/community", token, id=int(community_id)) or {}
        return self._community(data["community_view"]["community"], [m.ap_id for m in self._moderators(data)])

    def set_community_visibility(self, token: str, community_id: str, visibility: str) -> NCommunity:
        data = self._call("PUT", "/community", token, {"community_id": int(community_id),
                                                       "visibility": visibility})
        return self._community(data["community_view"]["community"])

    def join_requests(self, token: str, pending_only: bool = True) -> list[JoinRequest]:
        """Requests to join the private communities this account moderates."""
        out = []
        for batch in self._pages("/community/pending_follows/list", token, unread_only=pending_only, limit=50):
            for item in batch:
                person, community = item.get("person") or {}, item.get("community") or {}
                out.append(JoinRequest(person_local_id=str(person.get("id")), person=self._actor(person),
                                       community_local_id=str(community.get("id")),
                                       community_ap_id=community.get("ap_id") or "",
                                       state=item.get("follow_state") or "approval_required"))
        return out

    def answer_join_request(self, token: str, community_id: str, person_id: str, approve: bool) -> None:
        self._call("POST", "/community/pending_follows/approve", token, {
            "community_id": int(community_id), "follower_id": int(person_id), "approve": approve})
