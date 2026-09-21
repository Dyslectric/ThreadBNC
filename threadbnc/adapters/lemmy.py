"""Lemmy adapter (HTTP API v3, as served by Lemmy 0.19.x).

PieFed exposes a Lemmy-shaped API under /api/alpha, so PieFedAdapter subclasses
this and overrides the few differences.
"""

from __future__ import annotations

import copy
import time
from typing import Any

from ..db import fmt_ts, parse_ts
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
    ThreadiverseAdapter,
    ThreadRef,
    host_of,
    parse_thread_url,
)
from .http import HttpClient

COMMENT_PAGE_LIMIT = 50
MAX_COMMENT_PAGES = 400


def _ts(value: Any) -> str | None:
    dt = parse_ts(value) if isinstance(value, str) else None
    return fmt_ts(dt) if dt else None


class LemmyAdapter(ThreadiverseAdapter):
    software = "lemmy"
    api_base = "/api/v3"
    # Field names that differ between Lemmy and PieFed.
    login_user_field = "username_or_email"
    post_title_field = "name"
    comment_body_field = "content"
    mods_only_field = "posting_restricted_to_mods"
    supports_totp = True

    def __init__(self, domain: str, http: HttpClient):
        super().__init__(domain)
        self.http = http
        self._admins: set[str] | None = None
        self._read_token: str | None = None  # see reading_as()

    def reading_as(self, token: str) -> "LemmyAdapter":
        """A copy whose reads are made as a logged-in account, for content only
        members can see (private communities)."""
        reader = copy.copy(self)
        reader._read_token = token
        return reader

    # -- low level -------------------------------------------------------
    def _get(self, path: str, **params: Any) -> Any:
        return self.http.get_json(self.domain, f"{self.api_base}{path}", params, token=self._read_token)

    def _call(self, method: str, path: str, token: str | None, body: dict[str, Any] | None = None,
              **params: Any) -> Any:
        return self.http.request_json(method, self.domain, f"{self.api_base}{path}", params=params,
                                      json=body, token=token)

    # -- normalizers -----------------------------------------------------
    def _actor(self, person: dict[str, Any] | None) -> NActor:
        person = person or {}
        ap_id = person.get("actor_id") or person.get("ap_id") or ""
        return NActor(
            ap_id=ap_id,
            username=person.get("name") or person.get("user_name") or "?",
            domain=host_of(ap_id) or self.domain,
            display_name=person.get("display_name") or person.get("title"),
        )

    def _community(self, c: dict[str, Any], moderators: list[str] | None = None) -> NCommunity:
        ap_id = c.get("actor_id") or c.get("ap_id") or ""
        return NCommunity(
            ap_id=ap_id,
            name=c.get("name", "?"),
            domain=host_of(ap_id) or self.domain,
            title=c.get("title"),
            local_id=str(c["id"]) if c.get("id") is not None else None,
            removed=bool(c.get("removed")),
            deleted=bool(c.get("deleted")),
            description=c.get("description"),
            moderator_ap_ids=moderators,
        )

    def _post(self, pv: dict[str, Any]) -> NPost:
        p = pv["post"]
        counts = pv.get("counts") or {}
        meta = {
            "nsfw": bool(p.get("nsfw")),
            "language_id": p.get("language_id"),
            "alt_text": p.get("alt_text"),
        }
        return NPost(
            ap_id=p.get("ap_id") or f"https://{self.domain}/post/{p['id']}",
            local_id=str(p["id"]),
            title=p.get("name") if p.get("name") is not None else (p.get("title") or ""),
            body=p.get("body"),
            url=p.get("url"),
            created_at=_ts(p.get("published")),
            updated_at=_ts(p.get("updated")),
            deleted=bool(p.get("deleted")),
            removed=bool(p.get("removed")),
            locked=bool(p.get("locked")),
            community=self._community(pv.get("community") or {}),
            author=self._actor(pv.get("creator")),
            metadata={k: v for k, v in meta.items() if v not in (None, False, "")},
            score=counts.get("score"),
            comment_count=counts.get("comments"),
            thumbnail_url=p.get("thumbnail_url") or p.get("small_thumbnail_url"),
            newest_comment_at=_ts(counts.get("newest_comment_time")),
            featured=bool(p.get("featured_community") or p.get("featured_local")  # Lemmy
                          or p.get("sticky") or p.get("instance_sticky")),  # PieFed
            upvotes=counts.get("upvotes"),
            downvotes=counts.get("downvotes"),
        )

    def _comment(self, cv: dict[str, Any]) -> NComment:
        c = cv["comment"]
        counts = cv.get("counts") or {}
        parent: str | None = None
        path = c.get("path")
        if path:
            parts = [x for x in str(path).split(".") if x]
            # path = "0.<ancestor ids>.<own id>"
            if len(parts) >= 3:
                parent = parts[-2]
        elif c.get("parent_id"):
            parent = str(c["parent_id"])
        meta = {"distinguished": bool(c.get("distinguished")), "language_id": c.get("language_id")}
        return NComment(
            ap_id=c.get("ap_id") or f"https://{self.domain}/comment/{c['id']}",
            local_id=str(c["id"]),
            parent_local_id=parent,
            body=c.get("content") if "content" in c else c.get("body"),
            created_at=_ts(c.get("published")),
            updated_at=_ts(c.get("updated")),
            deleted=bool(c.get("deleted")),
            removed=bool(c.get("removed")),
            author=self._actor(cv.get("creator")),
            metadata={k: v for k, v in meta.items() if v not in (None, False, "")},
            score=counts.get("score"),
            reply_count=counts.get("child_count"),
            upvotes=counts.get("upvotes"),
            downvotes=counts.get("downvotes"),
        )

    # -- interface -------------------------------------------------------
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
            data = self._get("/resolve_object", q=ap_id)
        except RemoteError:
            return None
        post = (data or {}).get("post")
        return str(post["post"]["id"]) if post else None

    def fetch_post(self, local_id: str) -> NPost:
        data = self._get("/post", id=local_id)
        pv = data.get("post_view")
        if not pv:
            raise RemoteNotFound(f"post {local_id} missing from response")
        post = self._post(pv)
        mods = data.get("moderators")
        if mods is not None:
            post.community.moderator_ap_ids = [
                (m.get("moderator") or {}).get("actor_id", "") for m in mods
            ]
        return post

    def _comment_page(self, post_local_id: str, page: int) -> list[dict[str, Any]]:
        data = self._get(
            "/comment/list", post_id=post_local_id, type_="All", sort="Old",
            limit=COMMENT_PAGE_LIMIT, page=page,
        )
        return data.get("comments") or []

    def fetch_comments(self, post_local_id: str) -> list[NComment]:
        out: dict[str, NComment] = {}
        for page in range(1, MAX_COMMENT_PAGES + 1):
            batch = self._comment_page(post_local_id, page)
            for cv in batch:
                c = self._comment(cv)
                out[c.local_id] = c
            if len(batch) < COMMENT_PAGE_LIMIT:
                break
        return list(out.values())

    def fetch_community(self, ref: CommunityRef) -> NCommunity:
        data = self._get("/community", name=ref.qualified)
        cv = data["community_view"]
        mods = [(m.get("moderator") or {}).get("actor_id", "") for m in data.get("moderators") or []]
        return self._community(cv["community"], mods)

    def list_community_posts(
        self, ref: CommunityRef, sort: str = "New", page: int = 1, limit: int = 20
    ) -> list[NPost]:
        data = self._get(
            "/post/list", community_name=ref.qualified, sort=sort, page=page, limit=limit, type_="All"
        )
        return [self._post(pv) for pv in data.get("posts") or []]

    # -- acting as an account ---------------------------------------------
    def login(self, username: str, password: str, totp: str | None = None) -> str:
        body: dict[str, Any] = {self.login_user_field: username, "password": password}
        if totp and self.supports_totp:
            body["totp_2fa_token"] = totp
        data = self._call("POST", "/user/login", None, body)
        token = (data or {}).get("jwt")
        if not token:
            raise RemoteAuthError(f"{self.domain}: login returned no session (email verification or "
                                  "registration approval may be pending)")
        return str(token)

    def whoami(self, token: str) -> NActor:
        data = self._call("GET", "/site", token)
        person = (((data or {}).get("my_user") or {}).get("local_user_view") or {}).get("person")
        if not person:
            raise RemoteAuthError(f"{self.domain}: session not accepted")
        return self._actor(person)

    def my_roles(self, token: str) -> dict[str, bool]:
        """Whether this account is an admin of its server. Uses the user's own
        admin flag where the server has one (Lemmy) and the site's admin list
        otherwise (PieFed)."""
        data = self._call("GET", "/site", token) or {}
        view = ((data.get("my_user") or {}).get("local_user_view") or {})
        me = (view.get("person") or {})
        my_id = me.get("actor_id") or me.get("ap_id")
        admin_ids = {((a or {}).get("person") or {}).get("actor_id") for a in data.get("admins") or []}
        admin = bool((view.get("local_user") or {}).get("admin") or me.get("admin") or (my_id and my_id in admin_ids))
        return {"admin": admin}

    def create_community(self, token: str, name: str, title: str, description: str | None = None,
                         nsfw: bool = False, mods_only: bool = False) -> NCommunity:
        data = self._call("POST", "/community", token, {
            "name": name, "title": title, "description": description or None, "nsfw": nsfw,
            self.mods_only_field: mods_only,
        })
        return self._community(data["community_view"]["community"])

    # -- moderation -------------------------------------------------------
    def resolve_person(self, token: str, ref: str) -> tuple[str, NActor]:
        """Local id + identity of a person on this server, from an actor URL or
        user@host handle (fetched over federation if needed)."""
        q = ref if ref.startswith(("http://", "https://")) else "@" + ref.lstrip("@")
        data = self._call("GET", "/resolve_object", token, q=q) or {}
        person = (data.get("person") or {}).get("person")
        if not person:
            raise RemoteNotFound(f"{self.domain} couldn't find {ref}")
        return str(person["id"]), self._actor(person)

    def community_moderators(self, token: str, community_id: str) -> list[NActor]:
        data = self._call("GET", "/community", token, id=int(community_id)) or {}
        return [self._actor(m.get("moderator")) for m in data.get("moderators") or []]

    def set_moderator(self, token: str, community_id: str, person_id: str, added: bool) -> list[NActor]:
        data = self._call("POST", "/community/mod", token, {
            "community_id": int(community_id), "person_id": int(person_id), "added": added}) or {}
        return [self._actor(m.get("moderator")) for m in data.get("moderators") or []]

    def ban_from_community(self, token: str, community_id: str, person_id: str, ban: bool,
                           reason: str | None = None, days: int | None = None, remove_data: bool = False) -> None:
        body: dict[str, Any] = {"community_id": int(community_id), "person_id": int(person_id), "ban": ban,
                                "remove_data": remove_data, "reason": reason or None}
        if ban and days:
            body["expires"] = int(time.time()) + days * 86400
        self._call("POST", "/community/ban_user", token, body)

    def community_bans(self, token: str, community_id: str) -> list[NActor] | None:
        return None  # Lemmy 0.19 has no API to list a community's bans

    def remove_post(self, token: str, post_id: str, removed: bool, reason: str | None = None) -> None:
        self._call("POST", "/post/remove", token, {"post_id": int(post_id), "removed": removed,
                                                   "reason": reason or None})

    def remove_comment(self, token: str, comment_id: str, removed: bool, reason: str | None = None) -> None:
        self._call("POST", "/comment/remove", token, {"comment_id": int(comment_id), "removed": removed,
                                                      "reason": reason or None})

    def lock_post(self, token: str, post_id: str, locked: bool) -> None:
        self._call("POST", "/post/lock", token, {"post_id": int(post_id), "locked": locked})

    def feature_post(self, token: str, post_id: str, featured: bool) -> None:
        self._call("POST", "/post/feature", token, {"post_id": int(post_id), "featured": featured,
                                                    "feature_type": "Community"})

    def site_ban(self, token: str, person_id: str, ban: bool, reason: str | None = None,
                 days: int | None = None, remove_data: bool = False) -> None:
        body: dict[str, Any] = {"person_id": int(person_id), "ban": ban, "remove_data": remove_data,
                                "reason": reason or None}
        if ban and days:
            body["expires"] = int(time.time()) + days * 86400
        self._call("POST", "/user/ban", token, body)

    def site_banned(self, token: str) -> list[NActor] | None:
        data = self._call("GET", "/user/banned", token) or {}
        return [self._actor(p.get("person")) for p in data.get("banned") or []]

    def admin_settings(self, token: str) -> dict[str, Any]:
        """Registration mode and the server-wide instance / link blocklists."""
        site = self._call("GET", "/site", token) or {}
        local = (site.get("site_view") or {}).get("local_site") or {}
        fed = (self._call("GET", "/federated_instances", token) or {}).get("federated_instances") or {}
        return {
            "registration_mode": local.get("registration_mode"),
            "blocked_instances": sorted(i.get("domain") for i in fed.get("blocked") or [] if i.get("domain")),
            "blocked_urls": sorted(u.get("url") for u in site.get("blocked_urls") or [] if u.get("url")),
            "supports_blocklists": True,
        }

    def update_site(self, token: str, **fields: Any) -> None:
        self._call("PUT", "/site", token, fields)

    def logout(self, token: str) -> None:
        self._call("POST", "/user/logout", token, {})

    def resolve_as(self, token: str, ap_id: str) -> dict[str, str]:
        """Local ids on this server for a post/comment/community ActivityPub id,
        fetching it over federation if needed. Keys: post, comment, community."""
        data = self._call("GET", "/resolve_object", token, q=ap_id) or {}
        out: dict[str, str] = {}
        if data.get("post"):
            out["post"] = str(data["post"]["post"]["id"])
        if data.get("comment"):
            out["comment"] = str(data["comment"]["comment"]["id"])
            out["post"] = str(data["comment"]["comment"]["post_id"])
        if data.get("community"):
            out["community"] = str(data["community"]["community"]["id"])
        return out

    def create_post(self, token: str, community_id: str, title: str, body: str | None = None,
                    url: str | None = None) -> NPost:
        data = self._call("POST", "/post", token, {self.post_title_field: title, "community_id": int(community_id),
                                                   "body": body or None, "url": url or None})
        return self._post(data["post_view"])

    def edit_post(self, token: str, post_id: str, title: str, body: str | None = None,
                  url: str | None = None) -> NPost:
        data = self._call("PUT", "/post", token, {"post_id": int(post_id), self.post_title_field: title,
                                                  "body": body or "", "url": url or None})
        return self._post(data["post_view"])

    def delete_post(self, token: str, post_id: str, deleted: bool = True) -> NPost:
        data = self._call("POST", "/post/delete", token, {"post_id": int(post_id), "deleted": deleted})
        return self._post(data["post_view"])

    def vote_post(self, token: str, post_id: str, score: int) -> NPost:
        data = self._call("POST", "/post/like", token, {"post_id": int(post_id), "score": score})
        return self._post(data["post_view"])

    def create_comment(self, token: str, post_id: str, body: str, parent_id: str | None = None) -> NComment:
        data = self._call("POST", "/comment", token, {self.comment_body_field: body, "post_id": int(post_id),
                                                      "parent_id": int(parent_id) if parent_id else None})
        return self._comment(data["comment_view"])

    def edit_comment(self, token: str, comment_id: str, body: str) -> NComment:
        data = self._call("PUT", "/comment", token, {"comment_id": int(comment_id), self.comment_body_field: body})
        return self._comment(data["comment_view"])

    def delete_comment(self, token: str, comment_id: str, deleted: bool = True) -> NComment:
        data = self._call("POST", "/comment/delete", token, {"comment_id": int(comment_id), "deleted": deleted})
        return self._comment(data["comment_view"])

    def vote_comment(self, token: str, comment_id: str, score: int) -> NComment:
        data = self._call("POST", "/comment/like", token, {"comment_id": int(comment_id), "score": score})
        return self._comment(data["comment_view"])

    # -- moderation ------------------------------------------------------
    def _site_admins(self) -> set[str]:
        if self._admins is None:
            try:
                data = self._get("/site")
                self._admins = {
                    (a.get("person") or {}).get("actor_id", "") for a in data.get("admins") or []
                }
            except RemoteError:
                self._admins = set()
        return self._admins

    def _attribution(self, moderator: NActor | None, community: NCommunity | None) -> str:
        if moderator is None or not moderator.ap_id:
            return "unknown"  # instance hides moderator names
        is_admin = moderator.ap_id in self._site_admins()
        mods = set(community.moderator_ap_ids or []) if community else set()
        is_mod = moderator.ap_id in mods
        if is_mod and not is_admin:
            return "moderator"
        if is_admin and not is_mod:
            return "admin"
        return "unknown"

    def fetch_moderation_state(
        self, *, post_local_id: str | None = None, comment_local_id: str | None = None,
        community: NCommunity | None = None,
    ) -> list[ModAction]:
        params: dict[str, Any] = {"limit": 50}
        if comment_local_id:
            params["comment_id"] = comment_local_id
        elif post_local_id:
            params["post_id"] = post_local_id
        else:
            return []
        data = self._get("/modlog", **params)
        actions: list[ModAction] = []

        def collect(key: str, inner: str, flag: str, kind: str, id_field: str, want: str) -> None:
            for entry in data.get(key) or []:
                rec = entry.get(inner) or {}
                if str(rec.get(id_field)) != want:
                    continue
                mod = self._actor(entry["moderator"]) if entry.get("moderator") else None
                actions.append(ModAction(
                    kind=kind,
                    active=bool(rec.get(flag)),
                    when=_ts(rec.get("when_") or rec.get("published")),
                    reason=rec.get("reason"),
                    moderator=mod,
                    attribution=self._attribution(mod, community),
                ))

        if comment_local_id:
            collect("removed_comments", "mod_remove_comment", "removed", "remove_comment",
                    "comment_id", comment_local_id)
        else:
            collect("removed_posts", "mod_remove_post", "removed", "remove_post", "post_id", post_local_id)
            collect("locked_posts", "mod_lock_post", "locked", "lock_post", "post_id", post_local_id)
        actions.sort(key=lambda a: a.when or "")
        return actions
