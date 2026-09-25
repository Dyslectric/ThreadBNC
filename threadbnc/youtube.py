"""YouTube: channels followed like feeds, and videos saved with yt-dlp when kept.

A followed channel or playlist is known by its Atom feed's address
(https://www.youtube.com/feeds/videos.xml?channel_id=…), but YouTube's feeds
have been unreliable (404s for every channel for days at a time), so what's on
it is read from the channel's Videos tab or the playlist's page instead: each
video's id, title and "2 weeks ago", from the data the page carries
(`parse_page`). Like subreddits, these pages are only checked while you're
using ThreadBNC, one at a time and spread out (bouncer.py). Channel and
playlist links are turned into the feed address with no request. An @handle
or /c/ link is fetched once, when you follow it, to learn the channel's id.

Videos are big, so they're only downloaded for posts you keep: not when a
post is merely opened, and never in the background for the rest. yt-dlp does
the downloading. YouTube increasingly asks for a signed-in session before it
serves video (its "confirm you're not a bot" check). The YouTube page takes
your browser's youtube.com cookies, and optionally a PO token. They're
encrypted like account tokens (see vault.py) and only used for these
downloads.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .db import Database, utcnow
from .render import escape_markdown, plain_lines
from .vault import TokenVault, VaultError

FEED = "https://www.youtube.com/feeds/videos.xml"
FEED_PREFIX = "rss:" + FEED  # canonical ids of followed YouTube feeds
SETTING = "youtube_session"
HOSTS = ("youtube.com", "youtu.be", "youtube-nocookie.com")
DEFAULT_MAX_MB = 2000
DEFAULT_MAX_HEIGHT = 1080
HEIGHTS = (360, 480, 720, 1080, 1440, 2160)
# Sent with the one page fetch that turns an @handle into a channel id, so
# European visitors get the page rather than the cookie consent interstitial.
CONSENT_COOKIE = "SOCS=CAI"

_ID = r"[A-Za-z0-9_-]{11}"


def is_youtube_host(host: str | None) -> bool:
    host = (host or "").lower()
    return any(host == h or host.endswith("." + h) for h in HOSTS)


def is_youtube_feed(ap_id: str | None) -> bool:
    """A followed community that's a YouTube channel or playlist."""
    return (ap_id or "").startswith(FEED_PREFIX)


def feed_url(url: str) -> str | None:
    """The feed for a channel or playlist link, when the link itself says which
    (no request needed). None for anything else, @handles included."""
    u = urlparse(url if "://" in url else "https://" + url)
    if not is_youtube_host(u.hostname):
        return None
    q = parse_qs(u.query)
    path = u.path.rstrip("/")
    if path == "/feeds/videos.xml":
        return url
    if q.get("list") and (path in ("/playlist", "/watch") or not path):
        return f"{FEED}?playlist_id={q['list'][0]}"
    m = re.match(r"^/channel/(UC[A-Za-z0-9_-]{22})(?:/|$)", u.path)
    if m:
        return f"{FEED}?channel_id={m.group(1)}"
    m = re.match(r"^/user/([A-Za-z0-9_.-]+)(?:/|$)", u.path)
    if m:
        return f"{FEED}?user={m.group(1)}"
    return None


def channel_page(url: str) -> str | None:
    """The page to read a channel id from: an @handle or /c/ link, trimmed to
    the channel's home (not its /videos tab etc.)."""
    u = urlparse(url if "://" in url else "https://" + url)
    if not is_youtube_host(u.hostname):
        return None
    m = re.match(r"^/(@[^/?#]+|c/[^/?#]+)", u.path)
    return f"https://www.youtube.com/{m.group(1)}" if m else None


def channel_id_in(page: str) -> str | None:
    """The channel id a channel page is about. Its own markers first: the page
    also mentions other channels (featured, related)."""
    for pattern in (r'<link rel="canonical" href="https://www\.youtube\.com/channel/(UC[\w-]{22})"',
                    r'<meta itemprop="identifier" content="(UC[\w-]{22})"',
                    r'"externalId":"(UC[\w-]{22})"',
                    r'feeds/videos\.xml\?channel_id=(UC[\w-]{22})',
                    r'"channelId":"(UC[\w-]{22})"'):
        m = re.search(pattern, page)
        if m:
            return m.group(1)
    return None


