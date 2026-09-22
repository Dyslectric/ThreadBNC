"""Where the archive's space goes, for the Storage page: archived media and stored
text, broken down by kind of content, kind of thread and community. Linked
articles (articles.py) count as their text plus the pictures only they use.

Media files are stored once however many posts use them (media.py), so each
file counts once in a total. A file shared by two communities counts in both of
their rows, so the community rows can add up to more than the total.
"""

from __future__ import annotations

import shutil
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .db import Conn, Database

KINDS = {"pictures": "Pictures", "gifs": "GIFs", "videos": "Videos", "other": "Other files",
         "articles": "Article pictures"}
NOUNS = {"pictures": ("picture", "pictures"), "gifs": ("GIF", "GIFs"), "videos": ("video", "videos"),
         "other": ("other file", "other files"), "articles": ("article picture", "article pictures")}
THREAD_KINDS = {"manual": "Kept", "auto": "Auto-captured (followed communities)", "trash": "In the trash",
                None: "Not in a thread"}


def media_kind(content_type: str | None) -> str:
    ctype = content_type or ""
    if ctype == "image/gif":
        return "gifs"
    if ctype.startswith("image/"):
        return "pictures"
    return "videos" if ctype.startswith("video/") else "other"


@dataclass
class Usage:
    """Space used by one slice of the archive (a community, a kind of thread)."""
    text: int = 0          # bytes of stored titles, bodies, links and metadata, every revision
    article_text: int = 0  # bytes of linked articles' text
    articles: int = 0
    revisions: int = 0
    threads: int = 0
    media: dict[str, list[int]] = field(default_factory=lambda: {k: [0, 0] for k in KINDS})  # kind -> [files, bytes]

    @property
    def media_bytes(self) -> int:
        return sum(b for _n, b in self.media.values())

    @property
    def media_files(self) -> int:
        return sum(n for n, _b in self.media.values())

    @property
    def total(self) -> int:
        return self.text + self.article_text + self.media_bytes

    def add_files(self, paths: set[str], files: dict[str, tuple[int, str]]) -> None:
        for p in paths:
            size, kind = files[p]
            self.media[kind][0] += 1
            self.media[kind][1] += size


def _octets(db: Database, col: str) -> str:
    size = f"OCTET_LENGTH({col})" if db.is_postgres else f"LENGTH(CAST({col} AS BLOB))"
    return f"COALESCE({size}, 0)"


def _thread_kind_sql() -> str:
    return "CASE WHEN t.trashed_at IS NOT NULL THEN 'trash' ELSE t.retention END"


def database_bytes(db: Database, conn: Conn) -> int | None:
    if db.is_postgres:
        return conn.execute("SELECT pg_database_size(current_database())").fetchone()[0]
    path = Path(db.path)
    return sum(p.stat().st_size for p in (path, Path(f"{path}-wal"), Path(f"{path}-shm")) if p.exists()) or None


