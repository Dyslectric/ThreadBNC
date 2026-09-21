"""PieFed adapter. PieFed serves a Lemmy-compatible API under /api/alpha."""

from __future__ import annotations

from typing import Any

from .base import ModAction, NCommunity, RemoteError
from .lemmy import LemmyAdapter


class PieFedAdapter(LemmyAdapter):
    software = "piefed"
    api_base = "/api/alpha"
    login_user_field = "username"
    post_title_field = "title"
    comment_body_field = "body"
    supports_totp = False

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