def page_url(feed: str) -> str | None:
    """The page listing what a YouTube feed would: the channel's Videos tab, or
    the playlist. None for anything that isn't a YouTube feed."""
    u = urlparse(feed)
    if not is_youtube_host(u.hostname) or u.path != "/feeds/videos.xml":
        return None
    q = {k: v[0] for k, v in parse_qs(u.query).items()}
    if re.fullmatch(r"UC[\w-]{22}", q.get("channel_id", "")):
        return f"https://www.youtube.com/channel/{q['channel_id']}/videos"
    if re.fullmatch(r"[\w-]+", q.get("playlist_id", "")):
        return f"https://www.youtube.com/playlist?list={q['playlist_id']}"
    if re.fullmatch(r"[\w.-]+", q.get("user", "")):
        return f"https://www.youtube.com/user/{q['user']}/videos"
    return None


@dataclass
class PageVideo:
    id: str
    title: str
    published: datetime  # approximate: the page only says "2 weeks ago"
    author: str | None


@dataclass
class Page:
    title: str
    link: str
    description: str | None
    videos: list[PageVideo]


_AGO = re.compile(r"(\d+)\s*(second|minute|hour|day|week|month|year)s?\s+ago", re.I)
_UNIT = {"second": 1, "minute": 60, "hour": 3600, "day": 86400, "week": 7 * 86400, "month": 30 * 86400,
         "year": 365 * 86400}


def _ago(text: str, now: datetime) -> datetime | None:
    m = _AGO.search(text or "")
    return now - timedelta(seconds=int(m.group(1)) * _UNIT[m.group(2).lower()]) if m else None


def _initial_data(html: str) -> dict[str, Any] | None:
    m = re.search(r"(?:var ytInitialData|window\[\"ytInitialData\"\])\s*=\s*", html)
    if not m:
        return None
    try:
        data, _ = json.JSONDecoder().raw_decode(html, m.end())
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _text(o: Any) -> str:
    """YouTube's text shapes: {"content"}, {"simpleText"} or {"runs": [...]}."""
    if not isinstance(o, dict):
        return ""
    if "content" in o:
        return str(o["content"])
    if "simpleText" in o:
        return str(o["simpleText"])
    return "".join(str(r.get("text", "")) for r in o.get("runs", []) if isinstance(r, dict))


def _walk(o: Any, keys: set[str]):
    """Every (key, value) under `o` whose key is one of `keys`, in page order."""
    stack: list[tuple[str | None, Any]] = [(None, o)]
    while stack:
        key, cur = stack.pop()
        if key in keys:
            yield key, cur
        if isinstance(cur, dict):
            stack.extend(reversed(list(cur.items())))
        elif isinstance(cur, list):
            stack.extend((None, v) for v in reversed(cur))


def _lockup(v: dict[str, Any]) -> tuple[str, str, list[str], str | None] | None:
    """(id, title, metadata texts, channel) from a lockupViewModel (the layout since 2025)."""
    if v.get("contentType") != "LOCKUP_CONTENT_TYPE_VIDEO" or not v.get("contentId"):
        return None
    md = v.get("metadata", {}).get("lockupMetadataViewModel", {})
    texts = []
    for row in md.get("metadata", {}).get("contentMetadataViewModel", {}).get("metadataRows", []):
        for part in row.get("metadataParts", []):
            texts.append(part.get("accessibilityLabel") or _text(part.get("text")))
    label = md.get("image", {}).get("decoratedAvatarViewModel", {}).get("a11yLabel", "")
    channel = label[len("Go to channel "):] if label.startswith("Go to channel ") else None
    return v["contentId"], _text(md.get("title")), texts, channel


def _renderer(v: dict[str, Any]) -> tuple[str, str, list[str], str | None] | None:
    """The same from the older videoRenderer family, which some pages still use."""
    if not v.get("videoId"):
        return None
    texts = [_text(v.get(k)) for k in ("publishedTimeText", "videoInfo")]
    owner = _text(v.get("shortBylineText") or v.get("ownerText")) or None
    return v["videoId"], _text(v.get("title")) or _text(v.get("headline")), texts, owner


