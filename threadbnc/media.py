"""Media archiving: images/GIFs/short videos referenced by retained content.

Media is registered when a revision is recorded and downloaded later by the
bouncer (never while holding the DB write lock). Files are content-addressed
under <data_dir>/media and are only ever removed when every object referencing
them has been purged (expired auto-captured threads).

Each community can say what gets archived (everything, pictures only, nothing),
how big a file may be, and whether files over that are transcoded down to fit
(see transcode.py) instead of given up on. Media shared between communities gets
the most generous of their settings.

Pictures (and video thumbnails, which are pictures) are downloaded as soon as
they're seen. Full videos wait until a post showing them is opened or kept
(`held`): most scroll past unwatched, and they're by far the biggest files.
YouTube videos (`kept_only`) wait longer still: they're downloaded with yt-dlp
only once a post linking to one is kept (see youtube.py).
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import mimetypes
import socket
import tempfile
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urljoin, urlparse

import httpx

from . import transcode, youtube
from .adapters.base import RemotePaused
from .adapters.http import HostThrottle
from .db import Conn, Database, fmt_ts, parse_ts, utcnow
from .render import VIDEO_EXTENSIONS, MediaInfo, extract_media_urls, looks_like_media

MAX_ATTEMPTS = 5
MAX_REDIRECTS = 5
CHUNK = 64 * 1024

_MAGIC = [
    (b"GIF87a", "image/gif"), (b"GIF89a", "image/gif"), (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"), (b"\x1aE\xdf\xa3", "video/webm"), (b"BM", "image/bmp"),
]
_EXT = {"image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif", "image/webp": ".webp",
        "image/avif": ".avif", "video/mp4": ".mp4", "video/webm": ".webm", "image/svg+xml": ".svg",
        "video/quicktime": ".mov", "image/bmp": ".bmp", "image/heic": ".heic"}


class MediaRejected(Exception):
    """Permanent: not media, too large, or a forbidden address."""


class MediaSkipped(MediaRejected):
    """Not wanted: not media at all, or a community's settings leave it out."""


class MediaHeld(Exception):
    """A video nobody has opened yet: wait until someone does."""


# A file is wanted in full once a post (or comment) showing it is in a thread
# you opened or kept. Until then, videos are held.
WANTED_SQL = ("EXISTS (SELECT 1 FROM media_refs wr JOIN objects wo ON wo.id=wr.object_id "
              "JOIN archived_threads wt ON wt.id=wo.thread_id WHERE wr.media_id=media.id "
              "AND (wt.retention='manual' OR wt.opened_at IS NOT NULL))")
# A YouTube video is wanted only once a post linking to it is kept.
KEPT_SQL = ("EXISTS (SELECT 1 FROM media_refs kr JOIN objects ko ON ko.id=kr.object_id "
            "JOIN archived_threads kt ON kt.id=ko.thread_id WHERE kr.media_id=media.id "
            "AND kt.retention='manual' AND kt.trashed_at IS NULL)")


def looks_like_video(url: str) -> bool:
    return urlparse(url).path.lower().endswith(VIDEO_EXTENSIONS)


ARCHIVE_MODES = {"all": "Pictures and videos", "images": "Pictures only", "off": "Nothing"}
_RANK = {"off": 0, "images": 1, "all": 2}
# Errors that a change of settings can fix; requeue() retries these.
POLICY_SUFFIX = "for this community"


@dataclass(frozen=True)
class MediaPolicy:
    archive: str = "all"  # all | images | off
    max_bytes: int = 25_000_000
    transcode: bool = False  # shrink files over max_bytes with ffmpeg

    def override(self, archive: str | None, max_mb: int | None, transcode_: int | None) -> MediaPolicy:
        """This policy with a community's own choices (NULL = keep the default) applied."""
        return MediaPolicy(archive if archive in ARCHIVE_MODES else self.archive,
                           max_mb * 1_000_000 if max_mb else self.max_bytes,
                           self.transcode if transcode_ is None else bool(transcode_))

    def merge(self, other: MediaPolicy) -> MediaPolicy:
        """The more generous of two policies, for media several communities share."""
        return MediaPolicy(max(self.archive, other.archive, key=_RANK.__getitem__),
                           max(self.max_bytes, other.max_bytes), self.transcode or other.transcode)


def policy_for(conn: Conn, media_id: int, default: MediaPolicy) -> MediaPolicy:
    rows = conn.execute(
        "SELECT DISTINCT c.media_archive, c.media_max_mb, c.media_transcode FROM media_refs r "
        "JOIN objects o ON o.id=r.object_id LEFT JOIN communities c ON c.id=o.community_id WHERE r.media_id=?",
        (media_id,)).fetchall()
    if not rows:
        return default
    policies = [default.override(r[0], r[1], r[2]) for r in rows]
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
DEFAULT_KEYS = ("media_archive", "media_max_mb", "media_transcode")


def save_defaults(conn: Conn, archive: str | None, max_mb: int | None, transcode_: bool | None) -> None:
    """None for a setting goes back to the environment's."""
    for key, value in zip(DEFAULT_KEYS, (archive, max_mb, None if transcode_ is None else int(transcode_))):
        if value is None:
            conn.execute("DELETE FROM app_settings WHERE key=?", (key,))
        else:
            conn.execute("INSERT INTO app_settings(key, value) VALUES (?, ?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))


def load_defaults(conn: Conn) -> tuple[str | None, int | None, int | None]:
    """(archive, max MB, transcode) as saved; None where unset."""
    marks = ",".join("?" * len(DEFAULT_KEYS))
    saved = {r[0]: r[1] for r in conn.execute(f"SELECT key, value FROM app_settings WHERE key IN ({marks})",
                                              DEFAULT_KEYS).fetchall()}
    mb, tc = saved.get("media_max_mb"), saved.get("media_transcode")
    return saved.get("media_archive"), int(mb) if mb else None, int(tc) if tc is not None else None


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
        "AND original_bytes IS NOT NULL ORDER BY fetched_at DESC LIMIT 10", (community_id,)).fetchall()
    return {"counts": counts, "problems": problems, "transcoded": transcoded}


