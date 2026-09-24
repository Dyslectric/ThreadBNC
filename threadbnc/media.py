"""Media archiving: images/GIFs/short videos/audio referenced by retained content.

Media is registered when a revision is recorded and downloaded later by the
bouncer (never while holding the DB write lock). Files are content-addressed
under <data_dir>/media and are only ever removed when every object referencing
them has been purged (expired auto-captured threads).

Pictures, videos and audio each have their own settings (MediaPolicy): whether
they're archived at all, the largest file kept as it is, and for pictures and
videos, a size bigger ones are transcoded down to (see transcode.py) instead of
being left out. The server's defaults are set on the Storage page and each
community can choose its own. Media shared between communities gets the most
generous of their settings.

Pictures (and video thumbnails, which are pictures) are downloaded as soon as
they're seen. Full videos wait until a post showing them is opened or kept
(`held`): most scroll past unwatched, and they're by far the biggest files.
YouTube videos (`kept_only`) wait longer still: they're downloaded with yt-dlp
only once a post linking to one is kept (see youtube.py), and have settings of
their own on the YouTube page (the resolution they're saved at) rather than
the Videos ones. Audio files are held
too, but a post linking to one fetches it as soon as the post is scrolled into
view in a feed (Bouncer.fetch_audio), so it's ready to play from the post's
player bar. Podcast episodes (`episode`, from a feed's enclosures) aren't:
an hour of audio is 50-150 MB, and podcast hosts count every download as a
listen. One is downloaded when you press play on it (`wanted_at`) or keep its
post, and may be bigger than other audio (THREADBNC_PODCAST_MAX_MB).
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import mimetypes
import socket
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import timedelta
from pathlib import Path
from typing import Any, Iterable, Iterator
from urllib.parse import urljoin, urlparse

import httpx

from . import thumbs, transcode, youtube
from .adapters.base import RemotePaused
from .adapters.http import HostThrottle
from .db import Conn, Database, fmt_ts, parse_ts, utcnow
from .render import VIDEO_EXTENSIONS, MediaInfo, extract_media_urls, looks_like_audio, looks_like_media

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 5
MAX_REDIRECTS = 5
CHUNK = 64 * 1024

_MAGIC = [
    (b"GIF87a", "image/gif"), (b"GIF89a", "image/gif"), (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"), (b"\x1aE\xdf\xa3", "video/webm"), (b"BM", "image/bmp"),
]
_EXT = {"image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif", "image/webp": ".webp",
        "image/avif": ".avif", "video/mp4": ".mp4", "video/webm": ".webm", "image/svg+xml": ".svg",
        "video/quicktime": ".mov", "image/bmp": ".bmp", "image/heic": ".heic", "audio/mpeg": ".mp3",
        "audio/mp4": ".m4a", "audio/aac": ".aac", "audio/ogg": ".ogg", "audio/flac": ".flac",
        "audio/wav": ".wav", "audio/opus": ".opus"}


class MediaRejected(Exception):
    """Permanent: not media, too large, or a forbidden address."""


class MediaSkipped(MediaRejected):
    """Not wanted: not media at all, or a community's settings leave it out."""


class MediaHeld(Exception):
    """A video or audio file nobody has opened yet: wait until someone does."""


# A file is wanted in full once a post (or comment) showing it is in a thread
# you opened or kept. Until then, videos and audio are held.
WANTED_SQL = ("EXISTS (SELECT 1 FROM media_refs wr JOIN objects wo ON wo.id=wr.object_id "
              "JOIN archived_threads wt ON wt.id=wo.thread_id WHERE wr.media_id=media.id "
              "AND (wt.retention='manual' OR wt.opened_at IS NOT NULL))")
# A YouTube video is wanted only once a post linking to it is kept.
KEPT_SQL = ("EXISTS (SELECT 1 FROM media_refs kr JOIN objects ko ON ko.id=kr.object_id "
            "JOIN archived_threads kt ON kt.id=ko.thread_id WHERE kr.media_id=media.id "
            "AND kt.retention='manual' AND kt.trashed_at IS NULL)")


def looks_like_video(url: str) -> bool:
    return urlparse(url).path.lower().endswith(VIDEO_EXTENSIONS)


_BIG = ("video/", "audio/")  # held until wanted


# What's archived is chosen per kind of file. Pictures and videos up to a size
# are kept as they are; bigger ones are transcoded down to a target size, or
# for videos to a bitrate (see transcode.py), or left out when there's neither.
# Audio is only ever kept as it is, up to its size limit.
KINDS = {"image": "Pictures", "video": "Videos", "audio": "Audio"}
TRANSCODED = ("image", "video")  # the kinds that can be transcoded down
BY_RATE = ("video",)  # the kinds that can be transcoded to a bitrate instead of a size
# Errors that a change of settings can fix; requeue() retries these.
POLICY_SUFFIX = "for this community"


def kind_of(ctype: str) -> str | None:
    """"image", "video" or "audio" for a content type; None for anything else.
    Animated GIFs are pictures (transcoding one makes it an MP4)."""
    for kind in KINDS:
        if ctype.startswith(kind + "/"):
            return kind
    return None


