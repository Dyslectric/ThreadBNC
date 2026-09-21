"""Lemmy adapter (HTTP API v3, as served by Lemmy 0.19.x).

PieFed exposes a Lemmy-shaped API under /api/alpha, so PieFedAdapter subclasses
this and overrides the few differences.
"""

from __future__ import annotations

from typing import Any

from ..db import fmt_ts, parse_ts
from .base import (
    CommunityRef,
    ModAction,
    NActor,
    NComment,
    NCommunity,
    NPost,
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

    def __init__(self, domain: str, http: HttpClient):
        super().__init__(domain)
        self.http = http
        self._admins: set[str] | None = None

    # -- low level -------------------------------------------------------
    def _get(self, path: str, **params: Any) -> Any:
        return self.http.get_json(self.domain, f"{self.api_base}{path}", params)

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