def parse_page(html: str, url: str, now: datetime | None = None) -> Page | None:
    """A channel's Videos tab or a playlist page: its name and the videos on it,
    newest first. Videos not out yet (premieres, live now) are left out until
    they are. None if the page has no data in it."""
    data = _initial_data(html)
    if data is None:
        return None
    now = now or datetime.now(timezone.utc)
    meta = data.get("metadata", {})
    info = meta.get("channelMetadataRenderer") or meta.get("playlistMetadataRenderer") or {}
    title = info.get("title") or ""
    link = info.get("channelUrl") or url
    owner = title if "channelMetadataRenderer" in meta else None
    videos: list[PageVideo] = []
    seen: set[str] = set()
    for key, v in _walk(data, {"lockupViewModel", "videoRenderer", "gridVideoRenderer", "playlistVideoRenderer"}):
        got = _lockup(v) if key == "lockupViewModel" else _renderer(v)
        if not got or got[0] in seen or not re.fullmatch(_ID, got[0]):
            continue
        vid, name, texts, channel = got
        when = next((w for w in (_ago(t, now) for t in texts) if w), None)
        if when is None:
            continue
        seen.add(vid)
        # Keep the page's order among videos that say the same "2 weeks ago".
        videos.append(PageVideo(vid, name or vid, when - timedelta(seconds=len(videos)), channel or owner))
    if "playlistMetadataRenderer" not in meta:
        videos.sort(key=lambda p: p.published, reverse=True)
    return Page(title, link, info.get("description") or None, videos)


@dataclass
class Watch:
    description: str | None  # as Markdown (description_markdown)
    likes: int | None
    # What its post needs when it's kept from a link rather than a followed channel.
    title: str | None = None
    channel: str | None = None
    channel_id: str | None = None
    published: str | None = None  # as the page says it: "2026-09-01T07:00:00-07:00", or just the day


def _player_response(html: str) -> dict[str, Any] | None:
    m = re.search(r"(?:var ytInitialPlayerResponse|window\[\"ytInitialPlayerResponse\"\])\s*=\s*", html)
    if not m:
        return None
    try:
        data, _ = json.JSONDecoder().raw_decode(html, m.end())
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


_LIKES_LABEL = re.compile(r"along with ([\d,.\s]+) other", re.I)


def _likes(data: dict[str, Any] | None, html: str) -> int | None:
    """The like count: the like button's own number, else its label ("like
    this video along with 1,234 other people"). None when likes are hidden."""
    for _, v in _walk(data or {}, {"likeCountIfIndifferentNumber", "likeCount"}):
        if isinstance(v, (str, int)) and str(v).isdigit():
            return int(v)
    m = _LIKES_LABEL.search(html)
    digits = re.sub(r"\D", "", m.group(1)) if m else ""
    return int(digits) if digits else None


def parse_watch(html: str) -> Watch | None:
    """A video's page: its description and likes, title, channel and when it
    came out. None if the page has no data in it (YouTube may have changed
    it, or asked for a sign-in)."""
    player = _player_response(html)
    data = _initial_data(html)
    if player is None and data is None:
        return None
    details = (player or {}).get("videoDetails") or {}
    micro = ((player or {}).get("microformat") or {}).get("playerMicroformatRenderer") or {}
    text = details.get("shortDescription")  # the whole of it, despite the name

    def s(v: Any) -> str | None:
        return (v.strip() or None) if isinstance(v, str) else None

    channel_id = s(details.get("channelId")) or s(micro.get("externalChannelId"))
    return Watch(description_markdown(text) if isinstance(text, str) else None, _likes(data, html),
                 title=s(details.get("title")), channel=s(details.get("author")) or s(micro.get("ownerChannelName")),
                 channel_id=channel_id if channel_id and re.fullmatch(r"UC[\w-]{22}", channel_id) else None,
                 published=s(micro.get("publishDate")) or s(micro.get("uploadDate")))


_URL = re.compile(r"https?://[^\s<>\"]+[^\s<>\".,;:!?)\]'’]")


def description_markdown(text: str | None) -> str | None:
    """A video description (plain text) as Markdown that shows as written: its
    line breaks kept, links made links, and nothing in it taken as formatting."""
    lines = []
    for line in (text or "").strip().splitlines():
        parts, at = [], 0
        for m in _URL.finditer(line):
            parts += [escape_markdown(line[at:m.start()]), f"<{m.group(0)}>"]
            at = m.end()
        parts.append(escape_markdown(line[at:]))
        lines.append("".join(parts))
    return plain_lines(lines)


