"""Portable, credential-free archive bundles and their verifier."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Iterator

from . import opml
from .db import Database

FORMAT = "threadbnc-portable-1"
TABLES = (
    "instances", "actors", "communities", "community_follows", "archived_threads", "objects",
    "object_local_ids", "revisions", "state_events", "media", "media_refs", "articles", "article_media",
    "article_links", "article_refs", "article_keys", "discussions", "custom_feeds", "custom_feed_communities",
    "listening",
)
README = """ThreadBNC portable archive

This bundle is a credential-free, machine-readable copy of archived content.
Rows are UTF-8 JSON Lines under data/. Stored media is under media/, using the
same content-addressed paths as ThreadBNC. subscriptions.opml contains portable
RSS and Atom follows. SHA256SUMS covers every other member.

This is an interchange/export format, not a replacement for a full operational
backup. It intentionally excludes account tokens, inbox state, moderation
credentials, queued jobs, and application secrets. See ThreadBNC's README for
database-and-media backup restoration and verification.
"""


def _row_dict(row: Any) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def _safe_media_path(media_dir: Path, rel: str) -> Path | None:
    root = media_dir.resolve()
    path = (media_dir / rel).resolve()
    return path if path.is_relative_to(root) and path.is_file() else None


def create(db: Database, media_dir: Path, output: Path) -> dict[str, Any]:
    """Create a portable ZIP atomically and return its manifest."""
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=output.name + ".", suffix=".tmp", dir=output.parent)
    os.close(fd)
    temp_path = Path(temporary)
    hashes: list[tuple[str, str]] = []
    counts: dict[str, int] = {}
    missing: list[str] = []

    def put(zf: zipfile.ZipFile, name: str, data: bytes) -> None:
        zf.writestr(name, data)
        hashes.append((hashlib.sha256(data).hexdigest(), name))

    def put_stream(zf: zipfile.ZipFile, name: str, chunks: Iterable[bytes]) -> None:
        digest = hashlib.sha256()
        with zf.open(name, "w") as dest:
            for chunk in chunks:
                digest.update(chunk)
                dest.write(chunk)
        hashes.append((digest.hexdigest(), name))

    def table_lines(conn: Any, table: str) -> Iterator[bytes]:
        counts[table] = 0
        for row in conn.execute(f"SELECT * FROM {table} ORDER BY 1"):
            counts[table] += 1
            yield (json.dumps(_row_dict(row), ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")

    def file_chunks(path: Path) -> Iterator[bytes]:
        with path.open("rb") as source:
            yield from iter(lambda: source.read(1024 * 1024), b"")

    try:
        with db.connect() as conn, zipfile.ZipFile(temp_path, "w", compression=zipfile.ZIP_DEFLATED,
                                                   compresslevel=6, allowZip64=True) as zf:
            put(zf, "README.txt", README.encode())
            put(zf, "subscriptions.opml", opml.export(opml.followed_rows(conn)))
            for table in TABLES:
                put_stream(zf, f"data/{table}.jsonl", table_lines(conn, table))
            media_rows = conn.execute(
                "SELECT DISTINCT storage_path FROM media WHERE status='ok' AND storage_path IS NOT NULL "
                "ORDER BY storage_path").fetchall()
            stored = 0
            for row in media_rows:
                rel = str(row["storage_path"])
                path = _safe_media_path(media_dir, rel)
                if path is None:
                    missing.append(rel)
                    continue
                put_stream(zf, "media/" + PurePosixPath(rel).as_posix(), file_chunks(path))
                stored += 1
            manifest = {
                "format": FORMAT,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "database": "postgres" if db.is_postgres else "sqlite",
                "tables": counts,
                "media_files": stored,
                "missing_media": missing,
                "credentials_included": False,
            }
            put(zf, "manifest.json", (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode())
            sums = "".join(f"{digest}  {name}\n" for digest, name in sorted(hashes, key=lambda item: item[1]))
            zf.writestr("SHA256SUMS", sums.encode())
        os.replace(temp_path, output)
        return manifest
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise


def verify(path: Path) -> dict[str, Any]:
    """Verify member names, manifest, and every recorded SHA-256 digest."""
    issues: list[str] = []
    checked = 0
    try:
        with zipfile.ZipFile(path) as zf:
            infos = zf.infolist()
            names = [info.filename for info in infos]
            if len(names) != len(set(names)):
                issues.append("archive contains duplicate member names")
            for name in names:
                pure = PurePosixPath(name)
                if pure.is_absolute() or ".." in pure.parts or "\\" in name:
                    issues.append(f"unsafe member name: {name}")
            try:
                manifest = json.loads(zf.read("manifest.json"))
            except (KeyError, json.JSONDecodeError) as exc:
                manifest = {}
                issues.append(f"manifest.json is missing or invalid: {exc}")
            if manifest.get("format") != FORMAT:
                issues.append(f"unsupported format: {manifest.get('format')!r}")
            if manifest.get("credentials_included") is not False:
                issues.append("manifest does not confirm that credentials were excluded")
            for rel in manifest.get("missing_media") or []:
                issues.append(f"export was created without media file: {rel}")
            try:
                sums = zf.read("SHA256SUMS").decode("utf-8").splitlines()
            except (KeyError, UnicodeDecodeError) as exc:
                sums = []
                issues.append(f"SHA256SUMS is missing or invalid: {exc}")
            recorded: set[str] = set()
            for line in sums:
                if "  " not in line:
                    issues.append(f"invalid checksum line: {line[:120]}")
                    continue
                wanted, name = line.split("  ", 1)
                recorded.add(name)
                try:
                    digest = hashlib.sha256()
                    with zf.open(name) as source:
                        for chunk in iter(lambda: source.read(1024 * 1024), b""):
                            digest.update(chunk)
                    checked += 1
                    if digest.hexdigest() != wanted:
                        issues.append(f"checksum mismatch: {name}")
                except KeyError:
                    issues.append(f"checksummed member is missing: {name}")
            expected = set(names) - {"SHA256SUMS"}
            for name in sorted(expected - recorded):
                issues.append(f"member has no checksum: {name}")
            for name in sorted(recorded - expected):
                issues.append(f"checksum names absent member: {name}")
            for table, wanted_count in (manifest.get("tables") or {}).items():
                name = f"data/{table}.jsonl"
                try:
                    count = 0
                    with zf.open(name) as source:
                        for number, line in enumerate(source, 1):
                            if not line.strip():
                                continue
                            count += 1
                            try:
                                json.loads(line)
                            except json.JSONDecodeError as exc:
                                issues.append(f"invalid JSON in {name} line {number}: {exc}")
                    if count != wanted_count:
                        issues.append(f"row count mismatch for {table}: manifest {wanted_count}, data {count}")
                except KeyError:
                    issues.append(f"manifest table is missing: {name}")
            media_count = sum(1 for name in names if name.startswith("media/") and not name.endswith("/"))
            if manifest.get("media_files") is not None and media_count != manifest["media_files"]:
                issues.append(f"media count mismatch: manifest {manifest['media_files']}, data {media_count}")
    except (OSError, zipfile.BadZipFile) as exc:
        issues.append(f"cannot read ZIP: {exc}")
        manifest = {}
    return {"ok": not issues, "checked": checked, "issues": issues, "manifest": manifest}
