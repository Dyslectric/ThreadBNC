"""Storage: SQLite by default, Postgres when THREADBNC_DATABASE_URL is set. Schema is append-oriented: revisions and state_events are
never updated or deleted, except when a thread is purged (an expired
auto-captured thread, or one whose time in the trash has run out)."""

from __future__ import annotations

import json
import queue
import re
import sqlite3
import time
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
    cur_deleted INTEGER NOT NULL DEFAULT 0,
    moderators_json TEXT,     -- last observed moderator actor ids (JSON list)
    view_mode TEXT,           -- feed shown as 'list' or 'tiles'; NULL = decide from how much media it has
    -- Media archiving here (see media.MediaPolicy); NULL = the server-wide default.
    media_archive TEXT,       -- 'all' | 'images' | 'off'
    media_max_mb INTEGER,     -- largest file kept
    media_transcode INTEGER   -- 1 = shrink files over the limit with ffmpeg instead of giving up
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
    cur_featured INTEGER NOT NULL DEFAULT 0,
    score INTEGER,
    reply_count INTEGER,
    thumbnail_url TEXT,  -- current preview image; archived via media
    upvotes INTEGER,
    downvotes INTEGER,
    revision_count INTEGER NOT NULL DEFAULT 0,
    last_changed_at TEXT,
    dupe_key TEXT        -- posts: same link / same text as other posts (see dupes.py)
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
    fetched_at TEXT,
    original_bytes INTEGER,   -- set when the stored file was transcoded down from a bigger one
    original_type TEXT
);
CREATE INDEX IF NOT EXISTS media_pending ON media(status, next_attempt_at);
CREATE INDEX IF NOT EXISTS media_sha ON media(sha256);

-- Which objects (in any revision) referenced which media.
CREATE TABLE IF NOT EXISTS media_refs (
    object_id INTEGER NOT NULL REFERENCES objects(id),
    media_id INTEGER NOT NULL REFERENCES media(id),
    first_seen_at TEXT NOT NULL,
    from_article INTEGER NOT NULL DEFAULT 0,  -- 1 = only in the article the post links to, not the post itself
    PRIMARY KEY (object_id, media_id)
);
CREATE INDEX IF NOT EXISTS media_refs_media ON media_refs(media_id);

-- Web pages posts link to, read by the bouncer (see articles.py). Keyed by the
-- link as posted; content_html is the extracted article, already sanitised.
CREATE TABLE IF NOT EXISTS articles (
    id INTEGER PRIMARY KEY,
    url TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'ok', 'failed', 'skipped')),
    title TEXT,
    byline TEXT,
    site_name TEXT,
    published TEXT,
    lead_image_url TEXT,      -- the page's preview picture, when the article has none of its own
    content_html TEXT,
    images_json TEXT,         -- the article's pictures (archived as media of the posts linking to it)
    word_count INTEGER,
    fetched_from TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    error TEXT,
    first_seen_at TEXT NOT NULL,
    fetched_at TEXT
);
CREATE INDEX IF NOT EXISTS articles_pending ON articles(status, next_attempt_at);

-- Which posts (in any revision) linked to which articles.
CREATE TABLE IF NOT EXISTS article_refs (
    object_id INTEGER NOT NULL REFERENCES objects(id),
    article_id INTEGER NOT NULL REFERENCES articles(id),
    first_seen_at TEXT NOT NULL,
    PRIMARY KEY (object_id, article_id)
);
CREATE INDEX IF NOT EXISTS article_refs_article ON article_refs(article_id);

-- Accounts ThreadBNC can act as. Only the server-issued session token is kept,
-- encrypted (see vault.py); passwords are never stored.
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY,
    domain TEXT NOT NULL,
    software TEXT,
    username TEXT NOT NULL,
    actor_ap_id TEXT NOT NULL UNIQUE,
    display_name TEXT,
    token_enc TEXT,
    status TEXT NOT NULL DEFAULT 'ok' CHECK (status IN ('ok', 'needs_login')),
    is_default INTEGER NOT NULL DEFAULT 0,
    is_admin INTEGER NOT NULL DEFAULT 0,
    added_at TEXT NOT NULL,
    last_used_at TEXT,
    last_error TEXT,
    inbox_checked_at TEXT,    -- last look at the account's replies, mentions and messages
    inbox_error TEXT
);