# -- comments ---------------------------------------------------------------------
# A video's comments aren't on its page: the page carries a token, and the
# comments come from YouTube's own API (/youtubei/v1/next) a page (20) at a
# time, as the site loads them while you scroll. Each thread's replies take
# another request of their own.

NEXT_API = "https://www.youtube.com/youtubei/v1/next?prettyPrint=false"


@dataclass
class Comment:
    id: str
    parent_id: str | None  # the comment a reply is under (its id is "<parent>.<own>")
    text: str
    author: str  # "@handle"
    channel_id: str | None
    published: datetime | None  # approximate: YouTube only says "5 years ago"
    likes: int | None  # approximate over 1000: "14K"
    edited: bool


@dataclass
class CommentPage:
    comments: list[Comment]
    next: str | None  # the token for the next page
    replies: dict[str, str]  # comment id -> the token for its replies


def api_context(html: str) -> dict[str, Any] | None:
    """What the page's own requests to YouTube's API say about the client."""
    m = re.search(r'"INNERTUBE_CONTEXT"\s*:\s*', html)
    if not m:
        return None
    try:
        ctx, _ = json.JSONDecoder().raw_decode(html, m.end())
    except ValueError:
        return None
    client = (ctx or {}).get("client") or {}
    if not client.get("clientName") or not client.get("clientVersion"):
        return None
    keep = ("clientName", "clientVersion", "hl", "gl", "visitorData")
    return {"client": {k: client[k] for k in keep if k in client}}


def _token(o: Any) -> str | None:
    for _, cmd in _walk(o, {"continuationCommand"}):
        if isinstance(cmd, dict) and cmd.get("token"):
            return str(cmd["token"])
    return None


def comments_token(html: str) -> str | None:
    """The token for a video's first page of comments. None when comments are
    off (or the page has changed)."""
    for _, section in _walk(_initial_data(html) or {}, {"itemSectionRenderer"}):
        if isinstance(section, dict) and section.get("sectionIdentifier") == "comment-item-section":
            if token := _token(section.get("contents")):
                return token
    return None


_COUNT = re.compile(r"^([\d.,]+)\s*([KMB]?)$", re.I)


def _count(text: Any) -> int | None:
    """"14K" -> 14000; "" (no likes) -> 0."""
    text = str(text or "").strip()
    if not text:
        return 0
    m = _COUNT.match(text)
    if not m:
        return None
    n = float(m.group(1).replace(",", ""))
    return int(round(n * {"": 1, "K": 1e3, "M": 1e6, "B": 1e9}[m.group(2).upper()]))


def parse_comments(data: dict[str, Any], now: datetime | None = None) -> CommentPage:
    """A page of comments (or of one thread's replies) from YouTube's API."""
    now = now or datetime.now(timezone.utc)
    entities: dict[str, dict[str, Any]] = {}
    for _, payload in _walk(data.get("frameworkUpdates") or {}, {"commentEntityPayload"}):
        cid = ((payload or {}).get("properties") or {}).get("commentId")
        if cid:
            entities[cid] = payload
    order: list[str] = []
    replies: dict[str, str] = {}
    following: str | None = None
    for _, cmd in _walk(data.get("onResponseReceivedEndpoints") or [],
                        {"reloadContinuationItemsCommand", "appendContinuationItemsAction"}):
        for item in (cmd or {}).get("continuationItems") or []:
            thread = item.get("commentThreadRenderer")
            view = (thread or item).get("commentViewModel") or {}
            view = view.get("commentViewModel", view)
            if view.get("commentId"):
                order.append(view["commentId"])
                if thread and (token := _token((thread.get("replies") or {}).get("commentRepliesRenderer"))):
                    replies[view["commentId"]] = token
            elif "continuationItemRenderer" in item:
                following = _token(item["continuationItemRenderer"]) or following
    comments = []
    for cid in order:
        p = entities.get(cid)
        if p is None:
            continue
        props, author, toolbar = p.get("properties") or {}, p.get("author") or {}, p.get("toolbar") or {}
        when = str(props.get("publishedTime") or "")
        comments.append(Comment(
            id=cid, parent_id=cid.split(".")[0] if "." in cid else None,
            text=_text(props.get("content")) if isinstance(props.get("content"), dict) else str(props.get("content") or ""),
            author=author.get("displayName") or "(unknown)", channel_id=author.get("channelId"),
            published=_ago(when, now), likes=_count(toolbar.get("likeCountNotliked")), edited="edited" in when))
    return CommentPage(comments, following, replies)