@dataclass(frozen=True)
class KindPolicy:
    save: bool = True
    keep_bytes: int = 25_000_000  # kept as they are up to this
    # Bigger ones: transcoded down to this size, or (videos) re-encoded at this
    # bitrate, whatever size that comes to; neither = left out.
    target_bytes: int | None = None
    target_bps: int | None = None

    @property
    def transcodes(self) -> bool:
        return bool(self.target_bytes or self.target_bps)

    def override(self, choice: dict[str, Any]) -> KindPolicy:
        """This policy with the choices made (a missing or None field keeps it).
        target_mb 0: bigger files are left out; target_mbps: a bitrate instead
        of a size; "fit": transcoded down to the size limit, whatever it is
        (settings from before, legacy_choices)."""
        save, keep = choice.get("save"), choice.get("keep_mb")
        keep_bytes = keep * 1_000_000 if keep else self.keep_bytes
        size, rate = self.target_bytes, self.target_bps
        if choice.get("fit"):
            size, rate = keep_bytes, None
        elif choice.get("target_mbps") is not None:
            size, rate = None, int(choice["target_mbps"] * 1_000_000) or None
        elif choice.get("target_mb") is not None:
            size, rate = choice["target_mb"] * 1_000_000 or None, None
        return KindPolicy(self.save if save is None else bool(save), keep_bytes, size, rate)

    def merge(self, other: KindPolicy) -> KindPolicy:
        """The more generous of two policies, for media several communities
        share. A bitrate wins over a size (it doesn't squeeze long videos)."""
        rates = [r for r in (self.target_bps, other.target_bps) if r]
        sizes = [t for t in (self.target_bytes, other.target_bytes) if t]
        return KindPolicy(self.save or other.save, max(self.keep_bytes, other.keep_bytes),
                          None if rates or not sizes else max(sizes), max(rates) if rates else None)


@dataclass(frozen=True)
class MediaPolicy:
    image: KindPolicy = KindPolicy()
    video: KindPolicy = KindPolicy()
    audio: KindPolicy = KindPolicy()

    @classmethod
    def uniform(cls, max_bytes: int, transcode_: bool) -> MediaPolicy:
        """Every kind kept up to max_bytes, pictures and videos over that
        transcoded to fit when transcode_ (the THREADBNC_MEDIA_* settings)."""
        shrunk = KindPolicy(True, max_bytes, max_bytes if transcode_ else None)
        return cls(shrunk, shrunk, KindPolicy(True, max_bytes, None))

    def kind(self, name: str) -> KindPolicy:
        return getattr(self, name)

    def for_type(self, ctype: str) -> KindPolicy | None:
        kind = kind_of(ctype)
        return self.kind(kind) if kind else None

    @property
    def saves_any(self) -> bool:
        return any(self.kind(k).save for k in KINDS)

    def override(self, choices: dict[str, dict[str, Any]] | None) -> MediaPolicy:
        """This policy with a community's (or the Storage page's) choices applied."""
        choices = choices or {}
        return MediaPolicy(**{k: self.kind(k).override(choices.get(k) or {}) for k in KINDS})

    def merge(self, other: MediaPolicy) -> MediaPolicy:
        return MediaPolicy(**{k: self.kind(k).merge(other.kind(k)) for k in KINDS})


def legacy_choices(archive: str | None, max_mb: int | None, transcode_: int | None) -> dict[str, dict[str, Any]]:
    """The per-kind choices that the settings saved before there was one set
    for each kind amount to: what was archived ("all"; "images", pictures
    only; "off"), one size limit, and whether to transcode to fit it ("fit":
    down to whatever the size limit is)."""
    out: dict[str, dict[str, Any]] = {}
    for kind in KINDS:
        c: dict[str, Any] = {}
        if archive in ("all", "images", "off"):
            c["save"] = archive == "all" or (archive == "images" and kind == "image")
        if max_mb:
            c["keep_mb"] = max_mb
        if transcode_ is not None and kind in TRANSCODED:
            if not transcode_:
                c["target_mb"] = 0
            elif max_mb:
                c["target_mb"] = max_mb
            else:
                c["fit"] = True
        if c:
            out[kind] = c
    return out


def parse_choices(raw: str | None) -> dict[str, dict[str, Any]]:
    """Choices as saved (JSON): {kind: {save, keep_mb, target_mb}}, fields left out following the default."""
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        return {}
    return {k: v for k, v in data.items() if k in KINDS and isinstance(v, dict)} if isinstance(data, dict) else {}


def policy_for(conn: Conn, media_id: int, default: MediaPolicy) -> MediaPolicy:
    rows = conn.execute(
        "SELECT DISTINCT c.media_policy FROM media_refs r JOIN objects o ON o.id=r.object_id "
        "LEFT JOIN communities c ON c.id=o.community_id WHERE r.media_id=?", (media_id,)).fetchall()
    if not rows:
        return default
    policies = [default.override(parse_choices(r[0])) for r in rows]
    result = policies[0]
    for p in policies[1:]:
        result = result.merge(p)
    return result


def community_ids_sql() -> str:
    return "SELECT r.media_id FROM media_refs r JOIN objects o ON o.id=r.object_id WHERE o.community_id=?"


def requeue(conn: Conn, community_id: int | None = None) -> int:
    """Try again the media that was too large or left out by settings, after
    those settings changed: one community's, or (None) everything's."""
    scope, args = (f"id IN ({community_ids_sql()}) AND ", [community_id]) if community_id is not None else ("", [])
    return conn.execute(
        "UPDATE media SET status='pending', attempts=0, next_attempt_at=?, error=NULL "
        f"WHERE {scope}((status='failed' AND error LIKE 'too large%') "
        "OR (status='skipped' AND error LIKE ?))", [utcnow(), *args, f"%{POLICY_SUFFIX}"]).rowcount


# Server-wide defaults chosen on the Storage page, in app_settings; each one
# unset follows the THREADBNC_MEDIA_* environment settings.
DEFAULTS_KEY = "media_policy"
_LEGACY_KEYS = ("media_archive", "media_max_mb", "media_transcode")


def dump_choices(choices: dict[str, dict[str, Any]]) -> str | None:
    """Choices to save; None when every field follows the default."""
    kept = {k: {f: v for f, v in c.items() if v is not None} for k, c in choices.items() if k in KINDS}
    kept = {k: c for k, c in kept.items() if c}
    return json.dumps(kept, sort_keys=True) if kept else None


