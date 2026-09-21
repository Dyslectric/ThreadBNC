from __future__ import annotations

import copy
import os
from dataclasses import replace
from pathlib import Path

import pytest

from threadbnc.adapters import (
    CommunityRef, JoinRequest, ModAction, NActor, NComment, NCommunity, NPost, RemoteAuthError, RemoteNotFound,
    RemoteRejected,
    RemoteUnavailable,
    ThreadiverseAdapter, ThreadRef,
)
from threadbnc.bouncer import Bouncer
from threadbnc.config import Settings
from threadbnc.db import open_database

# Set THREADBNC_TEST_DATABASE_URL=postgresql://... to run the suite against
# Postgres (each test gets an empty schema). Otherwise tests use SQLite.
TEST_PG_URL = os.environ.get("THREADBNC_TEST_DATABASE_URL")


def _fresh_test_db() -> str | None:
    if not TEST_PG_URL:
        return None
    import psycopg

    with psycopg.connect(TEST_PG_URL, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")
    return TEST_PG_URL

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
        # Accounts on a separate "home" server, and writes not yet federated
        # to the community's server (see federate()).
        self.users = {"dave": "hunter2"}
        self.sessions: dict[str, str] = {}
        self.outbox: list[tuple[str, NComment]] = []
        self.votes: dict[tuple[str, str], int] = {}
        self.next_id = 5000
        self.revoked = False
        self.admins: set[str] = set()
        self.communities: dict[str, NCommunity] = {}
        self.community_bans: list[tuple[str, str]] = []   # (community local id, person ap id)
        self.site_bans: set[str] = set()
        self.site = {"registration_mode": "RequireApplication", "blocked_instances": [], "blocked_urls": []}
        self.people = {"https://other.test/u/bob": "901", "https://home.test/u/dave": "900",
                       "https://home.test/u/carol": "902"}
        # Lemmy 1.0 private communities: visibility per community local id, and
        # follow requests as [person ap id, community ap id, state].
        self.v4 = True
        self.visibility: dict[str, str] = {}
        self.follows: list[list[str]] = []
        self.join_request_lists = 0
        self.members_only = False  # posts readable only with a session (a private community)
        # Single sign-on (Lemmy 1.0): providers, (provider id, provider identity) -> username,
        # and codes the fake identity provider handed out: code -> (identity, PKCE challenge).
        self.oauth_providers: list[dict] = []
        self.oauth_links: dict[tuple[int, str], str] = {}
        self.oauth_codes: dict[str, tuple[str, str | None]] = {}

    def ask_to_join(self, person_ap_id: str, community_ap_id: str) -> None:
        self.follows = [f for f in self.follows if f[:2] != [person_ap_id, community_ap_id]]
        self.follows.append([person_ap_id, community_ap_id, "approval_required"])

    def follow_state(self, person_ap_id: str) -> str | None:
        return next((f[2] for f in self.follows if f[0] == person_ap_id), None)

    def federate(self) -> None:
        for post_id, c in self.outbox:
            self.comments[post_id][c.local_id] = c
        self.outbox.clear()

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
        self.token: str | None = None

    def reading_as(self, token):
        reader = copy.copy(self)
        reader.token = token
        return reader

    def _check_reader(self) -> None:
        if self.s.members_only and self.token not in self.s.sessions:
            raise RemoteNotFound("couldnt_find_post")  # what Lemmy tells outsiders

    @property
    def supports_private_communities(self) -> bool:
        return self.s.v4

    @property
    def supports_sso(self) -> bool:
        return self.s.v4

    # -- single sign-on (Lemmy 1.0) ------------------------------------------------
    def sign_in_options(self):
        self._check()
        keys = ("id", "display_name", "authorization_endpoint", "client_id", "scopes", "use_pkce")
        return [{k: p[k] for k in keys} for p in self.s.oauth_providers if p["enabled"]]

    def sso_settings(self, token):
        self._auth(token)
        return {"providers": [{k: v for k, v in p.items() if k != "client_secret"} for p in self.s.oauth_providers],
                "signups": bool(self.s.site.get("oauth_registration"))}

    def create_oauth_provider(self, token, **fields):
        self._auth(token)
        provider = {"id": len(self.s.oauth_providers) + 1, **fields}
        self.s.oauth_providers.append(provider)
        return provider

    def edit_oauth_provider(self, token, provider_id, **fields):
        self._auth(token)
        next(p for p in self.s.oauth_providers if p["id"] == provider_id).update(fields)

    def delete_oauth_provider(self, token, provider_id):
        self._auth(token)
        self.s.oauth_providers = [p for p in self.s.oauth_providers if p["id"] != provider_id]

    def oauth_authenticate(self, code, provider_id, redirect_uri, verifier, username=None, answer=None):
        import base64
        import hashlib

        def refuse(err):
            raise RemoteRejected(f"{self.domain} refused: {err}", err)
        provider = next((p for p in self.s.oauth_providers if p["id"] == provider_id and p["enabled"]), None)
        if provider is None or redirect_uri != f"https://{self.domain}/oauth/callback" or code not in self.s.oauth_codes:
            refuse("oauth_authorization_invalid")
        identity, challenge = self.s.oauth_codes.pop(code)  # codes are single-use
        if provider.get("use_pkce"):
            digest = base64.urlsafe_b64encode(hashlib.sha256((verifier or "").encode()).digest()).rstrip(b"=")
            if not verifier or digest.decode() != challenge:
                refuse("oauth_authorization_invalid")
        user = self.s.oauth_links.get((provider_id, identity))
        if user is None:
            if not self.s.site.get("oauth_registration"):
                refuse("oauth_registration_closed")
            if not username:
                refuse("registration_username_required")
            if username in self.s.users:
                refuse("username_already_taken")
            self.s.users[username] = None
            self.s.oauth_links[(provider_id, identity)] = user = username
        token = f"jwt-{user}-{len(self.s.sessions)}"
        self.s.sessions[token] = user
        return {"jwt": token}

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
        self._check_reader()
        if local_id not in self.s.posts:
            raise RemoteNotFound(local_id)
        return copy.deepcopy(self.s.posts[local_id])

    def fetch_comments(self, post_local_id: str) -> list[NComment]:
        self._check()
        self._check_reader()
        return copy.deepcopy(list(self.s.comments.get(post_local_id, {}).values()))

    def fetch_community(self, ref: CommunityRef) -> NCommunity:
        self._check()
        return copy.deepcopy(self.s.communities.get(ref.name, COMMUNITY))

    def list_community_posts(self, ref, sort="New", page=1, limit=20):
        self._check()
        if self.s.members_only and self.token not in self.s.sessions:
            return []
        return [copy.deepcopy(p) for p in sorted(self.s.posts.values(), key=lambda p: p.created_at or "",
                                                  reverse=True)][:limit]

    def fetch_moderation_state(self, *, post_local_id=None, comment_local_id=None, community=None):
        want = ("comment", comment_local_id) if comment_local_id else ("post", post_local_id)
        return [a for kind, lid, a in self.s.modlog if (kind, lid) == want]

    # -- acting as an account on this (home) server --------------------------
    def _auth(self, token: str) -> str:
        if self.s.revoked or token not in self.s.sessions:
            raise RemoteAuthError("not_logged_in", "not_logged_in")
        return self.s.sessions[token]

    def login(self, username, password, totp=None):
        if self.s.users.get(username) != password:
            raise RemoteAuthError("incorrect_login", "incorrect_login")
        token = f"jwt-{username}-{len(self.s.sessions)}"
        self.s.sessions[token] = username
        return token

    def whoami(self, token):
        user = self._auth(token)
        return NActor(f"https://{self.domain}/u/{user}", user, self.domain, "Dave")

    def logout(self, token):
        self.s.sessions.pop(token, None)

    def my_roles(self, token):
        return {"admin": self._auth(token) in self.s.admins}

    # -- moderation ------------------------------------------------------------
    def _actor_for(self, ap_id):
        name = ap_id.rstrip("/").rsplit("/", 1)[-1]
        return NActor(ap_id, name, ap_id.split("/")[2])

    def resolve_person(self, token, ref):
        self._auth(token)
        if not ref.startswith("http"):
            user, host = ref.lstrip("@").split("@")
            ref = f"https://{host}/u/{user}"
        if ref not in self.s.people:
            raise RemoteNotFound(ref)
        return self.s.people[ref], self._actor_for(ref)

    def _mods(self):
        for p in self.s.posts.values():
            return p.community.moderator_ap_ids or []
        return []

    def community_moderators(self, token, community_id):
        self._auth(token)
        return [self._actor_for(a) for a in self._mods()]

    def set_moderator(self, token, community_id, person_id, added):
        self._auth(token)
        ap = next(a for a, pid in self.s.people.items() if pid == person_id)
        for p in self.s.posts.values():
            mods = p.community.moderator_ap_ids
            if added and ap not in mods:
                mods.append(ap)
            if not added and ap in mods:
                mods.remove(ap)
        return [self._actor_for(a) for a in self._mods()]

    def ban_from_community(self, token, community_id, person_id, ban, reason=None, days=None, remove_data=False):
        self._auth(token)
        ap = next(a for a, pid in self.s.people.items() if pid == person_id)
        if ban:
            self.s.community_bans.append((community_id, ap))
        else:
            self.s.community_bans = [b for b in self.s.community_bans if b != (community_id, ap)]

    def community_bans(self, token, community_id):
        return None

    def _mod_flag(self, token, post_id, **flags):
        user = self._auth(token)
        self.s.posts[post_id] = replace(self.s.posts[post_id], **flags)
        return user

    def remove_post(self, token, post_id, removed, reason=None):
        user = self._mod_flag(token, post_id, removed=removed, body="" if removed else self.s.posts[post_id].body)
        self.s.modlog.append(("post", post_id, ModAction("remove_post", removed, "2026-09-21T12:00:00.000000Z",
                                                         reason, NActor(f"https://{self.domain}/u/{user}", user,
                                                                        self.domain), "moderator")))

    def remove_comment(self, token, comment_id, removed, reason=None):
        self._auth(token)
        pid, cs = self._find_comment(comment_id)
        cs[comment_id] = replace(cs[comment_id], removed=removed)

    def lock_post(self, token, post_id, locked):
        self._mod_flag(token, post_id, locked=locked)

    def feature_post(self, token, post_id, featured):
        self._mod_flag(token, post_id, featured=featured)

    def site_ban(self, token, person_id, ban, reason=None, days=None, remove_data=False):
        self._auth(token)
        ap = next(a for a, pid in self.s.people.items() if pid == person_id)
        (self.s.site_bans.add if ban else self.s.site_bans.discard)(ap)

    def site_banned(self, token):
        self._auth(token)
        return [self._actor_for(a) for a in sorted(self.s.site_bans)]

    def admin_settings(self, token):
        self._auth(token)
        return {**copy.deepcopy(self.s.site), "supports_blocklists": True}

    def update_site(self, token, **fields):
        self._auth(token)
        self.s.site.update(fields)

    def create_community(self, token, name, title, description=None, nsfw=False, mods_only=False):
        user = self._auth(token)
        if user not in self.s.admins:
            raise RemoteRejected("home.test refused: only_admins_can_create_communities",
                                 "only_admins_can_create_communities")
        if name in self.s.communities:
            raise RemoteRejected("home.test refused: community_already_exists", "community_already_exists")
        community = NCommunity(ap_id=f"https://{self.domain}/c/{name}", name=name, domain=self.domain,
                               title=title, description=description,
                               moderator_ap_ids=[f"https://{self.domain}/u/{user}"])
        self.s.communities[name] = community
        return copy.deepcopy(community)

    def resolve_as(self, token, ap_id):
        self._auth(token)
        if "/c/" in ap_id:
            return {"community": "77"}
        for pid, p in self.s.posts.items():
            if p.ap_id == ap_id:
                return {"post": pid}
        for pid, cs in self.s.comments.items():
            for cid, c in cs.items():
                if c.ap_id == ap_id:
                    return {"comment": cid, "post": pid}
        for pid, c in self.s.outbox:
            if c.ap_id == ap_id:
                return {"comment": c.local_id, "post": pid}
        return {}

    # -- private communities (Lemmy 1.0) -------------------------------------------
    def community_by_id(self, token, community_id):
        self._auth(token)
        return replace(COMMUNITY, local_id=community_id, visibility=self.s.visibility.get(community_id, "public"))

    def set_community_visibility(self, token, community_id, visibility):
        self._auth(token)
        self.s.visibility[community_id] = visibility
        return self.community_by_id(token, community_id)

    def join_requests(self, token, pending_only=True):
        self._auth(token)
        self.s.join_request_lists += 1
        return [JoinRequest(self.s.people[p], self._actor_for(p), "77", c, state)
                for p, c, state in self.s.follows if state == "approval_required" or not pending_only]

    def answer_join_request(self, token, community_id, person_id, approve):
        self._auth(token)
        ap = next(a for a, pid in self.s.people.items() if pid == person_id)
        follow = next((f for f in self.s.follows if f[0] == ap), None)
        if follow is None or follow[2] != "approval_required":
            raise RemoteRejected("home.test refused: couldnt_update", "couldnt_update")
        follow[2] = "accepted" if approve else "denied"

    def create_comment(self, token, post_id, body, parent_id=None):
        user = self._auth(token)
        self.s.next_id += 1
        lid = str(self.s.next_id)
        c = NComment(ap_id=f"https://{self.domain}/comment/{lid}", local_id=lid, parent_local_id=parent_id,
                     body=body, created_at="2026-09-21T09:00:00.000000Z", updated_at=None, deleted=False,
                     removed=False, author=NActor(f"https://{self.domain}/u/{user}", user, self.domain))
        self.s.outbox.append((post_id, c))
        return copy.deepcopy(c)

    def _find_comment(self, cid):
        for pid, cs in self.s.comments.items():
            if cid in cs:
                return pid, cs
        for pid, c in self.s.outbox:
            if c.local_id == cid:
                return pid, None
        raise RemoteNotFound(cid)

    def edit_comment(self, token, comment_id, body):
        self._auth(token)
        pid, cs = self._find_comment(comment_id)
        if cs is None:
            raise RemoteNotFound("not federated in fake")
        cs[comment_id] = replace(cs[comment_id], body=body, updated_at="2026-09-21T10:00:00.000000Z")
        return copy.deepcopy(cs[comment_id])

    def delete_comment(self, token, comment_id, deleted=True):
        self._auth(token)
        pid, cs = self._find_comment(comment_id)
        return copy.deepcopy(cs[comment_id]) if cs else None

    def vote_comment(self, token, comment_id, score):
        user = self._auth(token)
        self.s.votes[(user, comment_id)] = score
        pid, cs = self._find_comment(comment_id)
        return copy.deepcopy(cs[comment_id]) if cs else None

    def vote_post(self, token, post_id, score):
        user = self._auth(token)
        self.s.votes[(user, "post:" + post_id)] = score
        return copy.deepcopy(self.s.posts[post_id])

    def create_post(self, token, community_id, title, body=None, url=None):
        user = self._auth(token)
        self.s.next_id += 1
        lid = str(self.s.next_id)
        p = NPost(ap_id=f"https://{self.domain}/post/{lid}", local_id=lid, title=title, body=body, url=url,
                  created_at="2026-09-21T09:00:00.000000Z", updated_at=None, deleted=False, removed=False,
                  locked=False, community=copy.deepcopy(COMMUNITY),
                  author=NActor(f"https://{self.domain}/u/{user}", user, self.domain))
        self.s.posts[lid] = p
        self.s.comments[lid] = {}
        return copy.deepcopy(p)


@pytest.fixture
def server() -> FakeServer:
    return FakeServer()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path, db_path=tmp_path / "t.sqlite3", database_url=_fresh_test_db(),
        password="pw", api_token="tok",
        secret_key="x" * 40, https_only_cookies=False, embedded_bouncer=False, default_sync_minutes=30,
        default_follow_poll_minutes=15, default_follow_retention_days=30, http_timeout=5,
        min_request_interval=0, user_agent="test",
    )


@pytest.fixture
def bouncer(settings: Settings, server: FakeServer) -> Bouncer:
    return Bouncer(open_database(settings), settings, adapter_factory=lambda d: FakeAdapter(d, server))
