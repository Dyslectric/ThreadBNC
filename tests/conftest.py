from __future__ import annotations

import copy
from dataclasses import replace
from pathlib import Path

import pytest

from threadbnc.adapters import (
    CommunityRef, ModAction, NActor, NComment, NCommunity, NPost, RemoteNotFound, RemoteUnavailable,
    ThreadiverseAdapter, ThreadRef,
)
from threadbnc.bouncer import Bouncer
from threadbnc.config import Settings
from threadbnc.db import Database

DOMAIN = "lemmy.test"
COMMUNITY = NCommunity(ap_id=f"https://{DOMAIN}/c/math", name="math", domain=DOMAIN, title="Math",
                       moderator_ap_ids=[f"https://{DOMAIN}/u/mod"])
ALICE = NActor(f"https://{DOMAIN}/u/alice", "alice", DOMAIN)
BOB = NActor(f"https://other.test/u/bob", "bob", "other.test")
MOD = NActor(f"https://{DOMAIN}/u/mod", "mod", DOMAIN)


class FakeServer:
    """Mutable in-memory Lemmy server."""

    def __init__(self) -> None:
        self.posts: dict[str, NPost] = {}
        self.comments: dict[str, dict[str, NComment]] = {}
        self.modlog: list[tuple[str, str, ModAction]] = []  # (kind, local_id, action)
        self.down = False

    def add_post(self, local_id: str, title: str, body: str, created: str = "2026-09-01T00:00:00.000000Z") -> NPost:
        p = NPost(ap_id=f"https://{DOMAIN}/post/{local_id}", local_id=local_id, title=title, body=body,
                  url=None, created_at=created, updated_at=None, deleted=False, removed=False,
                  locked=False, community=copy.deepcopy(COMMUNITY), author=ALICE)
        self.posts[local_id] = p
        self.comments[local_id] = {}
        return p

    def add_comment(self, post_id: str, local_id: str, body: str, parent: str | None = None,
                    author: NActor = BOB) -> NComment:
        c = NComment(ap_id=f"https://{DOMAIN}/comment/{local_id}", local_id=local_id, parent_local_id=parent,
                     body=body, created_at="2026-09-01T01:00:00.000000Z", updated_at=None,
                     deleted=False, removed=False, author=author)
        self.comments[post_id][local_id] = c
        return c

    def edit_comment(self, post_id: str, local_id: str, **changes) -> None:
        self.comments[post_id][local_id] = replace(self.comments[post_id][local_id], **changes)

    def edit_post(self, local_id: str, **changes) -> None:
        self.posts[local_id] = replace(self.posts[local_id], **changes)


class FakeAdapter(ThreadiverseAdapter):
    software = "lemmy"

    def __init__(self, domain: str, server: FakeServer):
        super().__init__(domain)
        self.s = server

    def _check(self) -> None:
        if self.s.down:
            raise RemoteUnavailable("connection refused")

    def resolve_url(self, ref: ThreadRef) -> str:
        self._check()
        if ref.kind == "post":
            return ref.local_id
        for pid, cs in self.s.comments.items():
            if ref.local_id in cs:
                return pid
        raise RemoteNotFound("comment")

    def resolve_ap_id(self, ap_id: str) -> str | None:
        return next((k for k, p in self.s.posts.items() if p.ap_id == ap_id), None)

    def fetch_post(self, local_id: str) -> NPost:
        self._check()
        if local_id not in self.s.posts:
            raise RemoteNotFound(local_id)
        return copy.deepcopy(self.s.posts[local_id])

    def fetch_comments(self, post_local_id: str) -> list[NComment]:
        self._check()
        return copy.deepcopy(list(self.s.comments.get(post_local_id, {}).values()))

    def fetch_community(self, ref: CommunityRef) -> NCommunity:
        self._check()
        return copy.deepcopy(COMMUNITY)

    def list_community_posts(self, ref, sort="New", page=1, limit=20):
        self._check()
        return [copy.deepcopy(p) for p in sorted(self.s.posts.values(), key=lambda p: p.created_at or "",
                                                  reverse=True)][:limit]

    def fetch_moderation_state(self, *, post_local_id=None, comment_local_id=None, community=None):
        want = ("comment", comment_local_id) if comment_local_id else ("post", post_local_id)
        return [a for kind, lid, a in self.s.modlog if (kind, lid) == want]


@pytest.fixture
def server() -> FakeServer:
    return FakeServer()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path, db_path=tmp_path / "t.sqlite3", password="pw", api_token="tok",
        secret_key="x" * 40, https_only_cookies=False, embedded_bouncer=False, default_sync_minutes=30,
        default_follow_poll_minutes=15, default_follow_retention_days=30, http_timeout=5,
        min_request_interval=0, user_agent="test",
    )


@pytest.fixture
def bouncer(settings: Settings, server: FakeServer) -> Bouncer:
    return Bouncer(Database(settings.db_path), settings, adapter_factory=lambda d: FakeAdapter(d, server))
