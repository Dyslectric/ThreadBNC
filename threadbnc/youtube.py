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
    """A video's page: its description and likes. None if the page has no
    data in it (YouTube may have changed it, or asked for a sign-in)."""
    player = _player_response(html)
    data = _initial_data(html)
    if player is None and data is None:
        return None
    text = ((player or {}).get("videoDetails") or {}).get("shortDescription")  # the whole of it, despite the name
    return Watch(description_markdown(text) if isinstance(text, str) else None, _likes(data, html))


_URL = re.compile(r"https?://[^\s<>\"]+[^\s<>\".,;:!?)\]'’]")
_ESCAPE = re.compile(r"([\\`*_\[\]<>])")
_LINE_START = re.compile(r"^(\s*)([#>+-]|\d+[.)])(?=\s|$)")


def description_markdown(text: str | None) -> str | None:
    """A video description (plain text) as Markdown that shows as written: its
    line breaks kept, links made links, and nothing in it taken as formatting."""
    lines = []
    for line in (text or "").strip().splitlines():
        parts, at = [], 0
        for m in _URL.finditer(line):
            parts += [_ESCAPE.sub(r"\\\1", line[at:m.start()]), f"<{m.group(0)}>"]
            at = m.end()
        parts.append(_ESCAPE.sub(r"\\\1", line[at:]))
        lines.append(_LINE_START.sub(lambda m: m.group(1) + m.group(2)[:-1] + "\\" + m.group(2)[-1],
                                     "".join(parts).rstrip()))
    out = ""
    for i, line in enumerate(lines):
        if i:  # a hard line break between lines of a paragraph; blank lines stay paragraph breaks
            out += "\\\n" if line and lines[i - 1] else "\n"
        out += line
    return out or None


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


def download(url: str, workdir: Path, session: YouTubeSession, max_bytes: int) -> tuple[Path, int]:
    """Download one video into `workdir`. Returns the file and the height it
    was saved at. Raises VideoGone, NeedsSession or VideoRetry."""
    try:
        import yt_dlp
        from yt_dlp.utils import DownloadError
    except ImportError as exc:
        raise VideoGone("yt-dlp isn't installed, so YouTube videos can't be saved") from exc
    cfg = session.status()
    height = cfg["max_height"]
    cookies, po_token = session.secrets()
    stem = ".yt-" + hashlib.sha1(url.encode()).hexdigest()[:16]
    workdir.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=stem, dir=workdir))
    merges = bool(shutil.which("ffmpeg"))
    opts: dict[str, Any] = {
        "outtmpl": str(tmp / "video.%(ext)s"),
        # Separate video and audio streams need ffmpeg to join; without it,
        # the best single file (often only 360p on YouTube now).
        "format": f"bv*[height<={height}]+ba/b[height<={height}]/b" if merges else f"b[height<={height}]/b",
        "format_sort": [f"res:{height}", "ext:mp4:m4a"],
        "merge_output_format": "mp4",
        "max_filesize": max_bytes,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "retries": 3,
        "overwrites": True,
        "js_runtimes": js_runtimes() or {"deno": {}},
        "remote_components": set(),  # the challenge solver comes from the yt-dlp-ejs package, never fetched
    }
    if cookies:
        opts["cookiefile"] = io.StringIO(cookies)  # a stream: the session never touches the disk
    if po_token:
        opts["extractor_args"] = {"youtube": {"po_token": [po_token if "+" in po_token else f"web.gvs+{po_token}"]}}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except DownloadError as exc:
        shutil.rmtree(tmp, ignore_errors=True)
        msg = re.sub(r"^ERROR:\s*(\[[^\]]+\]\s*[\w-]+:\s*)?", "", str(exc)).strip()
        low = msg.lower()
        if any(s in low for s in _SESSION):
            hint = " Add or refresh your session on the YouTube page." if not cookies else \
                " Your saved session may have expired: refresh it on the YouTube page."
            raise NeedsSession(f"YouTube wants a signed-in session for this video.{hint}") from None
        if any(s in low for s in _GONE):
            raise VideoGone(msg[:300]) from None
        raise VideoRetry(msg[:300]) from None
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
