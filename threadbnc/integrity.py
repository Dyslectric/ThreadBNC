"""Read-only consistency checks for the database and archived media."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .db import Database

CHUNK = 1024 * 1024


@dataclass
class Issue:
    severity: str
    kind: str
    detail: str


@dataclass
class Report:
    deep: bool
    database_check: str
    media_rows: int = 0
    media_files: int = 0
    media_bytes: int = 0
    checked_hashes: int = 0
    issues: list[Issue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(i.severity == "error" for i in self.issues)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok, "deep": self.deep, "database_check": self.database_check,
            "media_rows": self.media_rows, "media_files": self.media_files, "media_bytes": self.media_bytes,
            "checked_hashes": self.checked_hashes,
            "issues": [vars(i) for i in self.issues],
        }


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _count(conn: Any, sql: str) -> int:
    return int(conn.execute(sql).fetchone()[0])


def check(db: Database, conn: Any, media_dir: Path, deep: bool = False) -> Report:
    """Check logical references, stored-file metadata, and optionally hashes."""
    if db.is_postgres:
        db_check = "connected"
    else:
        pragma = "integrity_check" if deep else "quick_check"
        answers = [r[0] for r in conn.execute(f"PRAGMA {pragma}").fetchall()]
        db_check = "; ".join(answers)
    report = Report(deep=deep, database_check=db_check)
    if db_check.lower() not in ("ok", "connected"):
        report.issues.append(Issue("error", "database", db_check))

    relations = [
        ("thread root", "SELECT COUNT(*) FROM archived_threads t LEFT JOIN objects o ON o.id=t.root_object_id "
         "WHERE o.id IS NULL"),
        ("object thread", "SELECT COUNT(*) FROM objects o LEFT JOIN archived_threads t ON t.id=o.thread_id "
         "WHERE o.thread_id IS NOT NULL AND t.id IS NULL"),
        ("revision object", "SELECT COUNT(*) FROM revisions r LEFT JOIN objects o ON o.id=r.object_id "
         "WHERE o.id IS NULL"),
        ("local object id", "SELECT COUNT(*) FROM object_local_ids l LEFT JOIN objects o ON o.id=l.object_id "
         "WHERE o.id IS NULL"),
        ("media reference", "SELECT COUNT(*) FROM media_refs r LEFT JOIN media m ON m.id=r.media_id "
         "LEFT JOIN objects o ON o.id=r.object_id WHERE m.id IS NULL OR o.id IS NULL"),
        ("article reference", "SELECT COUNT(*) FROM article_refs r LEFT JOIN articles a ON a.id=r.article_id "
         "LEFT JOIN objects o ON o.id=r.object_id WHERE a.id IS NULL OR o.id IS NULL"),
        ("article media", "SELECT COUNT(*) FROM article_media r LEFT JOIN articles a ON a.id=r.article_id "
         "LEFT JOIN media m ON m.id=r.media_id WHERE a.id IS NULL OR m.id IS NULL"),
        ("trending picture", "SELECT COUNT(*) FROM stream_post_media s LEFT JOIN media m ON m.id=s.media_id "
         "WHERE m.id IS NULL"),
        ("custom feed", "SELECT COUNT(*) FROM custom_feed_communities x LEFT JOIN custom_feeds f ON f.id=x.feed_id "
         "LEFT JOIN communities c ON c.id=x.community_id WHERE f.id IS NULL OR c.id IS NULL"),
        ("revision counter", "SELECT COUNT(*) FROM objects o WHERE o.revision_count != COALESCE("
         "(SELECT MAX(r.seq) FROM revisions r WHERE r.object_id=o.id), 0)"),
        ("thread ownership", "SELECT COUNT(*) FROM archived_threads t JOIN objects o ON o.id=t.root_object_id "
         "WHERE o.thread_id IS NOT NULL AND o.thread_id != t.id"),
    ]
    for kind, sql in relations:
        number = _count(conn, sql)
        if number:
            report.issues.append(Issue("error", kind, f"{number:,} inconsistent row{'s' if number != 1 else ''}"))

    rows = conn.execute(
        "SELECT storage_path, MAX(size_bytes) AS size_bytes, MAX(sha256) AS sha256, COUNT(*) AS uses, "
        "COUNT(DISTINCT size_bytes) AS sizes, COUNT(DISTINCT sha256) AS hashes "
        "FROM media WHERE status='ok' GROUP BY storage_path ORDER BY storage_path"
    ).fetchall()
    report.media_rows = _count(conn, "SELECT COUNT(*) FROM media WHERE status='ok'")
    expected: set[str] = set()
    root = media_dir.resolve()
    for row in rows:
        rel = row["storage_path"]
        if not rel:
            report.issues.append(Issue("error", "media metadata", f"{row['uses']:,} archived row(s) have no path"))
            continue
        if row["size_bytes"] is None or not row["sha256"]:
            report.issues.append(Issue("error", "media metadata", f"{rel}: archived file has no size or checksum"))
        if row["sizes"] > 1 or row["hashes"] > 1:
            report.issues.append(Issue("error", "media metadata", f"{rel}: rows disagree about size or checksum"))
        path = (media_dir / rel).resolve()
        if not path.is_relative_to(root):
            report.issues.append(Issue("error", "unsafe media path", str(rel)))
            continue
        expected.add(path.as_posix().casefold())
        if not path.is_file():
            report.issues.append(Issue("error", "missing media", str(rel)))
            continue
        size = path.stat().st_size
        report.media_files += 1
        report.media_bytes += size
        if row["size_bytes"] is not None and size != row["size_bytes"]:
            report.issues.append(Issue("error", "media size", f"{rel}: database {row['size_bytes']:,}, disk {size:,}"))
        if deep and row["sha256"]:
            report.checked_hashes += 1
            actual = _hash(path)
            if actual != row["sha256"]:
                report.issues.append(Issue("error", "media checksum", f"{rel}: expected {row['sha256']}, got {actual}"))

    if media_dir.exists():
        for path in media_dir.rglob("*"):
            if not path.is_file() or "thumbs" in path.relative_to(media_dir).parts:
                continue
            if path.resolve().as_posix().casefold() not in expected:
                report.issues.append(Issue("warning", "unreferenced media", path.relative_to(media_dir).as_posix()))
    return report