def save_defaults(conn: Conn, choices: dict[str, dict[str, Any]]) -> None:
    value = dump_choices(choices)
    if value is None:
        conn.execute("DELETE FROM app_settings WHERE key=?", (DEFAULTS_KEY,))
    else:
        conn.execute("INSERT INTO app_settings(key, value) VALUES (?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (DEFAULTS_KEY, value))


def load_defaults(conn: Conn) -> dict[str, dict[str, Any]]:
    """The Storage page's choices as saved: {kind: {field: value}}."""
    row = conn.execute("SELECT value FROM app_settings WHERE key=?", (DEFAULTS_KEY,)).fetchone()
    return parse_choices(row[0] if row else None)


# After settings change, the files already archived are gone through (by id,
# from here) and those now over their size limit transcoded down: convert_some.
CONVERT_KEY = "media_convert_after"
CONVERT_SCAN = 200  # rows looked at per call


def request_conversion(conn: Conn) -> None:
    """Go through what's archived again, from the start, with the settings as they are now."""
    conn.execute("INSERT INTO app_settings(key, value) VALUES (?, '0') "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (CONVERT_KEY,))


_CONVERTIBLE = ("status='ok' AND storage_path IS NOT NULL AND content_type != 'image/svg+xml' "
                "AND (content_type LIKE 'image/%' OR content_type LIKE 'video/%')")


def conversion_progress(conn: Conn) -> dict[str, int] | None:
    """How far the pass over archived files (convert_some) has got: files
    checked and files to check; None when it isn't running."""
    row = conn.execute("SELECT value FROM app_settings WHERE key=?", (CONVERT_KEY,)).fetchone()
    if row is None:
        return None
    after = int(row[0] or 0)
    total = conn.execute(f"SELECT COUNT(*) FROM media WHERE {_CONVERTIBLE}").fetchone()[0]
    done = conn.execute(f"SELECT COUNT(*) FROM media WHERE {_CONVERTIBLE} AND id<=?", (after,)).fetchone()[0]
    return {"checked": done, "total": total}


_THREAD_OF = ("(SELECT o.thread_id FROM media_refs r JOIN objects o ON o.id=r.object_id "
              "WHERE r.media_id=m.id AND o.thread_id IS NOT NULL ORDER BY o.id LIMIT 1)")


def recent_transcodes(conn: Conn, limit: int = 12) -> list[Any]:
    """The files transcoded most recently, with a post each is in."""
    return conn.execute(
        f"SELECT m.id, m.url, m.content_type, m.size_bytes, m.original_bytes, m.original_type, "
        f"COALESCE(m.transcoded_at, m.fetched_at) AS at, {_THREAD_OF} AS tid FROM media m "
        "WHERE m.status='ok' AND m.original_bytes IS NOT NULL ORDER BY at DESC, m.id DESC LIMIT ?",
        (limit,)).fetchall()


def transcode_failures(conn: Conn, limit: int = 12) -> list[Any]:
    """Files that couldn't be transcoded: left out when downloaded, or kept as
    they were when converting them after settings changed failed."""
    return conn.execute(
        f"SELECT m.id, m.url, m.status, m.size_bytes, COALESCE(m.transcode_error, m.error) AS why, "
        f"COALESCE(m.fetched_at, m.first_seen_at) AS at, {_THREAD_OF} AS tid FROM media m "
        "WHERE (m.status='ok' AND m.transcode_error IS NOT NULL) "
        "OR (m.status='failed' AND m.error LIKE '%couldn''t transcode%') ORDER BY at DESC, m.id DESC LIMIT ?",
        (limit,)).fetchall()


def migrate_legacy(conn: Conn) -> None:
    """Settings saved as one set for every kind (media_archive, media_max_mb,
    media_transcode) become the same choices for each kind."""
    marks = ",".join("?" * len(_LEGACY_KEYS))
    saved = {r[0]: r[1] for r in conn.execute(f"SELECT key, value FROM app_settings WHERE key IN ({marks})",
                                              _LEGACY_KEYS).fetchall()}
    if saved:
        mb, tc = saved.get("media_max_mb"), saved.get("media_transcode")
        choices = legacy_choices(saved.get("media_archive"), int(mb) if mb else None,
                                 int(tc) if tc is not None else None)
        if not load_defaults(conn):
            save_defaults(conn, choices)
        conn.execute(f"DELETE FROM app_settings WHERE key IN ({marks})", _LEGACY_KEYS)
    for r in conn.execute("SELECT id, media_archive, media_max_mb, media_transcode FROM communities WHERE "
                          "media_archive IS NOT NULL OR media_max_mb IS NOT NULL OR media_transcode IS NOT NULL"
                          ).fetchall():
        conn.execute("UPDATE communities SET media_policy=?, media_archive=NULL, media_max_mb=NULL, "
                     "media_transcode=NULL WHERE id=?",
                     (dump_choices(legacy_choices(r[1], r[2], r[3])), r[0]))


def community_stats(conn: Conn, community_id: int) -> dict[str, Any]:
    """What's been archived for a community, for its Media tab."""
    ids = community_ids_sql()
    counts = {r["status"]: r for r in conn.execute(
        "SELECT status, COUNT(*) AS n, COALESCE(SUM(size_bytes), 0) AS bytes, "
        "COALESCE(SUM(original_bytes), 0) AS original, COUNT(original_bytes) AS transcoded "
        f"FROM media WHERE id IN ({ids}) GROUP BY status", (community_id,))}
    problems = conn.execute(
        f"SELECT id, url, status, error, attempts FROM media WHERE id IN ({ids}) "
        "AND (status='failed' OR (status='skipped' AND error NOT LIKE 'not media%')) ORDER BY id DESC LIMIT 25",
        (community_id,)).fetchall()
    transcoded = conn.execute(
        f"SELECT id, url, content_type, size_bytes, original_bytes, original_type FROM media WHERE id IN ({ids}) "
        "AND original_bytes IS NOT NULL ORDER BY COALESCE(transcoded_at, fetched_at) DESC LIMIT 10",
        (community_id,)).fetchall()
    return {"counts": counts, "problems": problems, "transcoded": transcoded}


def sniff(head: bytes) -> str | None:
    for magic, ctype in _MAGIC:
        if head.startswith(magic):
            return ctype
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "audio/wav"
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in (b"avif", b"avis"):
            return "image/avif"
        if brand.startswith(b"heic") or brand in (b"heix", b"mif1"):
            return "image/heic"
        if brand in (b"M4A ", b"M4B ", b"M4P "):
            return "audio/mp4"
        return "video/quicktime" if brand == b"qt  " else "video/mp4"
    if head.startswith(b"ID3"):
        return "audio/mpeg"
    if head.startswith(b"fLaC"):
        return "audio/flac"
    if head.startswith(b"OggS"):
        return "audio/ogg"
    if len(head) > 1 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0:  # MPEG audio frame sync
        return "audio/aac" if head[1] & 0x06 == 0 else "audio/mpeg"  # layer 0 is AAC (ADTS)
    return None


def _content_type(head: bytes, declared: str, url: str) -> str:
    """What a download is: its first bytes, unless they only say which container
    it is (MP4, Ogg), which holds audio as well as video: then the server's say,
    or the link's extension."""
    ctype = sniff(head) or declared
    if ctype in ("video/mp4", "audio/ogg") and declared.startswith(_BIG):
        return declared
    if ctype == "video/mp4" and looks_like_audio(url):
        return "audio/mp4"
    return ctype


def _noun(ctype: str) -> str:
    return {"image": "pictures", "audio": "audio files"}.get(kind_of(ctype) or "", "videos")


def _how(target: int | None, bps: int | None, height: int | None = None) -> str:
    if height:
        return f"down to {height}p"
    return f"at {bps / 1_000_000:g} Mbps" if bps else f"down to {(target or 0) // 1_000_000} MB"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def _after(now: str, seconds: float) -> str:
    return fmt_ts(parse_ts(now) + timedelta(seconds=seconds))  # type: ignore[operator]


def _assert_public_host(url: str) -> None:
    """Refuse loopback/private/link-local targets so archived content can't
    make the bouncer probe the local network."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise MediaRejected("unsupported URL")
    try:
        infos = socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
    except socket.gaierror as exc:
        raise httpx.ConnectError(f"DNS lookup failed: {exc}") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise MediaRejected(f"refusing non-public address {ip}")


# --- registration (inside store transactions) ------------------------------

def media_candidates(title: str | None, body: str | None, url: str | None) -> list[str]:
    urls = extract_media_urls(body)
    # A post's own link is only fetched when it looks like an image/video file.
    # Article links get their preview from the server's thumbnail instead, so we
    # never load third-party pages just to find out what they are.
    if url and looks_like_media(url) and url not in urls:
        urls.append(url)
    # A YouTube link is registered to be saved with yt-dlp if the post is kept.
    if url and youtube.video_id(url) and url not in urls:
        urls.append(url)
    return urls


def skip_unprobed_links(conn: Conn) -> int:
    """Stop pending downloads of post links registered before we stopped probing
    non-media links. Embedded images and thumbnails are left alone."""
    wanted: set[str] = set()
    for r in conn.execute("SELECT body FROM revisions WHERE body IS NOT NULL"):
        wanted.update(extract_media_urls(r["body"]))
    wanted.update(r[0] for r in conn.execute("SELECT thumbnail_url FROM objects WHERE thumbnail_url IS NOT NULL"))
    for r in conn.execute("SELECT images_json FROM articles WHERE images_json IS NOT NULL"):
        wanted.update(json.loads(r[0]))  # pictures in linked articles (articles.py)
    n = 0
    for r in conn.execute("SELECT id, url FROM media WHERE status='pending'").fetchall():
        if r["url"] not in wanted and not looks_like_media(r["url"]) and not youtube.video_id(r["url"]):
            conn.execute("UPDATE media SET status='skipped', error='post link is not an image; not fetched' "
                         "WHERE id=?", (r["id"],))
            n += 1
    return n


def media_id(conn: Conn, url: str, now: str, episode: bool = False) -> int:
    """The media row for a URL, registered (to be downloaded) if it's new.
    YouTube videos start out held until a post linking to them is kept, and
    podcast episodes until one is played or kept."""
    yt = int(bool(youtube.video_id(url)))
    conn.execute("INSERT INTO media(url, first_seen_at, next_attempt_at, held, kept_only, episode) "
                 "VALUES (?,?,?,?,?,?) ON CONFLICT(url) DO NOTHING",
                 (url, now, now, int(yt or episode), yt, int(episode)))
    if episode:  # the same file linked before, as a plain audio link
        conn.execute("UPDATE media SET episode=1, held=CASE WHEN status='pending' THEN 1 ELSE held END "
                     "WHERE url=? AND episode=0", (url,))
    return conn.execute("SELECT id FROM media WHERE url=?", (url,)).fetchone()[0]


def register(conn: Conn, object_id: int, urls: Iterable[str], now: str, from_article: bool = False,
             episode: bool = False) -> None:
    """`from_article`: pictures in the article the post links to (articles.py),
    which aren't shown as the post's own pictures in feeds. `episode`: a
    podcast episode's audio file."""
    # A picture that's the post's own as well as the article's counts as its own.
    on_conflict = "DO NOTHING" if from_article else "DO UPDATE SET from_article=0 WHERE media_refs.from_article=1"
    for url in urls:
        mid = media_id(conn, url, now, episode)
        conn.execute("INSERT INTO media_refs(object_id, media_id, first_seen_at, from_article) VALUES (?,?,?,?) "
                     f"ON CONFLICT(object_id, media_id) {on_conflict}", (object_id, mid, now, int(from_article)))


def register_youtube_links(conn: Conn) -> None:
    """YouTube links in posts from before videos were saved with yt-dlp,
    including ones an older version skipped as not being pictures."""
    now = utcnow()
    for r in conn.execute("SELECT DISTINCT object_id, url FROM revisions WHERE url LIKE '%youtu%'").fetchall():
        if youtube.video_id(r["url"]):
            register(conn, r["object_id"], [r["url"]], now)
            conn.execute("UPDATE media SET kept_only=1, held=1, status='pending', error=NULL, attempts=0 "
                         "WHERE url=? AND kept_only=0 AND status IN ('pending', 'skipped')", (r["url"],))


def retry_youtube(conn: Conn) -> int:
    """YouTube downloads that failed, perhaps for want of a session: try them
    again (one was just saved)."""
    return conn.execute("UPDATE media SET status='pending', attempts=0, error=NULL, next_attempt_at=NULL "
                        "WHERE kept_only=1 AND status='failed'").rowcount


def register_all_existing(conn: Conn) -> None:
    """Backfill refs for revisions stored before media archiving existed."""
    now = utcnow()
    for r in conn.execute("SELECT object_id, title, body, url FROM revisions").fetchall():
        register(conn, r["object_id"], media_candidates(r["title"], r["body"], r["url"]), now)


def lookup_for_objects(conn: Conn, object_ids: list[int]) -> dict[str, MediaInfo]:
    if not object_ids:
        return {}
    marks = ",".join("?" * len(object_ids))
    rows = conn.execute(
        f"SELECT DISTINCT m.* FROM media m JOIN media_refs r ON r.media_id=m.id WHERE r.object_id IN ({marks})",
        object_ids,
    ).fetchall()
    return {r["url"]: MediaInfo(r["id"], r["status"], r["content_type"], r["error"], r["size_bytes"],
                                r["original_bytes"], r["original_type"], r["transcode_error"]) for r in rows}


# --- downloading ------------------------------------------------------------

@dataclass
class Downloaded:
    tmp_path: Path
    content_type: str
    size: int
    sha256: str
    final_url: str
    original_size: int | None = None  # set when transcoded down from a bigger file
    original_type: str | None = None


class MediaFetcher:
    def __init__(self, db: Database, media_dir: Path, user_agent: str, max_bytes: int,
                 timeout: float = 30.0, client: httpx.Client | None = None, check_host: bool = True,
                 throttle: HostThrottle | None = None, transcode_default: bool = False,
                 transcode_source_max_bytes: int = 1_000_000_000,
                 youtube_session: youtube.YouTubeSession | None = None,
                 episode_max_bytes: int = 500_000_000):
        self.db = db
        self.episode_max_bytes = episode_max_bytes  # podcast episodes may be this big, whatever audio's limit
        self.youtube = youtube_session
        self.throttle = throttle or HostThrottle(1.0)
        self.media_dir = media_dir
        self.env_policy = MediaPolicy.uniform(max_bytes, transcode_default)
        # Files are downloaded up to this size when they might be transcoded down.
        self.source_max_bytes = transcode_source_max_bytes
        self.working: dict[str, Any] | None = None  # the file being transcoded now (_transcoding)
        self.check_host = check_host
        self.client = client or httpx.Client(timeout=timeout, headers={"User-Agent": user_agent},
                                             follow_redirects=False)

    @property
    def default_policy(self) -> MediaPolicy:
        """What communities that haven't chosen get: the environment's settings
        with the ones saved on the Storage page applied."""
        with self.db.connect() as conn:
            return self.env_policy.override(load_defaults(conn))

    def can_transcode(self) -> bool:
        return transcode.available()

    def _cap(self, kp: KindPolicy) -> int:
        """The largest file of a kind worth downloading: bigger than it's kept
        as-is when it could be transcoded down."""
        if kp.transcodes and self.can_transcode():
            return max(kp.keep_bytes, self.source_max_bytes)
        return kp.keep_bytes

    def _download(self, url: str, policy: MediaPolicy | None = None, wanted: bool = True) -> Downloaded:
        """`wanted` False: a video is held (MediaHeld) as soon as the server says it's one."""
        policy = policy or self.default_policy
        # Until the server says what it is, the most any kind archived here allows.
        cap = max((self._cap(policy.kind(k)) for k in KINDS if policy.kind(k).save), default=0)
        if urlparse(url).path.lower().endswith(".gifv"):  # imgur-style: the real file is .mp4
            url = url[: -len(".gifv")] + ".mp4"
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            if self.check_host:
                _assert_public_host(current)
            host = (urlparse(current).hostname or "").lower()
            self.throttle.wait(host)
            with self.client.stream("GET", current) as resp:
                self.throttle.note(host, resp.status_code, resp.headers)
                if resp.status_code in (301, 302, 303, 307, 308) and "location" in resp.headers:
                    current = urljoin(current, resp.headers["location"])
                    continue
                if resp.status_code == 429:
                    raise RemotePaused(f"{host}: HTTP 429 (too many requests)", self.throttle.paused_for(host))
                if resp.status_code == 404 or resp.status_code == 410:
                    raise MediaRejected(f"HTTP {resp.status_code}")
                if resp.status_code >= 400:
                    raise httpx.HTTPStatusError(f"HTTP {resp.status_code}", request=resp.request, response=resp)
                declared = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
                length = int(resp.headers.get("content-length") or 0)
                if declared.startswith(("text/", "application/json", "application/xhtml")):
                    raise MediaSkipped(f"not media ({declared})")
                kp = policy.for_type(declared)
                if kp is not None and not kp.save:
                    raise MediaSkipped(f"{_noun(declared)} aren't archived {POLICY_SUFFIX}")
                if not wanted and declared.startswith(_BIG):
                    raise MediaHeld()
                if kp is not None:
                    cap = self._cap(kp)
                if length > cap:
                    raise MediaRejected(f"too large ({length / 1_000_000:.1f} MB)")
                self.media_dir.mkdir(parents=True, exist_ok=True)
                h = hashlib.sha256()
                size, head = 0, b""
                tmp = tempfile.NamedTemporaryFile(dir=self.media_dir, prefix=".dl-", delete=False)
                try:
                    with tmp:
                        for chunk in resp.iter_bytes(CHUNK):
                            if len(head) < 32:
                                head += chunk[: 32 - len(head)]
                            size += len(chunk)
                            if size > cap:
                                raise MediaRejected(f"too large (> {cap // 1_000_000} MB)")
                            h.update(chunk)
                            tmp.write(chunk)
                    ctype = _content_type(head, declared, url)
                    kp = policy.for_type(ctype)
                    if kp is None:
                        raise MediaSkipped(f"not media ({declared or 'unknown type'})")
                    if not kp.save:
                        raise MediaSkipped(f"{_noun(ctype)} aren't archived {POLICY_SUFFIX}")
                    if size > self._cap(kp):
                        raise MediaRejected(f"too large ({size / 1_000_000:.1f} MB)")
                except BaseException:
                    Path(tmp.name).unlink(missing_ok=True)
                    raise
                return Downloaded(Path(tmp.name), ctype, size, h.hexdigest(), current)
        raise MediaRejected("too many redirects")

    def _download_youtube(self, url: str) -> Downloaded:
        """A YouTube video, with yt-dlp (youtube.py). Its size limit and quality
        come from the YouTube page rather than the community: saving the video
        is what keeping a YouTube post is for."""
        if self.youtube is None:
            raise MediaRejected("YouTube videos aren't saved here")
        cfg = self.youtube.status()
        self.throttle.wait("www.youtube.com")
        try:
            path, _ = youtube.download(url, self.media_dir, self.youtube, cfg["max_mb"] * 1_000_000)
        except youtube.VideoGone as exc:
            if isinstance(exc, youtube.NeedsSession):
                self.youtube.note(str(exc))
            raise MediaRejected(str(exc)) from None
        self.youtube.note(None)
        h = hashlib.sha256()
        with path.open("rb") as f:
            head = f.read(32)
            f.seek(0)
            for chunk in iter(lambda: f.read(CHUNK), b""):
                h.update(chunk)
        ctype = sniff(head) or mimetypes.guess_type(path.name)[0] or "video/mp4"
        return Downloaded(path, ctype, path.stat().st_size, h.hexdigest(), url)

    def _shrink(self, dl: Downloaded, limit: int) -> Downloaded:
        """Transcode a download that's over the limit (only downloaded that big when allowed to)."""
        try:
            out, ctype = transcode.shrink(dl.tmp_path, dl.content_type, limit, self.media_dir)
        except transcode.TranscodeError as exc:
            raise MediaRejected(f"too large ({dl.size / 1_000_000:.1f} MB); couldn't transcode: {exc}") from None
        finally:
            dl.tmp_path.unlink(missing_ok=True)
        h = hashlib.sha256()
        with out.open("rb") as f:
            for chunk in iter(lambda: f.read(CHUNK), b""):
                h.update(chunk)
        return Downloaded(out, ctype, out.stat().st_size, h.hexdigest(), dl.final_url, dl.size, dl.content_type)

    def convert_some(self) -> bool:
        """Bring archived pictures and videos in line with the settings (after
        they changed: request_conversion). Files over the size they're kept
        as they are up to, with a size or bitrate to transcode them to, are
        transcoded from the stored copy, which the smaller one replaces.
        YouTube videos go by the YouTube page instead: ones saved at a higher
        resolution than it says are scaled down to it. Files of a kind no
        longer archived stay: nothing is deleted for that. At most one file
        is transcoded per call; True while there's more to go through."""
        with self.db.connect() as conn:
            row = conn.execute("SELECT value FROM app_settings WHERE key=?", (CONVERT_KEY,)).fetchone()
            if row is None or not self.can_transcode():
                return False
            after = int(row[0] or 0)
            default = self.default_policy
            yt_height = self.youtube.status()["max_height"] if self.youtube else None
            rows = conn.execute(
                f"SELECT id, url, content_type, size_bytes, storage_path, kept_only FROM media "
                f"WHERE id>? AND {_CONVERTIBLE} ORDER BY id LIMIT ?", (after, CONVERT_SCAN)).fetchall()
            todo = None
            for r in rows:
                after = r["id"]
                kp = policy_for(conn, r["id"], default).for_type(r["content_type"])
                if r["kept_only"]:  # YouTube's: to its resolution (to_height tells whether it's over)
                    if kp and kp.save and yt_height:
                        todo = (r, None, None, yt_height)
                        break
                    continue
                if not (kp and kp.save and r["size_bytes"] > kp.keep_bytes):
                    continue
                if kp.target_bps and r["content_type"].startswith("video/"):
                    todo = (r, None, kp.target_bps)
                    break
                if kp.target_bytes and r["size_bytes"] > kp.target_bytes:
                    todo = (r, kp.target_bytes, None)
                    break
        with self.db.transaction() as conn:
            if rows:
                conn.execute("UPDATE app_settings SET value=? WHERE key=?", (str(after), CONVERT_KEY))
            else:
                conn.execute("DELETE FROM app_settings WHERE key=?", (CONVERT_KEY,))
        if todo:
            self._convert(*todo)
        return bool(rows)

    def _convert(self, row: Any, target: int | None, bps: int | None, height: int | None = None) -> None:
        src = self.media_dir / row["storage_path"]
        how = _how(target, bps, height)
        try:
            with self._transcoding(row["id"], row["url"], row["size_bytes"], how):
                if height:
                    out, ctype = transcode.to_height(src, height, self.media_dir), "video/mp4"
                elif bps:
                    out, ctype = transcode.at_bitrate(src, bps, self.media_dir), "video/mp4"
                else:
                    out, ctype = transcode.shrink(src, row["content_type"], target, self.media_dir)
        except (transcode.TranscodeError, OSError) as exc:
            log.warning("media %s: couldn't transcode it %s: %s", row["id"], how, exc)
            with self.db.transaction() as conn:
                conn.execute("UPDATE media SET transcode_error=? WHERE id=?",
                             (f"couldn't transcode it {how}: {exc}", row["id"]))
            return
        if out is None:  # at that rate or resolution, or lower, already
            with self.db.transaction() as conn:
                conn.execute("UPDATE media SET transcode_error=NULL WHERE id=?", (row["id"],))
            return
        h = hashlib.sha256()
        with out.open("rb") as f:
            for chunk in iter(lambda: f.read(CHUNK), b""):
                h.update(chunk)
        rel = self._store(Downloaded(out, ctype, out.stat().st_size, h.hexdigest(), ""))
        size = (self.media_dir / rel).stat().st_size
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE media SET content_type=?, size_bytes=?, sha256=?, storage_path=?, transcoded_at=?, "
                "transcode_error=NULL, original_bytes=COALESCE(original_bytes, ?), "
                "original_type=COALESCE(original_type, ?) WHERE id=?",
                (ctype, size, h.hexdigest(), rel, utcnow(), row["size_bytes"], row["content_type"], row["id"]))
            unused = rel != row["storage_path"] and not conn.execute(
                "SELECT 1 FROM media WHERE storage_path=? LIMIT 1", (row["storage_path"],)).fetchone()
        if unused:  # the bigger copy, replaced, and the smaller copies of it for browsing
            src.unlink(missing_ok=True)
            thumbs.remove_for(self.media_dir, src.stem)
        if thumbs.eligible(ctype):
            self.make_thumbs(rel, h.hexdigest())
        log.info("media %s: transcoded down from %.1f MB to %.1f MB", row["id"], row["size_bytes"] / 1e6, size / 1e6)

    @contextmanager
    def _transcoding(self, media_id: int | None, url: str, size: int, how: str) -> Iterator[None]:
        """What's being transcoded, while it is (`working`, for the Storage page)."""
        self.working = {"id": media_id, "url": url, "size": size, "how": how, "since": utcnow()}
        try:
            yield
        finally:
            self.working = None

    def _fit(self, dl: Downloaded, kp: KindPolicy, media_id: int | None = None) -> Downloaded:
        """A download bigger than it's kept as it is up to, transcoded as the
        settings say (to a size, or a video to a bitrate). With nothing to
        transcode it down to, it's left out (MediaRejected)."""
        if dl.size <= kp.keep_bytes:
            return dl
        if not (kp.transcodes and self.can_transcode()):
            dl.tmp_path.unlink(missing_ok=True)
            raise MediaRejected(f"too large ({dl.size / 1_000_000:.1f} MB)")
        if kp.target_bps and dl.content_type.startswith("video/"):
            with self._transcoding(media_id, dl.final_url, dl.size, _how(None, kp.target_bps)):
                return self._reencode(dl, kp.target_bps)
        if kp.target_bytes and dl.size > kp.target_bytes:  # (already small enough otherwise)
            with self._transcoding(media_id, dl.final_url, dl.size, _how(kp.target_bytes, None)):
                return self._shrink(dl, kp.target_bytes)
        return dl

    def _reencode(self, dl: Downloaded, bps: int) -> Downloaded:
        """A downloaded video at a bitrate (transcode.at_bitrate); as it is if it's no higher already."""
        try:
            out = transcode.at_bitrate(dl.tmp_path, bps, self.media_dir)
        except transcode.TranscodeError as exc:
            dl.tmp_path.unlink(missing_ok=True)
            raise MediaRejected(f"too large ({dl.size / 1_000_000:.1f} MB); couldn't transcode: {exc}") from None
        if out is None:
            return dl
        dl.tmp_path.unlink(missing_ok=True)
        return Downloaded(out, "video/mp4", out.stat().st_size, _sha256(out), dl.final_url, dl.size, dl.content_type)

    def _store(self, dl: Downloaded) -> str:
        ext = _EXT.get(dl.content_type) or mimetypes.guess_extension(dl.content_type) or ".bin"
        rel = Path(dl.sha256[:2]) / dl.sha256[2:4] / f"{dl.sha256}{ext}"
        dest = self.media_dir / rel
        if dest.exists():
            dl.tmp_path.unlink(missing_ok=True)
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dl.tmp_path.replace(dest)
        return rel.as_posix()

    def fetch_pending(self, limit: int = 20) -> int:
        now = utcnow()
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM media WHERE status='pending' AND (next_attempt_at IS NULL OR next_attempt_at<=?) "
                f"AND (held=0 OR (kept_only=0 AND episode=0 AND {WANTED_SQL}) "
                f"OR ((kept_only=1 OR episode=1) AND {KEPT_SQL}) OR (episode=1 AND wanted_at IS NOT NULL)) "
                "ORDER BY id LIMIT ?", (now, limit)).fetchall()
        for row in rows:
            self.fetch_one(row)
        return len(rows)

    def fetch_one(self, row: Any, wanted: bool = False) -> None:
        """`wanted`: fetch it even if it's a video or audio file nobody has
        opened yet (a post's audio, scrolled to in a feed)."""
        now = utcnow()
        with self.db.connect() as conn:
            policy = policy_for(conn, row["id"], self.env_policy.override(load_defaults(conn)))
            if row["kept_only"]:  # a YouTube video: only for kept posts, whoever asks
                wanted = bool(conn.execute(f"SELECT {KEPT_SQL} FROM media WHERE id=?", (row["id"],)).fetchone()[0])
            elif row["episode"]:  # a podcast episode: when played, or its post is kept (not just opened)
                wanted = bool(row["wanted_at"]) or bool(
                    conn.execute(f"SELECT {KEPT_SQL} FROM media WHERE id=?", (row["id"],)).fetchone()[0])
                policy = replace(policy, audio=replace(
                    policy.audio, keep_bytes=max(policy.audio.keep_bytes, self.episode_max_bytes)))
            else:
                wanted = wanted or bool(conn.execute(f"SELECT {WANTED_SQL} FROM media WHERE id=?",
                                                     (row["id"],)).fetchone()[0])
        try:
            if not policy.saves_any:
                raise MediaSkipped(f"archiving is off {POLICY_SUFFIX}")
            if row["kept_only"]:
                if not policy.video.save:
                    raise MediaSkipped(f"videos aren't archived {POLICY_SUFFIX}")
                if not wanted:
                    raise MediaHeld()
                # The YouTube page's settings, not the Videos ones: saved at
                # the resolution it says, as YouTube encoded it.
                dl = self._download_youtube(row["url"])
            else:
                if not wanted and (row["episode"] or looks_like_video(row["url"]) or looks_like_audio(row["url"])):
                    raise MediaHeld()
                dl = self._download(row["url"], policy, wanted)
                kp = policy.for_type(dl.content_type)
                if kp is not None:
                    dl = self._fit(dl, kp, media_id=row["id"])
            rel = self._store(dl)
        except MediaRejected as exc:
            with self.db.transaction() as conn:
                status = "skipped" if isinstance(exc, MediaSkipped) else "failed"
                conn.execute("UPDATE media SET status=?, error=?, attempts=attempts+1, fetched_at=? WHERE id=?",
                             (status, str(exc), now, row["id"]))
            return
        except MediaHeld:
            with self.db.transaction() as conn:
                conn.execute("UPDATE media SET held=1 WHERE id=?", (row["id"],))
            return
        except RemotePaused as exc:  # the server asked us to wait: not the file's fault
            with self.db.transaction() as conn:
                conn.execute("UPDATE media SET error=?, next_attempt_at=? WHERE id=?",
                             (str(exc), _after(now, exc.seconds), row["id"]))
            return
        except (httpx.HTTPError, OSError) as exc:
            attempts = row["attempts"] + 1
            with self.db.transaction() as conn:
                if attempts >= MAX_ATTEMPTS:
                    conn.execute("UPDATE media SET status='failed', error=?, attempts=? WHERE id=?",
                                 (f"{type(exc).__name__}: {exc}", attempts, row["id"]))
                else:
                    retry = fmt_ts(parse_ts(now) + timedelta(minutes=5 * 3 ** attempts))  # type: ignore[operator]
                    conn.execute("UPDATE media SET error=?, attempts=?, next_attempt_at=? WHERE id=?",
                                 (f"{type(exc).__name__}: {exc}", attempts, retry, row["id"]))
            return
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE media SET status='ok', content_type=?, size_bytes=?, sha256=?, storage_path=?, "
                "fetched_from=?, fetched_at=?, attempts=attempts+1, error=NULL, original_bytes=?, original_type=?, "
                "transcoded_at=?, transcode_error=NULL WHERE id=?",
                (dl.content_type, dl.size, dl.sha256, rel, dl.final_url, now, dl.original_size, dl.original_type,
                 now if dl.original_size else None, row["id"]),
            )
        if thumbs.eligible(dl.content_type):
            self.make_thumbs(rel, dl.sha256)

    def make_thumbs(self, storage_path: str, sha: str) -> None:
        """Smaller copies of a picture just stored, for browsing (thumbs.py)."""
        if not self.can_transcode():
            return
        with self.db.connect() as conn:
            want = thumbs.widths(conn)
        thumbs.make(self.media_dir, self.media_dir / storage_path, sha, want)


# --- garbage collection (only after purging expired auto threads) ----------

def collect_orphans(conn: Conn, media_dir: Path) -> list[Path]:
    """Delete media rows no object references; return files no row uses any more.
    The caller unlinks those files after the transaction commits."""
    orphans = conn.execute(
        "SELECT id, storage_path FROM media m WHERE NOT EXISTS (SELECT 1 FROM media_refs r WHERE r.media_id=m.id) "
        "AND NOT EXISTS (SELECT 1 FROM article_media a WHERE a.media_id=m.id)"
    ).fetchall()
    files: list[Path] = []
    for o in orphans:
        conn.execute("DELETE FROM listening WHERE media_id=?", (o["id"],))
        conn.execute("DELETE FROM media WHERE id=?", (o["id"],))
        if o["storage_path"]:
            still_used = conn.execute("SELECT 1 FROM media WHERE storage_path=? LIMIT 1",
                                      (o["storage_path"],)).fetchone()
            if not still_used:
                files.append(media_dir / o["storage_path"])
    return files
