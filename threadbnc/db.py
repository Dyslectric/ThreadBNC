"""SQLite storage. Schema is append-oriented: revisions and state_events are
never updated or deleted, except when a thread is purged (an expired
auto-captured thread, or one whose time in the trash has run out)."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS instances (
    id INTEGER PRIMARY KEY,
    domain TEXT NOT NULL UNIQUE,
    software TEXT,
    software_version TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    last_successful_contact_at TEXT,
    last_failure_at TEXT,
    last_error TEXT,
    available INTEGER NOT NULL DEFAULT 1,
    software_checked_at TEXT
);

CREATE TABLE IF NOT EXISTS actors (
    id INTEGER PRIMARY KEY,
    canonical_ap_id TEXT NOT NULL UNIQUE,
    username TEXT,
    instance TEXT,
    display_name TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS communities (
    id INTEGER PRIMARY KEY,
    canonical_ap_id TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    title TEXT,
    instance_id INTEGER REFERENCES instances(id),
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    cur_removed INTEGER NOT NULL DEFAULT 0,
    cur_deleted INTEGER NOT NULL DEFAULT 0
);

-- A followed community: bouncer polls its new posts and auto-captures them
-- for retention_days (NULL = keep forever).
CREATE TABLE IF NOT EXISTS community_follows (
    community_id INTEGER PRIMARY KEY REFERENCES communities(id),
    active INTEGER NOT NULL DEFAULT 1,
    followed_at TEXT NOT NULL,
    unfollowed_at TEXT,
    capture_since TEXT NOT NULL,
    poll_interval_minutes INTEGER NOT NULL,
    retention_days INTEGER,
    source_domain TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    last_polled_at TEXT,
    next_poll_at TEXT,
    last_error TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS archived_threads (
    id INTEGER PRIMARY KEY,
    root_object_id INTEGER UNIQUE REFERENCES objects(id),
    community_id INTEGER REFERENCES communities(id),
    source_url TEXT NOT NULL,
    source_domain TEXT NOT NULL,
    source_local_id TEXT NOT NULL,
    retention TEXT NOT NULL CHECK (retention IN ('manual', 'auto')),
    retained_at TEXT NOT NULL,
    promoted_at TEXT,
    expires_at TEXT,
    last_checked_at TEXT,
    last_success_at TEXT,
    next_check_at TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    last_viewed_at TEXT,
    prev_viewed_at TEXT,
    trashed_at TEXT,          -- non-NULL = in the trash (retention kept for restore)
    trash_expires_at TEXT,    -- permanent deletion time; NULL = trash kept forever
    last_full_fetch_at TEXT,  -- last time the whole comment tree was fetched
    remote_comment_count INTEGER,
    remote_newest_comment_at TEXT
);

CREATE TABLE IF NOT EXISTS objects (
    id INTEGER PRIMARY KEY,
    canonical_ap_id TEXT NOT NULL UNIQUE,
    object_type TEXT NOT NULL CHECK (object_type IN ('post', 'comment')),
    parent_id INTEGER REFERENCES objects(id),
    root_post_id INTEGER REFERENCES objects(id),
    thread_id INTEGER REFERENCES archived_threads(id),
    author_id INTEGER REFERENCES actors(id),
    community_id INTEGER REFERENCES communities(id),
    created_at TEXT,
    remote_updated_at TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    discovered_late INTEGER NOT NULL DEFAULT 0,
    -- cached *current observed* state; history lives in state_events
    cur_deleted INTEGER NOT NULL DEFAULT 0,
    cur_removed INTEGER NOT NULL DEFAULT 0,
    cur_locked INTEGER NOT NULL DEFAULT 0,
    cur_missing INTEGER NOT NULL DEFAULT 0,
    score INTEGER,
    reply_count INTEGER,
    thumbnail_url TEXT,  -- current preview image; archived via media
    upvotes INTEGER,
    downvotes INTEGER,
    revision_count INTEGER NOT NULL DEFAULT 0,
    last_changed_at TEXT
);
CREATE INDEX IF NOT EXISTS objects_thread ON objects(thread_id);
CREATE INDEX IF NOT EXISTS objects_parent ON objects(parent_id);

-- Remote API ids are per-server; never globally meaningful.
CREATE TABLE IF NOT EXISTS object_local_ids (
    object_id INTEGER NOT NULL REFERENCES objects(id),
    domain TEXT NOT NULL,
    local_id TEXT NOT NULL,
    PRIMARY KEY (domain, local_id)
);

CREATE TABLE IF NOT EXISTS revisions (
    id INTEGER PRIMARY KEY,
    object_id INTEGER NOT NULL REFERENCES objects(id),
    seq INTEGER NOT NULL,
    observed_at TEXT NOT NULL,
    remote_updated_at TEXT,
    title TEXT,
    body TEXT,
    url TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    content_hash TEXT NOT NULL,
    UNIQUE (object_id, seq)
);

CREATE TABLE IF NOT EXISTS state_events (
    id INTEGER PRIMARY KEY,
    object_id INTEGER REFERENCES objects(id),
    thread_id INTEGER REFERENCES archived_threads(id),
    community_id INTEGER REFERENCES communities(id),
    instance_id INTEGER REFERENCES instances(id),
    event_type TEXT NOT NULL,
    attribution TEXT,
    actor_id INTEGER REFERENCES actors(id),
    observed_at TEXT NOT NULL,
    remote_timestamp TEXT,
    reason TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS events_object ON state_events(object_id);
CREATE INDEX IF NOT EXISTS events_thread ON state_events(thread_id);
CREATE INDEX IF NOT EXISTS events_observed ON state_events(observed_at);

-- Archived media. Keyed by the URL as written in the content; files are stored
-- content-addressed (sha256) so identical files are kept once.
CREATE TABLE IF NOT EXISTS media (
    id INTEGER PRIMARY KEY,
    url TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'ok', 'failed', 'skipped')),
    content_type TEXT,
    size_bytes INTEGER,
    sha256 TEXT,
    storage_path TEXT,
    fetched_from TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    error TEXT,
    first_seen_at TEXT NOT NULL,
    fetched_at TEXT
);
CREATE INDEX IF NOT EXISTS media_pending ON media(status, next_attempt_at);
CREATE INDEX IF NOT EXISTS media_sha ON media(sha256);

-- Which objects (in any revision) referenced which media.
CREATE TABLE IF NOT EXISTS media_refs (
    object_id INTEGER NOT NULL REFERENCES objects(id),
    media_id INTEGER NOT NULL REFERENCES media(id),
    first_seen_at TEXT NOT NULL,
    PRIMARY KEY (object_id, media_id)
);
CREATE INDEX IF NOT EXISTS media_refs_media ON media_refs(media_id);

CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    run_after TEXT NOT NULL,
    result_json TEXT,
    error TEXT
);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status, run_after);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    v = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def fmt_ts(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def dumps(value: Any) -> str:
    return json.dumps(value or {}, sort_keys=True, ensure_ascii=False)


class Database:
    def __init__(self, path: Path | str):
        self.path = str(path)
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            self._migrate(conn)

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """Additive migrations for databases created by older versions."""
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(archived_threads)")}
        for col, typ in (("trashed_at", "TEXT"), ("trash_expires_at", "TEXT"), ("last_full_fetch_at", "TEXT"),
                         ("remote_comment_count", "INTEGER"), ("remote_newest_comment_at", "TEXT")):
            if col not in cols:
                conn.execute(f"ALTER TABLE archived_threads ADD COLUMN {col} {typ}")
        ocols = {r["name"] for r in conn.execute("PRAGMA table_info(objects)")}
        for col, typ in (("thumbnail_url", "TEXT"), ("upvotes", "INTEGER"), ("downvotes", "INTEGER")):
            if col not in ocols:
                conn.execute(f"ALTER TABLE objects ADD COLUMN {col} {typ}")

    def get_setting(self, key: str, default: str | None = None) -> str | None:
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = self._open()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self._open()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