def watch_url(vid: str) -> str:
    return f"https://www.youtube.com/watch?v={vid}"


def thumbnail_url(vid: str) -> str:
    """The same picture the feed gives, at an address that doesn't change."""
    return f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"


def video_id(url: str | None) -> str | None:
    """The video a link is to: watch?v=, youtu.be/, /shorts/, /live/, /embed/."""
    if not url:
        return None
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not is_youtube_host(u.hostname):
        return None
    if (u.hostname or "").lower().endswith("youtu.be"):
        m = re.match(rf"^/({_ID})$", u.path)
        return m.group(1) if m else None
    if u.path in ("/watch", "/watch/"):
        v = parse_qs(u.query).get("v", [""])[0]
        return v if re.fullmatch(_ID, v) else None
    m = re.match(rf"^/(?:shorts|live|embed|v)/({_ID})(?:[/?#]|$)", u.path)
    return m.group(1) if m else None


# -- the session ------------------------------------------------------------------

class YouTubeError(Exception):
    """Shown to the user as-is."""


def cookies_txt(text: str) -> str:
    """A Netscape cookies.txt (what yt-dlp reads) from what was pasted: a
    cookies.txt export, a Cookie header, or name=value pairs."""
    text = text.strip()
    lines = [l for l in text.splitlines() if l.strip() and not l.lstrip().startswith("#")]
    if lines and all(len(l.split("\t")) == 7 for l in lines):
        return "# Netscape HTTP Cookie File\n" + "\n".join(lines) + "\n"
    if text.lower().startswith("cookie:"):
        text = text[len("cookie:"):]
    pairs = []
    for part in re.split(r";\s*|\n", text):
        name, sep, value = part.strip().partition("=")
        if sep and name and re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name):
            pairs.append((name, value.strip()))
    if not pairs:
        return ""
    expiry = 2_000_000_000  # 2033; YouTube's own expiries aren't in a Cookie header
    rows = [f".youtube.com\tTRUE\t/\tTRUE\t{expiry}\t{n}\t{v}" for n, v in pairs]
    return "# Netscape HTTP Cookie File\n" + "\n".join(rows) + "\n"


