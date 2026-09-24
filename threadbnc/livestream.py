"""Livestreams: links to a Twitch channel, a YouTube live stream, or an Owncast
server open a box with the stream's own player, under the link (app.js).

Twitch and YouTube links are known by their address. An Owncast server is
just a site's front page, so a link to one isn't: once a page with links to
sites' front pages is shown, app.js asks, and each site not asked lately is
asked once for Owncast's /api/status (bouncer.owncast_hosts). The answer is
remembered for a month, found or not.

Nothing is fetched from Twitch or YouTube here; the player is theirs, in an
iframe, and only once the box is opened.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from urllib.parse import quote, urlparse

from .db import parse_ts
from .youtube import _ID as _VIDEO_ID
from .youtube import is_youtube_host

LIVE_HREF = "/live/{}/{}"  # the box a livestream link opens (web.py, app.js)
OWNCAST_RECHECK = timedelta(days=30)

_CHANNEL_ID = r"UC[A-Za-z0-9_-]{22}"
_TWITCH_NAME = re.compile(r"^[A-Za-z0-9_]{2,25}$")
_TWITCH_VIDEO = re.compile(r"^[0-9]{1,15}$")
# Twitch's own pages, not channels: twitch.tv/<these>.
_TWITCH_PAGES = {
    "directory", "videos", "p", "settings", "subscriptions", "inventory", "wallet", "drops", "downloads",
    "jobs", "turbo", "prime", "search", "login", "signup", "messages", "friends", "following", "store", "bits",
    "products", "team", "collections", "moderator", "popout", "embed", "broadcast", "creatorcamp", "privacy",
    "legal", "security", "partners", "user", "clip", "clips", "event", "events", "u", "dashboard",
}
_HOST = re.compile(r"^(?=.{1,253}(?::\d{1,5})?$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
                   r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+(?::\d{1,5})?$")


@dataclass(frozen=True)
class Stream:
    kind: str  # youtube, twitch, twitch-video, owncast
    key: str   # a video or channel id, a channel's name, a VOD's number, a host

    @property
    def href(self) -> str:
        return LIVE_HREF.format(self.kind, self.key)

    @property
    def site(self) -> str:
        return {"youtube": "YouTube", "owncast": "Owncast"}.get(self.kind, "Twitch")

    @property
    def is_channel(self) -> bool:
        """A channel's live stream, whatever is on now (not one video)."""
        return self.kind in ("twitch", "owncast") or bool(re.fullmatch(_CHANNEL_ID, self.key))

    @property
    def page(self) -> str:
        """Where it's watched on its own site."""
        if self.kind == "youtube":
            return (f"https://www.youtube.com/channel/{self.key}/live" if self.is_channel
                    else f"https://www.youtube.com/watch?v={self.key}")
        if self.kind == "twitch":
            return f"https://www.twitch.tv/{self.key}"
        if self.kind == "twitch-video":
            return f"https://www.twitch.tv/videos/{self.key}"
        return f"https://{self.key}/"

    @property
    def embed(self) -> str:
        """The player's address. Twitch's also needs `&parent=` this site's
        host name, which app.js adds (it knows the name it's reached by)."""
        if self.kind == "youtube":
            if self.is_channel:
                return f"https://www.youtube-nocookie.com/embed/live_stream?channel={self.key}&autoplay=1"
            return f"https://www.youtube-nocookie.com/embed/{self.key}?autoplay=1"
        if self.kind == "twitch":
            return f"https://player.twitch.tv/?channel={quote(self.key)}&autoplay=true"
        if self.kind == "twitch-video":
            return f"https://player.twitch.tv/?video={self.key}&autoplay=true"
        return f"https://{self.key}/embed/video"

    @property
    def needs_parent(self) -> bool:
        return self.kind.startswith("twitch")

    @property
    def link_title(self) -> str:
        return f"{self.site} {'live stream' if self.is_channel else 'video'}: watch it here"


def from_key(kind: str, key: str) -> Stream | None:
    """A box's stream from its address (/live/<kind>/<key>), if it's one."""
    if kind == "youtube" and (re.fullmatch(_VIDEO_ID, key) or re.fullmatch(_CHANNEL_ID, key)):
        return Stream(kind, key)
    if kind == "twitch" and _TWITCH_NAME.match(key) and key.lower() not in _TWITCH_PAGES:
        return Stream(kind, key.lower())
    if kind == "twitch-video" and _TWITCH_VIDEO.match(key):
        return Stream(kind, key)
    if kind == "owncast" and is_host(key):
        return Stream(kind, key.lower())
    return None


def stream_of(url: str | None) -> Stream | None:
    """The livestream a link is to, if its address says so: a YouTube /live/
    link or channel's /live page, a Twitch channel or VOD. (Owncast servers
    are only known once asked; see bouncer.owncast_hosts.)"""
    if not url:
        return None
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        return None
    host = u.hostname.lower()
    parts = [p for p in u.path.split("/") if p]
    if is_youtube_host(host):
        if len(parts) == 2 and parts[0] == "live" and re.fullmatch(_VIDEO_ID, parts[1]):
            return Stream("youtube", parts[1])
        if len(parts) == 3 and parts[0] == "channel" and parts[2] == "live" and re.fullmatch(_CHANNEL_ID, parts[1]):
            return Stream("youtube", parts[1])
        return None
    if host in ("twitch.tv", "www.twitch.tv", "m.twitch.tv", "go.twitch.tv"):
        if len(parts) == 1:
            return from_key("twitch", parts[0])
        if len(parts) == 2 and parts[0] == "videos":
            return from_key("twitch-video", parts[1])
    return None


def is_host(host: str) -> bool:
    return bool(_HOST.match(host.lower()))


def owncast_status_ok(data: Any) -> bool:
    """Whether /api/status's answer is Owncast's."""
    return isinstance(data, dict) and "online" in data and "versionNumber" in data


def owncast_known(conn: Any, hosts: list[str]) -> dict[str, bool]:
    """What's known of these hosts: Owncast or not."""
    if not hosts:
        return {}
    rows = conn.execute(f"SELECT host, is_owncast FROM owncast_hosts WHERE host IN ({','.join('?' * len(hosts))})",
                        hosts)
    return {r["host"]: bool(r["is_owncast"]) for r in rows}


def owncast_to_ask(conn: Any, hosts: list[str], now: str) -> list[str]:
    """Of these, the hosts not asked in the last month."""
    if not hosts:
        return []
    cutoff = parse_ts(now) - OWNCAST_RECHECK
    rows = conn.execute(f"SELECT host, checked_at FROM owncast_hosts WHERE host IN ({','.join('?' * len(hosts))})",
                        hosts)
    recent = {r["host"] for r in rows if (parse_ts(r["checked_at"]) or cutoff) > cutoff}
    return [h for h in hosts if h not in recent]


def save_owncast(conn: Any, host: str, is_owncast: bool, now: str) -> None:
    conn.execute("INSERT INTO owncast_hosts(host, is_owncast, checked_at) VALUES (?,?,?) "
                 "ON CONFLICT(host) DO UPDATE SET is_owncast=excluded.is_owncast, checked_at=excluded.checked_at",
                 (host, 1 if is_owncast else 0, now))