def overview(db: Database, conn: Conn, media_dir: Path) -> dict[str, Any]:
    # Each stored file once: path -> (size, kind).
    files: dict[str, tuple[int, str]] = {}
    saved = transcoded = 0
    for r in conn.execute("SELECT storage_path, MAX(size_bytes), MAX(content_type), MAX(original_bytes) FROM media "
                          "WHERE status='ok' AND storage_path IS NOT NULL GROUP BY storage_path"):
        files[r[0]] = (r[1] or 0, media_kind(r[2]))
        if r[3]:
            transcoded += 1
            saved += max(0, r[3] - (r[1] or 0))
    # Files only linked articles use (not the posts themselves) are article pictures.
    # (Stored files are shared by URL, so it's decided per file: used by an article, shown by no post itself.)
    own = {r[0] for r in conn.execute("SELECT m.storage_path FROM media m JOIN media_refs r ON r.media_id=m.id "
                                      "WHERE r.from_article=0 AND m.storage_path IS NOT NULL")}
    for r in conn.execute("SELECT m.storage_path FROM media m WHERE m.status='ok' AND m.storage_path IS NOT NULL "
                          "AND (EXISTS (SELECT 1 FROM media_refs r WHERE r.media_id=m.id AND r.from_article=1) "
                          "OR EXISTS (SELECT 1 FROM article_media a WHERE a.media_id=m.id))"):
        if r[0] in files and r[0] not in own:
            files[r[0]] = (files[r[0]][0], "articles")
    everything = Usage()
    everything.add_files(set(files), files)

    # Text, by community, post/comment and kind of thread.
    text_len = " + ".join(_octets(db, f"v.{c}") for c in ("title", "body", "url", "metadata_json"))
    by_type: dict[str, list[int]] = {"post": [0, 0, 0], "comment": [0, 0, 0]}  # [objects, revisions, bytes]
    communities: dict[int | None, Usage] = defaultdict(Usage)
    threads: dict[str | None, Usage] = {k: Usage() for k in THREAD_KINDS}
    for r in conn.execute(
            f"SELECT o.community_id, o.object_type, {_thread_kind_sql()} AS k, COUNT(*) AS revs, "
            f"COUNT(DISTINCT o.id) AS objs, SUM({text_len}) AS bytes FROM revisions v "
            "JOIN objects o ON o.id=v.object_id LEFT JOIN archived_threads t ON t.id=o.thread_id "
            "GROUP BY o.community_id, o.object_type, k"):
        bytes_ = r["bytes"] or 0
        row = by_type[r["object_type"]]
        row[0] += r["objs"]
        row[1] += r["revs"]
        row[2] += bytes_
        for usage in (communities[r["community_id"]], threads[r["k"]]):
            usage.text += bytes_
            usage.revisions += r["revs"]
        everything.text += bytes_
        everything.revisions += r["revs"]

    # Media, counted once per community and once per kind of thread.
    paths_c: dict[int | None, set[str]] = defaultdict(set)
    paths_t: dict[str | None, set[str]] = defaultdict(set)
    for r in conn.execute(
            f"SELECT DISTINCT o.community_id, {_thread_kind_sql()} AS k, m.storage_path FROM media m "
            "JOIN media_refs r ON r.media_id=m.id JOIN objects o ON o.id=r.object_id "
            "LEFT JOIN archived_threads t ON t.id=o.thread_id "
            "WHERE m.status='ok' AND m.storage_path IS NOT NULL"):
        if r["storage_path"] in files:
            paths_c[r["community_id"]].add(r["storage_path"])
            paths_t[r["k"]].add(r["storage_path"])
    for cid, paths in paths_c.items():
        communities[cid].add_files(paths, files)
    for k, paths in paths_t.items():
        threads.setdefault(k, Usage()).add_files(paths, files)
    seen: dict[str, int] = defaultdict(int)
    for paths in paths_c.values():
        for p in paths:
            seen[p] += 1
    shared = sum(files[p][0] for p, n in seen.items() if n > 1)

    # Linked articles' text, counted once overall and once per community and kind of thread.
    article_len = _octets(db, "a.content_html")
    row = conn.execute(f"SELECT COUNT(*), SUM({article_len}) FROM articles a WHERE a.status='ok'").fetchone()
    everything.articles, everything.article_text = row[0], row[1] or 0
    for r in conn.execute(
            f"SELECT x.community_id, x.k, COUNT(*) AS n, SUM({article_len}) AS bytes FROM articles a JOIN ("
            f"SELECT DISTINCT o.community_id, {_thread_kind_sql()} AS k, ar.article_id FROM article_refs ar "
            "JOIN objects o ON o.id=ar.object_id LEFT JOIN archived_threads t ON t.id=o.thread_id) x "
            "ON x.article_id=a.id WHERE a.status='ok' GROUP BY x.community_id, x.k"):
        for usage in (communities[r["community_id"]], threads.setdefault(r["k"], Usage())):
            usage.articles += r["n"]
            usage.article_text += r["bytes"] or 0

    for r in conn.execute(f"SELECT t.community_id, {_thread_kind_sql()} AS k, COUNT(*) AS n "
                          "FROM archived_threads t GROUP BY t.community_id, k"):
        communities[r["community_id"]].threads += r["n"]
        threads[r["k"]].threads += r["n"]

    names = {r["id"]: r for r in conn.execute("SELECT id, name, canonical_ap_id FROM communities")}
    rows = sorted(((names.get(cid), u) for cid, u in communities.items() if u.total or u.threads),
                  key=lambda cu: -cu[1].total)

    kinds = [{"label": KINDS[k], "count": everything.media[k][0], "bytes": everything.media[k][1]}
             for k in KINDS if everything.media[k][0] and k != "articles"]
    pictures = everything.media["articles"]
    if everything.articles or pictures[0]:
        kinds.append({"label": "Linked articles", "count": everything.articles,
                      "bytes": everything.article_text + pictures[1],
                      "parts": {"text": everything.article_text, "pictures": pictures[0],
                                "picture_bytes": pictures[1]}})
    kinds += [{"label": "Posts" if t == "post" else "Comments", "count": by_type[t][0],
               "bytes": by_type[t][2],
               "note": f"{by_type[t][1]:,} version{'s' if by_type[t][1] != 1 else ''}"}
              for t in by_type if by_type[t][0]]
    kinds.sort(key=lambda k: -k["bytes"])
    try:
        free = shutil.disk_usage(media_dir).free if media_dir.exists() else None
    except OSError:
        free = None
    return {
        "everything": everything, "kinds": kinds,
        "threads": [(THREAD_KINDS[k], u) for k, u in threads.items() if u.total or u.threads],
        "communities": rows, "shared": shared, "saved": saved, "transcoded": transcoded,
        "database": database_bytes(db, conn), "free": free,
    }
