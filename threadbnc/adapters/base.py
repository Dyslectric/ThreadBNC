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


class CommentList(list):
    """fetch_comments' result. `complete` is False when the server only returned
    part of the tree (Reddit's "load more comments"), so comments that aren't in
    it mustn't be taken as gone."""

    complete: bool = True


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
    visibility: str | None = None  # Lemmy 1.0: public | unlisted | local_only_* | private


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
class NInboxItem:
    """A reply, mention or private message to the logged-in account. Ids are
    the account's server's own; `remote_id` is what marking it read takes."""

    kind: str  # reply | mention | message
    remote_id: str
    unread: bool
    author: NActor
    body: str | None
    created_at: str | None
    object_type: str  # comment | post | message
    object_ap_id: str | None = None
    object_local_id: str | None = None
    author_local_id: str | None = None
    deleted: bool = False
    subject: str | None = None
    post_ap_id: str | None = None
    post_local_id: str | None = None
    post_title: str | None = None
    community: NCommunity | None = None


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


# Reddit isn't federated: every reddit.com host (www., old., np., m.) and redd.it
# links are one "server", REDDIT_DOMAIN.
REDDIT_DOMAIN = "reddit.com"
_REDDIT_POST = [re.compile(r"^/(?:r/[^/]+/)?comments/([a-z0-9]+)", re.I), re.compile(r"^/gallery/([a-z0-9]+)", re.I)]
_REDDIT_SHARE = re.compile(r"^/r/[^/]+/s/[A-Za-z0-9]+/?$")
_SUBREDDIT = re.compile(r"^/?r/([A-Za-z0-9_]{2,21})/?$")


# RSS and Atom feeds live on a pseudo server; their communities, posts and
# authors have ids starting with RSS_PREFIX (see adapters/rss.py).
RSS_DOMAIN = "rss"
RSS_PREFIX = "rss:"


def is_rss(ap_id: str | None) -> bool:
    return (ap_id or "").startswith(RSS_PREFIX)


def is_reddit_host(host: str | None) -> bool:
    host = (host or "").lower().split(":")[0]
    return host in ("reddit.com", "redd.it") or host.endswith(".reddit.com")


def _reddit_thread_ref(host: str, path: str) -> ThreadRef:
    if host == "redd.it" and re.fullmatch(r"/[a-z0-9]+/?", path, re.I):
        return ThreadRef(REDDIT_DOMAIN, "post", path.strip("/").lower())
    for pat in _REDDIT_POST:
        m = pat.match(path)
        if m:
            return ThreadRef(REDDIT_DOMAIN, "post", m.group(1).lower())
    if _REDDIT_SHARE.match(path):  # the app's share links: /r/name/s/code redirects to the post
        return ThreadRef(REDDIT_DOMAIN, "share", path)
    raise ValueError(f"Unrecognised Reddit post URL path: {path}")


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
    path = parsed.path or "/"
    if is_reddit_host(domain):
        return _reddit_thread_ref(domain, path)
    if parsed.port:
        domain = f"{domain}:{parsed.port}"
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
    """Accepts !name@host, name@host, https://host/c/name, https://host/c/name@home,
    subreddits: r/name or https://www.reddit.com/r/name, and feeds: rss:<url>, or
    any other web address (a feed, or a page that links to one)."""
    text = text.strip()
    if text.lower().startswith(RSS_PREFIX):
        return CommunityRef(RSS_DOMAIN, text[len(RSS_PREFIX):].strip(), RSS_DOMAIN)
    if text.startswith("!"):
        text = text[1:]
    m = _SUBREDDIT.match(text)
    if m:
        return CommunityRef(REDDIT_DOMAIN, m.group(1), REDDIT_DOMAIN)
    if "reddit.com/" in text.lower():
        parsed = urlparse(text if "://" in text else "https://" + text)
        m = re.match(r"^/r/([A-Za-z0-9_]{2,21})(?:/|$)", parsed.path or "")
        if is_reddit_host(parsed.hostname) and m:
            return CommunityRef(REDDIT_DOMAIN, m.group(1), REDDIT_DOMAIN)
        if is_reddit_host(parsed.hostname):
            raise ValueError("Not a subreddit URL (expected https://www.reddit.com/r/name)")
    if "://" in text or text.startswith(("/", "www.")) or ("/c/" in text):
        u = text if "://" in text else "https://" + text
        parsed = urlparse(u)
        m = re.match(r"^/(?:c|m)/([^/?#]+)", parsed.path or "")
        if not parsed.hostname:
            raise ValueError("Not a community URL (expected https://host/c/name)")
        if not m:  # not a Lemmy/PieFed community page: try it as a feed
            return CommunityRef(RSS_DOMAIN, u, RSS_DOMAIN)
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
    # the account's inbox: inbox(token, me_ap_id), mark_inbox_read(token, kind, remote_id, read),
    # mark_all_inbox_read(token, [(kind, remote_id)]), send_message(token, recipient_local_id, body, in_reply_to)
    inbox = mark_inbox_read = mark_all_inbox_read = send_message = _unsupported
