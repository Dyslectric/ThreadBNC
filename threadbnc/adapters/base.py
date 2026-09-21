"""Software-neutral types and the ThreadiverseAdapter interface."""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse


class RemoteError(Exception):
    """Base error for remote API failures."""


class RemoteUnavailable(RemoteError):
    """Temporary failure (network, 5xx, rate limit). Never implies deletion."""


class RemoteNotFound(RemoteError):
    """The remote says the object does not exist (purged, never existed, or hidden)."""


class UnsupportedSoftware(RemoteError):
    pass


class RemoteAuthError(RemoteError):
    """Login failed, or a stored session is no longer accepted."""

    def __init__(self, message: str, code: str = ""):
        super().__init__(message)
        self.code = code


class RemoteRejected(RemoteError):
    """The server refused a write (banned, locked thread, rate limit, bad input...)."""

    def __init__(self, message: str, code: str = ""):
        super().__init__(message)
        self.code = code


@dataclass
class NActor:
    ap_id: str
    username: str
    domain: str
    display_name: str | None = None


@dataclass
class NCommunity:
    ap_id: str
    name: str
    domain: str
    title: str | None = None
    local_id: str | None = None
    removed: bool = False
    deleted: bool = False
    description: str | None = None
    moderator_ap_ids: list[str] | None = None


@dataclass
class NPost:
    ap_id: str
    local_id: str
    title: str
    body: str | None
    url: str | None
    created_at: str | None
    updated_at: str | None
    deleted: bool
    removed: bool
    locked: bool
    community: NCommunity
    author: NActor
    metadata: dict[str, Any] = field(default_factory=dict)
    score: int | None = None
    comment_count: int | None = None
    thumbnail_url: str | None = None  # server-generated preview (e.g. from og:image)
    upvotes: int | None = None
    downvotes: int | None = None
    featured: bool = False  # pinned in the community or instance; sorts first regardless of age
    newest_comment_at: str | None = None  # from the server's counts; cheap change detector


@dataclass
class NComment:
    ap_id: str
    local_id: str
    parent_local_id: str | None  # None => top-level reply to the post
    body: str | None
    created_at: str | None
    updated_at: str | None
    deleted: bool
    removed: bool
    author: NActor
    metadata: dict[str, Any] = field(default_factory=dict)
    score: int | None = None
    reply_count: int | None = None
    upvotes: int | None = None
    downvotes: int | None = None


@dataclass
class ModAction:
    """One moderation log entry relevant to an object.

    attribution: 'moderator' | 'admin' | 'unknown'  (never guessed)
    """

    kind: str  # remove_post | remove_comment | lock_post | purge_post | purge_comment
    active: bool  # removed/locked = True, restored/unlocked = False
    when: str | None
    reason: str | None
    moderator: NActor | None
    attribution: str = "unknown"


@dataclass
class ThreadRef:
    """A parsed reference to a post (or comment inside a post) on one server."""

    domain: str
    kind: str  # 'post' | 'comment'
    local_id: str


@dataclass
class CommunityRef:
    domain: str  # server to query
    name: str  # local name
    home: str | None  # community's home instance if given (name@home)

    @property
    def qualified(self) -> str:
        return f"{self.name}@{self.home}" if self.home and self.home != self.domain else self.name


_POST_PATTERNS = [
    re.compile(r"^/post/(\d+)(?:/(\d+))?/?"),  # lemmy /post/1  /post/1/2 (comment context)
    re.compile(r"^/c/[^/]+/p/(\d+)(?:/[^/]*)?/?"),  # piefed /c/name/p/1/slug
]
_COMMENT_PATTERNS = [re.compile(r"^/comment/(\d+)/?"), re.compile(r"^/post/\d+/comment/(\d+)")]


def parse_thread_url(url: str) -> ThreadRef:
    url = url.strip()
    if "://" not in url:
        url = "https://" + url
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("Not an http(s) URL")
    domain = parsed.hostname.lower()
    if parsed.port:
        domain = f"{domain}:{parsed.port}"
    path = parsed.path or "/"
    for pat in _COMMENT_PATTERNS:
        m = pat.match(path)
        if m:
            return ThreadRef(domain, "comment", m.group(1))
    for pat in _POST_PATTERNS:
        m = pat.match(path)
        if m:
            return ThreadRef(domain, "post", m.group(1))
    raise ValueError(f"Unrecognised post URL path: {path}")


def parse_community_ref(text: str) -> CommunityRef:
    """Accepts !name@host, name@host, https://host/c/name, https://host/c/name@home."""
    text = text.strip()
    if text.startswith("!"):
        text = text[1:]
    if "://" in text or text.startswith(("/", "www.")) or ("/c/" in text):
        u = text if "://" in text else "https://" + text
        parsed = urlparse(u)
        m = re.match(r"^/(?:c|m)/([^/?#]+)", parsed.path or "")
        if not parsed.hostname or not m:
            raise ValueError("Not a community URL (expected https://host/c/name)")
        domain = parsed.hostname.lower()
        name = m.group(1)
        if "@" in name:
            name, home = name.split("@", 1)
            return CommunityRef(domain, name, home.lower())
        return CommunityRef(domain, name, domain)
    if "@" in text:
        name, home = text.split("@", 1)
        if not name or not home:
            raise ValueError("Expected !name@host")
        return CommunityRef(home.lower(), name, home.lower())
    raise ValueError("Expected !name@host or a community URL")


def host_of(ap_id: str) -> str:
    return (urlparse(ap_id).hostname or "").lower()


class ThreadiverseAdapter(ABC):
    software: str = "unknown"

    def __init__(self, domain: str):
        self.domain = domain

    @abstractmethod
    def resolve_url(self, ref: ThreadRef) -> str:
        """Return the local post id on this server for a post/comment reference."""

    @abstractmethod
    def resolve_ap_id(self, ap_id: str) -> str | None:
        """Local post id on this server for a canonical post ActivityPub id, if known."""

    @abstractmethod
    def fetch_post(self, local_id: str) -> NPost: ...

    @abstractmethod
    def fetch_comments(self, post_local_id: str) -> list[NComment]:
        """Return the complete currently-visible comment list or raise."""

    @abstractmethod
    def fetch_community(self, ref: CommunityRef) -> NCommunity: ...

    @abstractmethod
    def list_community_posts(
        self, ref: CommunityRef, sort: str = "New", page: int = 1, limit: int = 20
    ) -> list[NPost]: ...

    def fetch_moderation_state(
        self, *, post_local_id: str | None = None, comment_local_id: str | None = None,
        community: NCommunity | None = None,
    ) -> list[ModAction]:
        return []

    # -- acting as an account (all take the account's session token) --------
    def _unsupported(self, *_a: Any, **_k: Any) -> Any:
        raise UnsupportedSoftware(f"{self.software} adapter can't act as an account")

    login = whoami = logout = resolve_as = my_roles = create_community = _unsupported
    # moderation (community moderators / admins)
    resolve_person = community_moderators = set_moderator = _unsupported
    ban_from_community = community_bans = remove_post = remove_comment = _unsupported
    lock_post = feature_post = site_ban = site_banned = admin_settings = update_site = _unsupported
    create_post = edit_post = delete_post = vote_post = _unsupported
    create_comment = edit_comment = delete_comment = vote_comment = _unsupported