-- Replies, mentions and private messages to your accounts, as their servers
-- report them. `unread` mirrors the server's own read state.
CREATE TABLE IF NOT EXISTS inbox_items (
    id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN ('reply', 'mention', 'message')),
    remote_id TEXT NOT NULL,       -- the server's id for this notification (marking it read needs it)
    unread INTEGER NOT NULL DEFAULT 1,
    author_ap_id TEXT,
    author_name TEXT,
    author_local_id TEXT,          -- the author's id on the account's server (to write back)
    body TEXT,
    subject TEXT,
    created_at TEXT,
    deleted INTEGER NOT NULL DEFAULT 0,
    object_type TEXT,              -- comment | post | message
    object_ap_id TEXT,
    object_local_id TEXT,          -- ids on the account's server
    post_ap_id TEXT,
    post_local_id TEXT,
    post_title TEXT,
    community_ap_id TEXT,
    community_name TEXT,
    first_seen_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (account_id, kind, remote_id)
);
CREATE INDEX IF NOT EXISTS inbox_unread ON inbox_items(unread, created_at);

-- Votes cast through ThreadBNC, per account (remote APIs are read anonymously,
-- so this is how the UI knows what you voted).
CREATE TABLE IF NOT EXISTS my_votes (
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    object_ap_id TEXT NOT NULL,
    score INTEGER NOT NULL,
    voted_at TEXT NOT NULL,
    PRIMARY KEY (account_id, object_ap_id)
);

-- Moderation/admin actions taken through ThreadBNC (bans, blocks, mod changes).
-- Object-level actions (remove/lock/pin) are recorded as state_events instead.
CREATE TABLE IF NOT EXISTS mod_actions (
    id INTEGER PRIMARY KEY,
    account_id INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
    account_handle TEXT NOT NULL,
    action TEXT NOT NULL,
    community_id INTEGER REFERENCES communities(id),
    target TEXT,
    target_ap_id TEXT,
    reason TEXT,
    expires_days INTEGER,
    created_at TEXT NOT NULL
);

-- Private communities (Lemmy 1.0) whose membership ThreadBNC manages, through
-- one of your moderator accounts on the community's own server.
CREATE TABLE IF NOT EXISTS private_communities (
    community_id INTEGER PRIMARY KEY REFERENCES communities(id) ON DELETE CASCADE,
    account_id INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
    visibility TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    last_swept_at TEXT,
    last_error TEXT
);

-- The approved list: usernames (user@host, lowercase) let in automatically.
CREATE TABLE IF NOT EXISTS private_members (
    community_id INTEGER NOT NULL REFERENCES communities(id) ON DELETE CASCADE,
    handle TEXT NOT NULL,
    added_at TEXT NOT NULL,
    PRIMARY KEY (community_id, handle)
);

-- Requests to join a private community and what became of them.
-- status: waiting | approved | denied | elsewhere (answered outside ThreadBNC)
--         | revoked (banned when taken off the list) | unbanned (put back; must ask again)
CREATE TABLE IF NOT EXISTS join_requests (
    id INTEGER PRIMARY KEY,
    community_id INTEGER NOT NULL REFERENCES communities(id) ON DELETE CASCADE,
    handle TEXT NOT NULL,
    person_ap_id TEXT,
    display_name TEXT,
    person_local_id TEXT,
    community_local_id TEXT,
    status TEXT NOT NULL,
    decided_by TEXT,
    first_seen_at TEXT NOT NULL,
    decided_at TEXT,
    UNIQUE (community_id, handle)
);

-- Single sign-on logins in progress (state is the OAuth state parameter), and
-- how they ended. Rows are short-lived.
CREATE TABLE IF NOT EXISTS sso_logins (
    state TEXT PRIMARY KEY,
    domain TEXT NOT NULL,
    provider_id INTEGER NOT NULL,
    provider_name TEXT,
    verifier TEXT,
    username TEXT,
    answer TEXT,
    authorize_url TEXT NOT NULL,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'started',
    message TEXT,
    account_id INTEGER REFERENCES accounts(id) ON DELETE SET NULL
);

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


