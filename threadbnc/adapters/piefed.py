"""PieFed adapter. PieFed serves a Lemmy-compatible API under /api/alpha."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from .base import ModAction, NActor, NCommunity, RemoteError, UnsupportedSoftware
from .lemmy import LemmyAdapter


class PieFedAdapter(LemmyAdapter):
    software = "piefed"
    api_base = "/api/alpha"
    login_user_field = "username"
    post_title_field = "title"
    comment_body_field = "body"
    mods_only_field = "restricted_to_mods"
    supports_totp = False
    # Mentions come back as comment replies, marked read the same way.
    mentions_path = "/user/mentions"
    mentions_key = "replies"
    mention_record = "comment_reply"
    mention_read = ("/comment/mark_as_read", "comment_reply_id")

    # PieFed splits bans/unbans into separate endpoints and can list bans.
    def ban_from_community(self, token: str, community_id: str, person_id: str, ban: bool,
                           reason: str | None = None, days: int | None = None, remove_data: bool = False) -> None:
        if not ban:
            self._call("PUT", "/community/moderate/unban", token,
                       {"community_id": int(community_id), "user_id": int(person_id)})
            return
        body: dict[str, Any] = {"community_id": int(community_id), "user_id": int(person_id),
                                "reason": reason or "", "permanent": not days}
        if days:
            body["expires_at"] = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
        self._call("POST", "/community/moderate/ban", token, body)

    def community_bans(self, token: str, community_id: str) -> list[NActor] | None:
        data = self._call("GET", "/community/moderate/bans", token, community_id=int(community_id)) or {}
        items = data.get("items") or data.get("bans") or data.get("banned") or []
        return [self._actor(i.get("banned_user") or i.get("person") or i) for i in items]

    def site_ban(self, token: str, person_id: str, ban: bool, reason: str | None = None,
                 days: int | None = None, remove_data: bool = False) -> None:
        if ban:
            self._call("POST", "/user/ban", token, {"person_id": int(person_id), "reason": reason or "",
                                                    "purge_content": remove_data})
        else:
            self._call("POST", "/user/unban", token, {"person_id": int(person_id)})

    def site_banned(self, token: str) -> list[NActor] | None:
        return None  # not exposed by PieFed's API

    def admin_settings(self, token: str) -> dict[str, Any]:
        site = self._call("GET", "/site", token) or {}
        return {"registration_mode": (site.get("site") or {}).get("registration_mode"),
                "blocked_instances": [], "blocked_urls": [], "supports_blocklists": False}

    def update_site(self, token: str, **fields: Any) -> None:
        raise UnsupportedSoftware("PieFed's API doesn't expose server-wide settings or blocklists; "
                                  "use PieFed's own admin pages for that.")

    def fetch_moderation_state(
        self, *, post_local_id: str | None = None, comment_local_id: str | None = None,
        community: NCommunity | None = None,
    ) -> list[ModAction]:
        # PieFed's modlog API is not stable across versions; treat any failure
        # as "unknown" rather than guessing.
        try:
            return super().fetch_moderation_state(
                post_local_id=post_local_id, comment_local_id=comment_local_id, community=community
            )
        except (RemoteError, KeyError, TypeError, AttributeError):
            return []

    def _site_admins(self) -> set[str]:
        try:
            return super()._site_admins()
        except (KeyError, TypeError, AttributeError):
            return set()

    def _comment_page(self, post_local_id: str, page: int) -> list[dict[str, Any]]:
        data = self._get(
            "/comment/list", post_id=post_local_id, sort="Old", limit=50, page=page, type_="All"
        )
        return data.get("comments") or []