class YouTubeSession:
    """The saved session and download settings, in app_settings."""

    def __init__(self, db: Database, vault: TokenVault):
        self.db = db
        self.vault = vault

    def config(self) -> dict[str, Any]:
        raw = self.db.get_setting(SETTING)
        return json.loads(raw) if raw else {}

    def _save(self, cfg: dict[str, Any]) -> None:
        with self.db.transaction() as conn:
            conn.execute("INSERT INTO app_settings(key, value) VALUES (?, ?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (SETTING, json.dumps(cfg)))

    def status(self) -> dict[str, Any]:
        """What the YouTube page shows; secrets left out."""
        cfg = self.config()
        return {"has_cookies": bool(cfg.get("cookies_enc")), "has_po_token": bool(cfg.get("po_token_enc")),
                "cookie_count": cfg.get("cookie_count", 0), "saved_at": cfg.get("saved_at"),
                "last_error": cfg.get("last_error"), "last_ok_at": cfg.get("last_ok_at"),
                "max_mb": cfg.get("max_mb", DEFAULT_MAX_MB), "max_height": cfg.get("max_height", DEFAULT_MAX_HEIGHT),
                "downloader": downloader_available(), "merges": bool(shutil.which("ffmpeg")),
                "js_runtimes": list(js_runtimes())}

    def save_session(self, cookies: str, po_token: str) -> None:
        cfg = self.config()
        if cookies.strip():
            jar = cookies_txt(cookies)
            if not jar:
                raise YouTubeError("That doesn't look like cookies: paste a cookies.txt export or a Cookie header.")
            if not any(f"\t{n}\t" in jar for n in ("SID", "__Secure-3PSID", "__Secure-1PSID", "LOGIN_INFO")):
                raise YouTubeError("Those cookies aren't from a signed-in youtube.com session (no SID or "
                                   "LOGIN_INFO cookie). Export them while signed in to YouTube.")
            cfg["cookies_enc"] = self.vault.encrypt(jar)
            cfg["cookie_count"] = jar.count("\n") - 1
        if po_token.strip():
            cfg["po_token_enc"] = self.vault.encrypt(po_token.strip())
        if not cookies.strip() and not po_token.strip():
            raise YouTubeError("Paste your youtube.com cookies, a PO token, or both.")
        cfg.update(saved_at=utcnow(), last_error=None)
        self._save(cfg)

    def forget(self) -> None:
        cfg = self.config()
        for k in ("cookies_enc", "po_token_enc", "cookie_count", "saved_at", "last_error", "last_ok_at"):
            cfg.pop(k, None)
        self._save(cfg)

    def save_limits(self, max_mb: int, max_height: int) -> None:
        cfg = self.config()
        cfg["max_mb"] = max(10, min(int(max_mb), 50_000))
        cfg["max_height"] = max_height if max_height in HEIGHTS else DEFAULT_MAX_HEIGHT
        self._save(cfg)

    def note(self, error: str | None) -> None:
        """The outcome of a download, for the YouTube page."""
        cfg = self.config()
        if error is None:
            cfg.update(last_error=None, last_ok_at=utcnow())
        else:
            cfg["last_error"] = error
        self._save(cfg)

    def secrets(self) -> tuple[str | None, str | None]:
        """(cookies.txt, PO token), decrypted; None where not set or unreadable."""
        cfg = self.config()
        out: list[str | None] = []
        for key in ("cookies_enc", "po_token_enc"):
            try:
                out.append(self.vault.decrypt(cfg[key]) if cfg.get(key) else None)
            except VaultError:
                out.append(None)
        return out[0], out[1]


# -- downloading ------------------------------------------------------------------------

class VideoGone(Exception):
    """Permanent: private, removed, members-only, or over the size limit."""


class VideoRetry(OSError):
    """Worth trying again later (network trouble, YouTube throttling)."""


class NeedsSession(VideoGone):
    """YouTube wants a signed-in session (or a PO token) for this video."""


def downloader_available() -> bool:
    try:
        import yt_dlp  # noqa: F401
    except ImportError:
        return False
    return True


def js_runtimes() -> dict[str, dict[str, Any]]:
    """The JavaScript runtimes yt-dlp may use to solve YouTube's player
    challenges. Without one, only a few formats (or none) can be downloaded.
    The deno that requirements.txt installs (yt-dlp's "deno" extra) first."""
    found: dict[str, dict[str, Any]] = {}
    try:
        from deno import find_deno_bin
        found["deno"] = {"path": str(find_deno_bin())}
    except Exception:  # not installed, or no binary for this platform
        pass
    for name in ("deno", "node", "bun"):
        path = shutil.which(name)
        if path and name not in found:
            found[name] = {"path": path}
    return found


_GONE = ("private video", "video unavailable", "has been removed", "members-only", "join this channel",
         "this live event", "premieres in", "is not available", "account associated with this video has been")
_SESSION = ("sign in to confirm", "not a bot", "sign in to view", "age-restricted", "confirm your age",
            "po token", "login required", "use --cookies")


def _options(session: YouTubeSession) -> dict[str, Any]:
    """yt-dlp's options for a video at the YouTube page's resolution, with
    the saved session: the same for finding its size as for downloading it."""
    height = session.status()["max_height"]
    cookies, po_token = session.secrets()
    merges = bool(shutil.which("ffmpeg"))
    opts: dict[str, Any] = {
        # Separate video and audio streams need ffmpeg to join; without it,
        # the best single file (often only 360p on YouTube now).
        "format": f"bv*[height<={height}]+ba/b[height<={height}]/b" if merges else f"b[height<={height}]/b",
        "format_sort": [f"res:{height}", "ext:mp4:m4a"],
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "retries": 3,
        "js_runtimes": js_runtimes() or {"deno": {}},
        "remote_components": set(),  # the challenge solver comes from the yt-dlp-ejs package, never fetched
    }
    if cookies:
        opts["cookiefile"] = io.StringIO(cookies)  # a stream: the session never touches the disk
    if po_token:
        opts["extractor_args"] = {"youtube": {"po_token": [po_token if "+" in po_token else f"web.gvs+{po_token}"]}}
    return opts


def _failure(exc: Exception, signed_in: bool) -> Exception:
    """What a yt-dlp DownloadError means: NeedsSession, VideoGone or VideoRetry."""
    msg = re.sub(r"^ERROR:\s*(\[[^\]]+\]\s*[\w-]+:\s*)?", "", str(exc)).strip()
    low = msg.lower()
    if any(s in low for s in _SESSION):
        hint = " Your saved session may have expired: refresh it on the YouTube page." if signed_in else \
            " Add or refresh your session on the YouTube page."
        return NeedsSession(f"YouTube wants a signed-in session for this video.{hint}")
    if any(s in low for s in _GONE):
        return VideoGone(msg[:300])
    return VideoRetry(msg[:300])


def _yt_dlp() -> Any:
    try:
        import yt_dlp
    except ImportError as exc:
        raise VideoGone("yt-dlp isn't installed, so YouTube videos can't be saved") from exc
    return yt_dlp


@dataclass
class Probe:
    """What saving a video would come to (probe)."""
    title: str | None
    channel: str | None
    duration: int | None  # seconds
    size_bytes: int | None
    approx: bool  # YouTube only estimated the size
    height: int | None


def _size(info: dict[str, Any]) -> tuple[int | None, bool]:
    """The chosen formats' size, added up (video and audio, when they're joined)."""
    total, approx = 0, False
    for f in info.get("requested_formats") or [info]:
        if f.get("filesize"):
            total += int(f["filesize"])
        elif f.get("filesize_approx"):
            total += int(f["filesize_approx"])
            approx = True
        else:
            return None, False
    return total or None, approx


def probe(url: str, session: YouTubeSession) -> Probe:
    """A video's title and how big it would be, saved at the YouTube page's
    resolution, without downloading it. Raises VideoGone, NeedsSession or
    VideoRetry, like download."""
    yt_dlp = _yt_dlp()
    from yt_dlp.utils import DownloadError
    try:
        with yt_dlp.YoutubeDL({**_options(session), "skip_download": True}) as ydl:
            info = ydl.extract_info(url, download=False) or {}
    except DownloadError as exc:
        raise _failure(exc, bool(session.secrets()[0])) from None
    size, approx = _size(info)
    duration = info.get("duration")
    return Probe(title=info.get("title"), channel=info.get("channel") or info.get("uploader"),
                 duration=int(duration) if duration else None, size_bytes=size, approx=approx,
                 height=int(info["height"]) if info.get("height") else None)


def download(url: str, workdir: Path, session: YouTubeSession, max_bytes: int) -> tuple[Path, int]:
    """Download one video into `workdir`. Returns the file and the height it
    was saved at. Raises VideoGone, NeedsSession or VideoRetry."""
    yt_dlp = _yt_dlp()
    from yt_dlp.utils import DownloadError
    stem = ".yt-" + hashlib.sha1(url.encode()).hexdigest()[:16]
    workdir.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=stem, dir=workdir))
    opts = {**_options(session), "outtmpl": str(tmp / "video.%(ext)s"), "max_filesize": max_bytes,
            "overwrites": True}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except DownloadError as exc:
        shutil.rmtree(tmp, ignore_errors=True)
        raise _failure(exc, bool(session.secrets()[0])) from None
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    files = [p for p in tmp.iterdir() if p.is_file() and not p.name.endswith((".part", ".ytdl"))]
    if not files:
        shutil.rmtree(tmp, ignore_errors=True)
        raise VideoGone(f"larger than the {max_bytes // 1_000_000} MB limit for YouTube videos")
    out = workdir / (stem + files[0].suffix)
    files[0].replace(out)
    shutil.rmtree(tmp, ignore_errors=True)
    return out, int((info or {}).get("height") or 0)


