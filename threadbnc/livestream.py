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
from urllib.parse import parse_qs, quote, urlparse

from .db import fmt_ts, parse_ts, utcnow
from .youtube import _ID as _VIDEO_ID
from .youtube import is_youtube_host

LIVE_HREF = "/live/{}/{}"  # the box a livestream link opens (web.py, app.js)
OWNCAST_RECHECK = timedelta(days=30)
LIVE_RECHECK = timedelta(minutes=5)
LIVE_SHOWN = timedelta(minutes=10)  # a live answer is shown this long, so a late recheck doesn't hide it
UNKNOWN_RECHECK = timedelta(minutes=15)
LIVE_REQUEST_WINDOW = timedelta(minutes=30)
REQUEST_REFRESH = timedelta(minutes=10)  # a request is renewed this often, well inside its window

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


@dataclass(frozen=True)
class OwncastLive:
    live: bool
    title: str | None = None
    viewer_count: int | None = None
    video_id: str | None = None
    thumbnail_url: str | None = None


def parse_owncast_live(data: Any) -> OwncastLive | None:
    if not owncast_status_ok(data):
        return None
    count = data.get("viewerCount")
    viewers = count if isinstance(count, int) and not isinstance(count, bool) and count >= 0 else None
    title = data.get("streamTitle")
    thumbnail = data.get("thumbnailUrl")
    return OwncastLive(data["online"] is True, title if isinstance(title, str) else None,
                       viewers if data["online"] is True else None, None,
                       thumbnail if isinstance(thumbnail, str) and thumbnail.startswith("https://") else None)


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


def followed_youtube(conn: Any) -> list[dict[str, str]]:
    """Active YouTube channel follows, including older /user/ feed addresses."""
    rows = conn.execute("SELECT c.name, c.canonical_ap_id FROM community_follows f "
                        "JOIN communities c ON c.id=f.community_id WHERE f.active=1 "
                        "AND c.canonical_ap_id LIKE ?", ("rss:https://www.youtube.com/feeds/videos.xml%",))
    out = []
    for row in rows:
        feed = urlparse(row["canonical_ap_id"][4:])
        q = parse_qs(feed.query)
        channel = (q.get("channel_id") or [""])[0]
        user = (q.get("user") or [""])[0]
        if re.fullmatch(_CHANNEL_ID, channel):
            out.append({"kind": "youtube-channel", "key": channel, "name": row["name"]})
        elif re.fullmatch(r"[\w.-]+", user):
            out.append({"kind": "youtube-user", "key": user, "name": row["name"]})
    return out


def _checkable(kind: str, key: str) -> bool:
    return bool(kind == "youtube-video" and re.fullmatch(_VIDEO_ID, key) or
                kind == "youtube-channel" and re.fullmatch(_CHANNEL_ID, key) or
                kind == "youtube-user" and re.fullmatch(r"[\w.-]+", key) or
                kind == "owncast" and is_host(key) or
                kind == "twitch" and _TWITCH_NAME.fullmatch(key))


def register_live_checks(conn: Any, items: list[tuple[str, str]]) -> None:
    """Ask for these to be checked while the Live page is in use. A page names
    thousands, most asked for a moment ago: only those not asked for in the
    last REQUEST_REFRESH are written, in one go."""
    now = utcnow()
    refresh_before = fmt_ts(parse_ts(now) - REQUEST_REFRESH)
    wanted = {(kind, key) for kind, key in items if _checkable(kind, key)}
    if not wanted:
        return
    recent = {(r["kind"], r["key"]) for r in conn.execute(
        "SELECT kind, key FROM live_checks WHERE requested_at>?", (refresh_before,))}
    conn.executemany("INSERT INTO live_checks(kind, key, requested_at) VALUES (?, ?, ?) "
                     "ON CONFLICT(kind, key) DO UPDATE SET requested_at=excluded.requested_at "
                     "WHERE live_checks.requested_at IS NULL OR live_checks.requested_at<=?",
                     [(kind, key, now, refresh_before) for kind, key in sorted(wanted - recent)])


_DUE = ("requested_at>=? AND (checked_at IS NULL OR (status='unknown' AND checked_at<=?) OR "
        "(status<>'unknown' AND checked_at<=?))")


def _due_params(now: str) -> tuple[str, str, str]:
    moment = parse_ts(now)
    return (fmt_ts(moment - LIVE_REQUEST_WINDOW), fmt_ts(moment - UNKNOWN_RECHECK),
            fmt_ts(moment - LIVE_RECHECK))


def due_live_check(conn: Any, now: str) -> tuple[str, str] | None:
    """Any check that's due: whether the Live page is still waiting on some."""
    row = conn.execute(f"SELECT kind, key FROM live_checks WHERE {_DUE} "
                       "ORDER BY COALESCE(checked_at, ''), key LIMIT 1", _due_params(now)).fetchone()
    return (row["kind"], row["key"]) if row else None


def due_live_checks(conn: Any, now: str, limits: dict[str, int]) -> dict[str, list[str]]:
    """The checks due, longest waiting first, up to limits[kind] of each kind.
    Each kind gets its own turn, so a long queue of one (Twitch links from the
    firehoses) can't keep the others (YouTube, Owncast) from being checked."""
    out = {}
    for kind, limit in limits.items():
        if limit > 0:
            keys = [r["key"] for r in conn.execute(
                f"SELECT key FROM live_checks WHERE kind=? AND {_DUE} "
                "ORDER BY COALESCE(checked_at, ''), key LIMIT ?", (kind, *_due_params(now), limit))]
            if keys:
                out[kind] = keys
    return out


def save_live_check(conn: Any, kind: str, key: str, result: Any, now: str,
                    error: str | None = None) -> None:
    conn.execute("INSERT INTO live_checks(kind, key, status, video_id, title, viewer_count, thumbnail_url, checked_at, error) "
                 "VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(kind, key) DO UPDATE SET "
                 "status=excluded.status, video_id=excluded.video_id, title=excluded.title, "
                 "viewer_count=excluded.viewer_count, thumbnail_url=excluded.thumbnail_url, "
                 "checked_at=excluded.checked_at, error=excluded.error",
                 (kind, key, "live" if result and result.live else "offline" if result else "unknown",
                  getattr(result, "video_id", None), getattr(result, "title", None),
                  getattr(result, "viewer_count", None), getattr(result, "thumbnail_url", None), now, error))


def current_statuses(conn: Any, now: str) -> dict[tuple[str, str], dict[str, Any]]:
    """Only recently verified live results needed by the Live page."""
    cutoff = fmt_ts(parse_ts(now) - LIVE_SHOWN)
    return {(r["kind"], r["key"]): dict(r) for r in conn.execute(
        "SELECT * FROM live_checks WHERE status='live' AND checked_at>=?", (cutoff,))}


def followed_live(conn: Any, now: str, query: str = "") -> list[dict[str, Any]]:
    statuses = current_statuses(conn, now)
    needle = query.strip().casefold()
    out = []
    for follow in followed_youtube(conn):
        status = statuses.get((follow["kind"], follow["key"]))
        if not status or status["status"] != "live":
            continue
        vid = status["video_id"]
        if not vid or not re.fullmatch(_VIDEO_ID, vid):
            continue
        title = status["title"] or follow["name"]
        if needle and needle not in f"{title} {follow['name']}".casefold():
            continue
        out.append({"name": follow["name"], "title": title, "stream": Stream("youtube", vid),
                    "checked_at": status["checked_at"], "viewer_count": status["viewer_count"],
                    "thumbnail": f"/live/thumbnail/youtube/{vid}"})
    return out