def sniff(head: bytes) -> str | None:
    for magic, ctype in _MAGIC:
        if head.startswith(magic):
            return ctype
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in (b"avif", b"avis"):
            return "image/avif"
        if brand.startswith(b"heic") or brand in (b"heix", b"mif1"):
            return "image/heic"
        return "video/quicktime" if brand == b"qt  " else "video/mp4"
    return None


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


def media_id(conn: Conn, url: str, now: str) -> int:
    """The media row for a URL, registered (to be downloaded) if it's new.
    YouTube videos start out held until a post linking to them is kept."""
    yt = int(bool(youtube.video_id(url)))
    conn.execute("INSERT INTO media(url, first_seen_at, next_attempt_at, held, kept_only) VALUES (?,?,?,?,?) "
                 "ON CONFLICT(url) DO NOTHING", (url, now, now, yt, yt))
    return conn.execute("SELECT id FROM media WHERE url=?", (url,)).fetchone()[0]


def register(conn: Conn, object_id: int, urls: Iterable[str], now: str, from_article: bool = False) -> None:
    """`from_article`: pictures in the article the post links to (articles.py),
    which aren't shown as the post's own pictures in feeds."""
    # A picture that's the post's own as well as the article's counts as its own.
    on_conflict = "DO NOTHING" if from_article else "DO UPDATE SET from_article=0 WHERE media_refs.from_article=1"
    for url in urls:
        mid = media_id(conn, url, now)
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
    return {r["url"]: MediaInfo(r["id"], r["status"], r["content_type"], r["error"]) for r in rows}


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
                 youtube_session: youtube.YouTubeSession | None = None):
        self.db = db
        self.youtube = youtube_session
        self.throttle = throttle or HostThrottle(1.0)
        self.media_dir = media_dir
        self.env_policy = MediaPolicy("all", max_bytes, transcode_default)
        # Files are downloaded up to this size when they might be transcoded down.
        self.source_max_bytes = transcode_source_max_bytes
        self.check_host = check_host
        self.client = client or httpx.Client(timeout=timeout, headers={"User-Agent": user_agent},
                                             follow_redirects=False)

    @property
    def default_policy(self) -> MediaPolicy:
        """What communities that haven't chosen get: the environment's settings
        with the ones saved on the Storage page applied."""
        with self.db.connect() as conn:
            return self.env_policy.override(*load_defaults(conn))

    def can_transcode(self) -> bool:
        return transcode.available()

    def _download(self, url: str, policy: MediaPolicy | None = None, wanted: bool = True) -> Downloaded:
        """`wanted` False: a video is held (MediaHeld) as soon as the server says it's one."""
        policy = policy or self.default_policy
        shrinkable = policy.transcode and self.can_transcode()
        cap = max(policy.max_bytes, self.source_max_bytes) if shrinkable else policy.max_bytes
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
                if policy.archive == "images" and declared.startswith("video/"):
                    raise MediaSkipped(f"videos aren't archived {POLICY_SUFFIX}")
                if not wanted and declared.startswith("video/"):
                    raise MediaHeld()
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
                    ctype = sniff(head) or declared
                    if not ctype.startswith(("image/", "video/")):
                        raise MediaSkipped(f"not media ({declared or 'unknown type'})")
                    if policy.archive == "images" and ctype.startswith("video/"):
                        raise MediaSkipped(f"videos aren't archived {POLICY_SUFFIX}")
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
                f"AND (held=0 OR (kept_only=0 AND {WANTED_SQL}) OR (kept_only=1 AND {KEPT_SQL})) "
                "ORDER BY id LIMIT ?", (now, limit)).fetchall()
        for row in rows:
            self.fetch_one(row)
        return len(rows)

    def fetch_one(self, row: Any) -> None:
        now = utcnow()
        with self.db.connect() as conn:
            policy = policy_for(conn, row["id"], self.env_policy.override(*load_defaults(conn)))
            wanted = bool(conn.execute(f"SELECT {KEPT_SQL if row['kept_only'] else WANTED_SQL} FROM media "
                                       "WHERE id=?", (row["id"],)).fetchone()[0])
        try:
            if policy.archive == "off":
                raise MediaSkipped(f"archiving is off {POLICY_SUFFIX}")
            if row["kept_only"]:
                if policy.archive == "images":
                    raise MediaSkipped(f"videos aren't archived {POLICY_SUFFIX}")
                if not wanted:
                    raise MediaHeld()
                dl = self._download_youtube(row["url"])
            else:
                if not wanted and looks_like_video(row["url"]):
                    raise MediaHeld()
                dl = self._download(row["url"], policy, wanted)
                if dl.size > policy.max_bytes:
                    dl = self._shrink(dl, policy.max_bytes)
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
                "fetched_from=?, fetched_at=?, attempts=attempts+1, error=NULL, original_bytes=?, original_type=? "
                "WHERE id=?",
                (dl.content_type, dl.size, dl.sha256, rel, dl.final_url, now, dl.original_size, dl.original_type,
                 row["id"]),
            )


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
        conn.execute("DELETE FROM media WHERE id=?", (o["id"],))
        if o["storage_path"]:
            still_used = conn.execute("SELECT 1 FROM media WHERE storage_path=? LIMIT 1",
                                      (o["storage_path"],)).fetchone()
            if not still_used:
                files.append(media_dir / o["storage_path"])
    return files