# -- videos linked in text: their titles and sizes (the youtube_videos table) ------------
# A YouTube link in a post, comment or article shows the video's title and
# opens a box saying how big the video is, to save it here or watch it on
# YouTube. Titles come from YouTube's oEmbed endpoint when a page showing the
# link is opened; sizes from yt-dlp when the box is.

TITLE_RETRY = timedelta(days=1)  # a title YouTube wouldn't give, asked again after this
SIZE_FRESH = timedelta(hours=6)  # formats (and so sizes) change as YouTube re-encodes
PROBE_RETRY = timedelta(minutes=10)  # a size yt-dlp couldn't find, tried again after this
OEMBED = "https://www.youtube.com/oembed"


def is_video_id(vid: str | None) -> bool:
    return bool(re.fullmatch(_ID, vid or ""))


def _ts(value: str) -> datetime:
    return datetime.strptime(value[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)


def titles(conn: Any, vids: list[str]) -> dict[str, str]:
    """The titles known here for these videos: asked of YouTube before, or
    from a followed channel's post (whose title is the video's)."""
    vids = [v for v in dict.fromkeys(vids) if is_video_id(v)]
    if not vids:
        return {}
    out = {r["video_id"]: r["title"] for r in conn.execute(
        f"SELECT video_id, title FROM youtube_videos WHERE video_id IN ({','.join('?' * len(vids))}) "
        f"AND title IS NOT NULL", vids)}
    missing = {watch_url(v): v for v in vids if v not in out}
    if missing:
        for r in conn.execute(
                f"SELECT r.url, r.title FROM revisions r JOIN objects o ON o.id=r.object_id AND r.seq=o.revision_count "
                f"JOIN archived_threads t ON t.id=o.thread_id JOIN communities c ON c.id=t.community_id "
                f"WHERE r.url IN ({','.join('?' * len(missing))}) AND r.title IS NOT NULL "
                f"AND c.canonical_ap_id LIKE ?", [*missing, FEED_PREFIX + "%"]):
            out.setdefault(missing[r["url"]], r["title"])
    return out


def titles_to_ask(conn: Any, vids: list[str], now: str) -> list[str]:
    """Of these, the videos whose title isn't known and YouTube wasn't asked lately."""
    known = titles(conn, vids)
    ask = [v for v in dict.fromkeys(vids) if is_video_id(v) and v not in known]
    if not ask:
        return []
    cutoff = (_ts(now) - TITLE_RETRY).strftime("%Y-%m-%dT%H:%M:%S")
    recent = {r[0] for r in conn.execute(
        f"SELECT video_id FROM youtube_videos WHERE video_id IN ({','.join('?' * len(ask))}) "
        f"AND title_checked_at >= ?", [*ask, cutoff])}
    return [v for v in ask if v not in recent]


def save_title(conn: Any, vid: str, title: str | None, channel: str | None, now: str) -> None:
    conn.execute("INSERT INTO youtube_videos(video_id, title, channel, title_checked_at) VALUES (?,?,?,?) "
                 "ON CONFLICT(video_id) DO UPDATE SET title=COALESCE(excluded.title, youtube_videos.title), "
                 "channel=COALESCE(excluded.channel, youtube_videos.channel), "
                 "title_checked_at=excluded.title_checked_at", (vid, title, channel, now))


def save_probe(conn: Any, vid: str, found: Probe | None, error: str | None, now: str) -> None:
    """A probe's outcome: what it found, or why it couldn't."""
    if found is None:
        conn.execute("INSERT INTO youtube_videos(video_id, probed_at, probe_error) VALUES (?,?,?) "
                     "ON CONFLICT(video_id) DO UPDATE SET probed_at=excluded.probed_at, "
                     "probe_error=excluded.probe_error", (vid, now, error))
        return
    conn.execute(
        "INSERT INTO youtube_videos(video_id, title, channel, title_checked_at, duration, size_bytes, size_approx, "
        "height, probed_at, probe_error) VALUES (?,?,?,?,?,?,?,?,?,NULL) ON CONFLICT(video_id) DO UPDATE SET "
        "title=COALESCE(excluded.title, youtube_videos.title), "
        "channel=COALESCE(excluded.channel, youtube_videos.channel), title_checked_at=excluded.title_checked_at, "
        "duration=excluded.duration, size_bytes=excluded.size_bytes, size_approx=excluded.size_approx, "
        "height=excluded.height, probed_at=excluded.probed_at, probe_error=NULL",
        (vid, found.title, found.channel, now, found.duration, found.size_bytes, int(found.approx), found.height, now))


def probe_due(row: Any, now: str, max_height: int) -> bool:
    """Whether a video's size (its youtube_videos row) needs finding again:
    never found, found a while ago, or at more than the YouTube page's
    resolution (changed since)."""
    if row is None or not row["probed_at"]:
        return True
    age = _ts(now) - _ts(row["probed_at"])
    if row["probe_error"]:
        return age > PROBE_RETRY
    return age > SIZE_FRESH or bool(row["height"] and row["height"] > max_height)