# A connection as used by application code: sqlite3.Connection or _PgConn.
Conn = Any

# Columns added after the first release. Applied idempotently on startup so
# older databases catch up (the full SCHEMA above already includes them).
COLUMN_MIGRATIONS = [
    ("archived_threads", "trashed_at", "TEXT"),
    ("archived_threads", "trash_expires_at", "TEXT"),
    ("archived_threads", "last_full_fetch_at", "TEXT"),
    ("archived_threads", "remote_comment_count", "INTEGER"),
    ("archived_threads", "remote_newest_comment_at", "TEXT"),
    ("objects", "thumbnail_url", "TEXT"),
    ("objects", "upvotes", "INTEGER"),
    ("objects", "downvotes", "INTEGER"),
    ("accounts", "is_admin", "INTEGER NOT NULL DEFAULT 0"),
    ("communities", "moderators_json", "TEXT"),
    ("objects", "cur_featured", "INTEGER NOT NULL DEFAULT 0"),
    ("objects", "dupe_key", "TEXT"),
    ("communities", "view_mode", "TEXT"),
    ("accounts", "inbox_checked_at", "TEXT"),
    ("accounts", "inbox_error", "TEXT"),
    ("communities", "media_archive", "TEXT"),
    ("communities", "media_max_mb", "INTEGER"),
    ("communities", "media_transcode", "INTEGER"),
    ("media", "original_bytes", "INTEGER"),
    ("media", "original_type", "TEXT"),
    ("media_refs", "from_article", "INTEGER NOT NULL DEFAULT 0"),
]

# Tables with an integer `id` key: inserts into these get `RETURNING id` on
# Postgres so callers can keep using `cursor.lastrowid`.
_ID_TABLES = {"instances", "actors", "communities", "archived_threads", "objects", "revisions",
              "state_events", "media", "jobs", "accounts", "mod_actions", "join_requests",
              "inbox_items", "articles"}
_INSERT_RE = re.compile(r"^\s*INSERT\s+INTO\s+(\w+)", re.IGNORECASE)
_PG_WRITE_LOCK = 727_001  # advisory lock id: one writer at a time, like SQLite's BEGIN IMMEDIATE


def postgres_schema() -> str:
    """Translate SCHEMA's SQLite DDL to Postgres."""
    ddl = re.sub(r"\bid INTEGER PRIMARY KEY\b", "id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY", SCHEMA)
    # archived_threads is created before objects, so add that FK afterwards.
    ddl = ddl.replace("root_object_id INTEGER UNIQUE REFERENCES objects(id)", "root_object_id INTEGER UNIQUE")
    return ddl + """
DO $$ BEGIN
    ALTER TABLE archived_threads ADD CONSTRAINT archived_threads_root_object_fk
        FOREIGN KEY (root_object_id) REFERENCES objects(id);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
"""


class Row:
    """Postgres row that behaves like sqlite3.Row: r["col"], r[0], dict(r)."""

    __slots__ = ("_names", "_values")

    def __init__(self, names: tuple[str, ...], values: tuple[Any, ...]):
        self._names = names
        self._values = values

    def __getitem__(self, key: int | slice | str) -> Any:
        if isinstance(key, (int, slice)):
            return self._values[key]
        try:
            return self._values[self._names.index(key)]
        except ValueError:
            raise IndexError(f"No item with that key: {key}") from None

    def keys(self) -> list[str]:
        return list(self._names)

    def __iter__(self) -> Iterator[Any]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def __repr__(self) -> str:
        return f"Row({dict(zip(self._names, self._values))!r})"


def _row_factory(cursor: Any) -> Any:
    names = tuple(c.name for c in cursor.description or ())
    return lambda values: Row(names, tuple(values))


class _PgCursor:
    def __init__(self, cur: Any, returning: bool):
        self._cur = cur
        self.lastrowid = None
        if returning:
            row = cur.fetchone()
            self.lastrowid = row[0] if row else None

    @property
    def rowcount(self) -> int:
        return self._cur.rowcount

    def fetchone(self) -> Any:
        return self._cur.fetchone() if self._cur.description else None

    def fetchall(self) -> list[Any]:
        return self._cur.fetchall() if self._cur.description else []

    def __iter__(self) -> Iterator[Any]:
        return iter(self.fetchall())


