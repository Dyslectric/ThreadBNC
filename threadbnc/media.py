"""Media archiving: images/GIFs/short videos referenced by retained content.

Media is registered when a revision is recorded and downloaded later by the
bouncer (never while holding the DB write lock). Files are content-addressed
under <data_dir>/media and are only ever removed when every object referencing
them has been purged (expired auto-captured threads).
"""

from __future__ import annotations

import hashlib
import ipaddress
import mimetypes
import socket
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin, urlparse

import httpx

from .adapters.http import HostThrottle
from .db import Database, fmt_ts, parse_ts, utcnow
from .render import MediaInfo, extract_media_urls, looks_like_media

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
    return urls


def skip_unprobed_links(conn: sqlite3.Connection) -> int:
    """Stop pending downloads of post links registered before we stopped probing
    non-media links. Embedded images and thumbnails are left alone."""
    wanted: set[str] = set()
    for r in conn.execute("SELECT body FROM revisions WHERE body IS NOT NULL"):
        wanted.update(extract_media_urls(r["body"]))
    wanted.update(r[0] for r in conn.execute("SELECT thumbnail_url FROM objects WHERE thumbnail_url IS NOT NULL"))
    n = 0
    for r in conn.execute("SELECT id, url FROM media WHERE status='pending'").fetchall():
        if r["url"] not in wanted and not looks_like_media(r["url"]):
            conn.execute("UPDATE media SET status='skipped', error='post link is not an image; not fetched' "
                         "WHERE id=?", (r["id"],))
            n += 1
    return n


def register(conn: sqlite3.Connection, object_id: int, urls: Iterable[str], now: str) -> None:
    for url in urls:
        conn.execute("INSERT OR IGNORE INTO media(url, first_seen_at, next_attempt_at) VALUES (?,?,?)",
                     (url, now, now))
        mid = conn.execute("SELECT id FROM media WHERE url=?", (url,)).fetchone()[0]
        conn.execute("INSERT OR IGNORE INTO media_refs(object_id, media_id, first_seen_at) VALUES (?,?,?)",
                     (object_id, mid, now))


def register_all_existing(conn: sqlite3.Connection) -> None:
    """Backfill refs for revisions stored before media archiving existed."""
    now = utcnow()
    for r in conn.execute("SELECT object_id, title, body, url FROM revisions").fetchall():
        register(conn, r["object_id"], media_candidates(r["title"], r["body"], r["url"]), now)


def lookup_for_objects(conn: sqlite3.Connection, object_ids: list[int]) -> dict[str, MediaInfo]:
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


class MediaFetcher:
    def __init__(self, db: Database, media_dir: Path, user_agent: str, max_bytes: int,
                 timeout: float = 30.0, client: httpx.Client | None = None, check_host: bool = True,
                 throttle: HostThrottle | None = None):
        self.db = db
        self.throttle = throttle or HostThrottle(1.0)
        self.media_dir = media_dir
        self.max_bytes = max_bytes
        self.check_host = check_host
        self.client = client or httpx.Client(timeout=timeout, headers={"User-Agent": user_agent},
                                             follow_redirects=False)

    def _download(self, url: str) -> Downloaded:
        if urlparse(url).path.lower().endswith(".gifv"):  # imgur-style: the real file is .mp4
            url = url[: -len(".gifv")] + ".mp4"
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            if self.check_host:
                _assert_public_host(current)
            self.throttle.wait((urlparse(current).hostname or "").lower())
            with self.client.stream("GET", current) as resp:
                if resp.status_code in (301, 302, 303, 307, 308) and "location" in resp.headers:
                    current = urljoin(current, resp.headers["location"])
                    continue
                if resp.status_code == 404 or resp.status_code == 410:
                    raise MediaRejected(f"HTTP {resp.status_code}")
                if resp.status_code >= 400:
                    raise httpx.HTTPStatusError(f"HTTP {resp.status_code}", request=resp.request, response=resp)
                declared = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
                length = int(resp.headers.get("content-length") or 0)
                if declared.startswith(("text/", "application/json", "application/xhtml")):
                    raise MediaRejected(f"not media ({declared})")
                if length > self.max_bytes:
                    raise MediaRejected(f"too large ({length // 1_000_000} MB)")
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
                            if size > self.max_bytes:
                                raise MediaRejected(f"too large (> {self.max_bytes // 1_000_000} MB)")
                            h.update(chunk)
                            tmp.write(chunk)
                    ctype = sniff(head) or declared
                    if not ctype.startswith(("image/", "video/")):
                        raise MediaRejected(f"not media ({declared or 'unknown type'})")
                except BaseException:
                    Path(tmp.name).unlink(missing_ok=True)
                    raise
                return Downloaded(Path(tmp.name), ctype, size, h.hexdigest(), current)
        raise MediaRejected("too many redirects")

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
                "ORDER BY id LIMIT ?", (now, limit)).fetchall()
        for row in rows:
            self.fetch_one(row)
        return len(rows)

    def fetch_one(self, row: sqlite3.Row) -> None:
        now = utcnow()
        try:
            dl = self._download(row["url"])
            rel = self._store(dl)
        except MediaRejected as exc:
            with self.db.transaction() as conn:
                status = "skipped" if str(exc).startswith("not media") else "failed"
                conn.execute("UPDATE media SET status=?, error=?, attempts=attempts+1, fetched_at=? WHERE id=?",
                             (status, str(exc), now, row["id"]))
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
                "fetched_from=?, fetched_at=?, attempts=attempts+1, error=NULL WHERE id=?",
                (dl.content_type, dl.size, dl.sha256, rel, dl.final_url, now, row["id"]),
            )


# --- garbage collection (only after purging expired auto threads) ----------

def collect_orphans(conn: sqlite3.Connection, media_dir: Path) -> list[Path]:
    """Delete media rows no object references; return files no row uses any more.
    The caller unlinks those files after the transaction commits."""
    orphans = conn.execute(
        "SELECT id, storage_path FROM media m WHERE NOT EXISTS (SELECT 1 FROM media_refs r WHERE r.media_id=m.id)"
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