class _PgConn:
    """Minimal sqlite3.Connection look-alike over psycopg: `?` placeholders,
    lastrowid via RETURNING, rows usable by index or name."""

    def __init__(self, raw: Any):
        self.raw = raw

    def execute(self, sql: str, params: Any = ()) -> _PgCursor:
        returning = False
        m = _INSERT_RE.match(sql)
        if m and m.group(1).lower() in _ID_TABLES and "returning" not in sql.lower():
            sql = sql.rstrip().rstrip(";") + " RETURNING id"
            returning = True
        if params:
            sql = sql.replace("%", "%%").replace("?", "%s")
            cur = self.raw.execute(sql, tuple(params))
        else:
            cur = self.raw.execute(sql)
        return _PgCursor(cur, returning)

    def executescript(self, script: str) -> None:
        self.raw.execute(script)


class Database:
    """SQLite (default, zero setup) or Postgres (when given a postgres:// URL).

    Application code writes SQLite-style SQL with `?` placeholders; the Postgres
    path translates it. Both expose connect() for reads/small writes and
    transaction() for serialized write transactions."""

    def __init__(self, target: Path | str, pool_size: int = 8):
        target = str(target)
        self.is_postgres = target.startswith(("postgres://", "postgresql://"))
        self.path = target
        self._pool: queue.LifoQueue[Any] = queue.LifoQueue(maxsize=pool_size)
        self.search_backend = "like"  # set by _migrate: postgres, fts5, or like (SQLite without FTS5)
        if self.is_postgres:
            self._wait_for_postgres()
        # SQLite's executescript manages its own transaction; Postgres gets the
        # write lock so concurrent starters don't race on DDL.
        with (self.transaction() if self.is_postgres else self.connect()) as conn:
            conn.executescript(postgres_schema() if self.is_postgres else SCHEMA)
            self._migrate(conn)

    def _wait_for_postgres(self, attempts: int = 30, delay: float = 2.0) -> None:
        import psycopg

        for attempt in range(1, attempts + 1):
            try:
                psycopg.connect(self.path, connect_timeout=5).close()
                return
            except psycopg.OperationalError:
                if attempt == attempts:
                    raise
                time.sleep(delay)

    def _migrate(self, conn: Any) -> None:
        """Additive migrations for databases created by older versions."""
        for table, col, typ in COLUMN_MIGRATIONS:
            if self.is_postgres:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} {typ}")
            elif col not in {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
        conn.execute("CREATE INDEX IF NOT EXISTS objects_dupe ON objects(dupe_key)")
        from .dupes import backfill
        backfill(conn)
        from .search import install
        self.search_backend = install(conn, self.is_postgres)

    def get_setting(self, key: str, default: str | None = None) -> str | None:
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    # -- sqlite ----------------------------------------------------------------
    def _open_sqlite(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    # -- postgres (small LIFO pool; connections are cheap to reuse) ------------
    def _get_pg(self) -> Any:
        import psycopg

        while True:
            try:
                raw = self._pool.get_nowait()
            except queue.Empty:
                return psycopg.connect(self.path, autocommit=True, row_factory=_row_factory)
            if not raw.closed and not raw.broken:
                return raw

    def _put_pg(self, raw: Any) -> None:
        if raw.closed or raw.broken:
            return
        try:
            self._pool.put_nowait(raw)
        except queue.Full:
            raw.close()

    @contextmanager
    def connect(self) -> Iterator[Any]:
        if self.is_postgres:
            raw = self._get_pg()
            try:
                yield _PgConn(raw)
            finally:
                self._put_pg(raw)
            return
        conn = self._open_sqlite()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def transaction(self) -> Iterator[Any]:
        if self.is_postgres:
            raw = self._get_pg()
            try:
                with raw.transaction():
                    raw.execute("SELECT pg_advisory_xact_lock(%s)", (_PG_WRITE_LOCK,))
                    yield _PgConn(raw)
            finally:
                self._put_pg(raw)
            return
        conn = self._open_sqlite()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def close(self) -> None:
        while True:
            try:
                self._pool.get_nowait().close()
            except queue.Empty:
                return


def open_database(settings: Any) -> Database:
    return Database(settings.database_url or settings.db_path)
