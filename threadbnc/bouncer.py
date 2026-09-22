"""The bouncer: a long-running worker that observes retained threads and
followed communities and appends what it sees to the archive."""

from __future__ import annotations

import json
import logging
import threading
import traceback
from datetime import timedelta
from typing import Any, Callable

from . import media, store
from .adapters import (
    CommunityRef,
    HttpClient,
    RSS_DOMAIN,
    RSS_PREFIX,
    NCommunity,
    NPost,
    RemoteError,
    RemoteNotFound,
    RemoteUnavailable,
    ThreadiverseAdapter,
    UnsupportedSoftware,
    adapter_class,
    detect_software,
    host_of,
    is_reddit_host,
    is_rss,
    parse_community_ref,
    parse_thread_url,
)
from .adapters.reddit import RedditAdapter
from .adapters.rss import FeedFetcher, RssAdapter
from .config import REDDIT_MIN_POLL_MINUTES, RSS_MIN_POLL_MINUTES, Settings
from .db import Database, fmt_ts, parse_ts, utcnow
from .reddit import RedditConnection
from .vault import TokenVault

log = logging.getLogger("threadbnc.bouncer")

SOFTWARE_RECHECK = timedelta(days=1)
MAX_BACKOFF = timedelta(hours=24)
AUTO_CAPTURE_PAGE = 30
MAX_POLL_PAGES = 5  # read further back until we reach known posts, up to this many pages
# Re-check threads often while they're new, then back off: most comments arrive
# in a post's first day or two. (post age limit, minimum minutes between checks)
RECHECK_TAPER = [(timedelta(days=1), 0), (timedelta(days=3), 60), (None, 360)]
# Reddit threads back off further (its request budget is shared by everything),
# and so do feed articles (they rarely change): every 2 hours until day 3,
# twice a day until day 7, then daily.
REDDIT_RECHECK_TAPER = [(timedelta(days=1), 0), (timedelta(days=3), 120), (timedelta(days=7), 720), (None, 1440)]
# When a post's comment count and newest-comment time are unchanged, skip
# re-fetching the comment tree -- but still do a full fetch at least this often,
# since edits and some deletions don't change those counters.
FULL_FETCH_EVERY = timedelta(hours=6)


def _plus(ts: str, **delta: float) -> str:
    return fmt_ts(parse_ts(ts) + timedelta(**delta))  # type: ignore[operator]


class Bouncer:
    def __init__(self, db: Database, settings: Settings, http: HttpClient | None = None,
                 adapter_factory: Callable[[str], ThreadiverseAdapter] | None = None,
                 reddit: RedditConnection | None = None):
        self.db = db
        self.settings = settings
        self.http = http or HttpClient(settings.user_agent, settings.http_timeout,
                                       settings.min_request_interval)
        self.reddit = reddit or RedditConnection(
            db, TokenVault(settings.credentials_key, settings.data_dir), timeout=settings.http_timeout,
            min_interval=settings.reddit_min_request_interval)
        self.reddit_adapter = RedditAdapter(self.reddit)
        self.rss_adapter = RssAdapter(FeedFetcher(settings.user_agent, settings.http_timeout, self.http.throttle))
        self._adapter_factory = adapter_factory
        self._adapters: dict[str, tuple[ThreadiverseAdapter, str]] = {}  # domain -> (adapter, chosen at)
        # Extra work for each pass that needs accounts (e.g. private community join requests).
        self.hooks: list[Callable[[], Any]] = []
        # (domain, community id) -> a member's session token, for communities only
        # members can read (see private.py). None: read anonymously, as usual.
        self.read_token: Callable[[str, int], str | None] | None = None
        self.media_dir = settings.media_dir or (settings.data_dir / "media")
        self.media = media.MediaFetcher(db, self.media_dir, settings.user_agent, settings.media_max_bytes,
                                        timeout=max(settings.http_timeout, 30.0), throttle=self.http.throttle)
        self._media_backfilled = False
        self.wake = threading.Event()
        self._stop = threading.Event()

    # -- adapters ----------------------------------------------------------
    def adapter_for(self, domain: str) -> ThreadiverseAdapter:
        if is_reddit_host(domain):
            return self.reddit_adapter
        if domain == RSS_DOMAIN:
            return self.rss_adapter
        if self._adapter_factory:
            return self._adapter_factory(domain)
        now = utcnow()
        cached = self._adapters.get(domain)
        # Ask the server what it runs on first use after starting, then daily: a
        # server upgraded to Lemmy 1.0 needs the v4 adapter, and restarting
        # ThreadBNC should be enough to notice. What we saw last time is only a
        # fallback for when the server can't be reached.
        if cached and parse_ts(now) - parse_ts(cached[1]) <= SOFTWARE_RECHECK:  # type: ignore[operator]
            return cached[0]
        try:
            software, version = detect_software(self.http, domain)
        except RemoteError:
            with self.db.connect() as conn:
                row = conn.execute("SELECT software, software_version FROM instances WHERE domain=?",
                                   (domain,)).fetchone()
            if not row or not row["software"]:
                raise
            software, version = row["software"], row["software_version"]
        else:
            with self.db.transaction() as conn:
                store.upsert_instance(conn, domain, now, software=software, version=version)
        cls = adapter_class(software, version)
        if cls is None:
            raise UnsupportedSoftware(f"{domain} runs '{software}', which is not supported yet")
        if cached and type(cached[0]) is cls:
            adapter = cached[0]
        else:
            adapter = cls(domain, self.http)
            if cached:
                log.info("%s now runs %s %s; switching to %s", domain, software, version, cls.__name__)
        self._adapters[domain] = (adapter, now)
        return adapter

    def reader(self, domain: str, community_id: int | None) -> ThreadiverseAdapter:
        """The adapter to read a community's posts with: logged in as a member
        for private communities we manage, anonymous otherwise."""
        adapter = self.adapter_for(domain)
        token = self.read_token(domain, community_id) if self.read_token and community_id else None
        return adapter.reading_as(token) if token and hasattr(adapter, "reading_as") else adapter

    def _community_id(self, community_ap_id: str) -> int | None:
        with self.db.connect() as conn:
            row = conn.execute("SELECT id FROM communities WHERE canonical_ap_id=?", (community_ap_id,)).fetchone()
        return row["id"] if row else None

    def _note_contact(self, domain: str, ok: bool, error: str | None = None) -> None:
        with self.db.transaction() as conn:
            store.record_instance_contact(conn, domain, utcnow(), ok, error)

    # -- source selection --------------------------------------------------
    def _best_source(self, post: NPost, seen_domain: str, seen_local_id: str,
                     seen_adapter: ThreadiverseAdapter) -> tuple[str, str, NPost]:
        """Prefer the community's home instance (it relays every comment and is
        authoritative for moderation), then the post author's instance, then the
        server the user linked."""
        for dom in (post.community.domain, host_of(post.ap_id)):
            if not dom:
                continue
            if dom == seen_domain:
                break  # already on the best available source
            try:
                adapter = self.reader(dom, self._community_id(post.community.ap_id))
                local = adapter.resolve_ap_id(post.ap_id)
                if not local:
                    continue
                candidate = adapter.fetch_post(local)
                if candidate.ap_id == post.ap_id:
                    return dom, local, candidate
            except RemoteError as exc:
                log.info("source candidate %s rejected: %s", dom, exc)
        return seen_domain, seen_local_id, post

    # -- ingestion ---------------------------------------------------------
    def ingest_url(self, url: str, retention: str = "manual") -> int:
        ref = parse_thread_url(url)
        adapter = self.adapter_for(ref.domain)
        try:
            local = adapter.resolve_url(ref)
            post = adapter.fetch_post(local)
        except RemoteUnavailable as exc:
            self._note_contact(ref.domain, False, str(exc))
            raise
        self._note_contact(ref.domain, True)
        return self._ingest_post(post, ref.domain, local, adapter, source_url=url, retention=retention)

    def _existing_thread(self, ap_id: str) -> Any:
        with self.db.connect() as conn:
            return conn.execute(
                "SELECT t.* FROM archived_threads t JOIN objects o ON o.id=t.root_object_id "
                "WHERE o.canonical_ap_id=?", (ap_id,)
            ).fetchone()

    def _ingest_post(self, post: NPost, domain: str, local_id: str, adapter: ThreadiverseAdapter, *,
                     source_url: str, retention: str) -> int:
        existing = self._existing_thread(post.ap_id)
        if existing:
            if retention == "manual" and existing["trashed_at"]:
                self.restore_from_trash(existing["id"], keep=True)
            elif retention == "manual" and existing["retention"] == "auto":
                self.promote(existing["id"])
            return existing["id"]

        src_domain, src_local, post = self._best_source(post, domain, local_id, adapter)
        src_adapter = self.reader(src_domain, self._community_id(post.community.ap_id))
        comments = src_adapter.fetch_comments(src_local)
        now = utcnow()
        result = store.ApplyResult()
        with self.db.transaction() as conn:
            existing = conn.execute(  # re-check inside the write lock
                "SELECT t.id FROM archived_threads t JOIN objects o ON o.id=t.root_object_id "
                "WHERE o.canonical_ap_id=?", (post.ap_id,)
            ).fetchone()
            if existing:
                return existing["id"]
            cur = conn.execute(
                "INSERT INTO archived_threads(source_url, source_domain, source_local_id, retention, "
                "retained_at, last_checked_at, last_success_at) VALUES (?,?,?,?,?,?,?)",
                (source_url, src_domain, src_local, retention, now, now, now),
            )
            tid = cur.lastrowid
            root_id = store.apply_post(conn, tid, post, src_domain, now, True, result)
            cid = conn.execute("SELECT community_id FROM objects WHERE id=?", (root_id,)).fetchone()[0]
            store.apply_comments(conn, tid, root_id, cid, comments, src_domain, now, True,
                                 getattr(comments, "complete", True), result)
            expires = self._expiry_for(conn, cid, now) if retention == "auto" else None
            conn.execute(
                "UPDATE archived_threads SET last_full_fetch_at=?, remote_comment_count=?, "
                "remote_newest_comment_at=? WHERE id=?",
                (now, post.comment_count, post.newest_comment_at, tid),
            )
            conn.execute(
                "UPDATE archived_threads SET root_object_id=?, community_id=?, expires_at=?, next_check_at=? "
                "WHERE id=?",
                (root_id, cid, expires, _plus(now, minutes=self._thread_interval(conn, cid, post.created_at)),
                 tid),
            )
            store.add_event(conn, "retained" if retention == "manual" else "auto_captured", now,
                            thread_id=tid, metadata={"source_url": source_url, "source_domain": src_domain})
        self._enrich_moderation(src_adapter, result, post.community)
        log.info("ingested %s (%d objects) as thread %d", post.ap_id, len(result.object_ids), tid)
        return tid

    def promote(self, thread_id: int) -> None:
        now = utcnow()
        with self.db.transaction() as conn:
            t = conn.execute("SELECT retention FROM archived_threads WHERE id=?", (thread_id,)).fetchone()
            if t and t["retention"] == "auto":
                conn.execute(
                    "UPDATE archived_threads SET retention='manual', promoted_at=?, expires_at=NULL WHERE id=?",
                    (now, thread_id),
                )
                store.add_event(conn, "promoted", now, thread_id=thread_id)

    # -- trash ---------------------------------------------------------------
    def trash_days(self) -> int | None:
        raw = self.db.get_setting("trash_days")
        if raw is None:
            return self.settings.default_trash_days
        return None if raw == "forever" else int(raw)

    def set_trash_days(self, days: int | None) -> None:
        with self.db.transaction() as conn:
            conn.execute("INSERT INTO app_settings(key, value) VALUES ('trash_days', ?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                         ("forever" if days is None else str(days),))
            for t in conn.execute("SELECT id, trashed_at FROM archived_threads WHERE trashed_at IS NOT NULL"
                                  ).fetchall():
                exp = None if days is None else _plus(t["trashed_at"], days=days)
                conn.execute("UPDATE archived_threads SET trash_expires_at=? WHERE id=?", (exp, t["id"]))

    def move_to_trash(self, thread_id: int) -> None:
        """Un-keep: stop monitoring and schedule permanent deletion. Reversible
        until the trash period ends."""
        now = utcnow()
        days = self.trash_days()
        with self.db.transaction() as conn:
            t = conn.execute("SELECT * FROM archived_threads WHERE id=?", (thread_id,)).fetchone()
            if t is None or t["trashed_at"]:
                return
            conn.execute(
                "UPDATE archived_threads SET trashed_at=?, trash_expires_at=?, active=0 WHERE id=?",
                (now, None if days is None else _plus(now, days=days), thread_id),
            )
            store.add_event(conn, "trashed", now, thread_id=thread_id,
                            metadata={"retention": t["retention"], "trash_days": days})

    def unkeep(self, thread_id: int) -> str:
        """Undo "keep". A thread that was auto-captured from a community you still
        follow goes back to being auto-captured (if its window hasn't lapsed);
        anything else goes to the trash. Returns 'auto' or 'trash'."""
        now = utcnow()
        with self.db.transaction() as conn:
            t = conn.execute("SELECT * FROM archived_threads WHERE id=?", (thread_id,)).fetchone()
            if t is None or t["retention"] != "manual" or t["trashed_at"]:
                return "noop"
            followed = conn.execute("SELECT 1 FROM community_follows WHERE community_id=? AND active=1",
                                    (t["community_id"],)).fetchone()
            expires = self._expiry_for(conn, t["community_id"], t["retained_at"])
            if t["promoted_at"] and followed and (expires is None or expires > now):
                conn.execute("UPDATE archived_threads SET retention='auto', promoted_at=NULL, expires_at=? "
                             "WHERE id=?", (expires, thread_id))
                store.add_event(conn, "unkept", now, thread_id=thread_id, metadata={"now": "auto"})
                return "auto"
        self.move_to_trash(thread_id)
        return "trash"

    def restore_from_trash(self, thread_id: int, keep: bool = False) -> str | None:
        """Put a thread back where it came from. An auto-captured thread whose
        retention window ran out while in the trash (or keep=True) comes back as kept.
        Returns the retention it was restored to."""
        now = utcnow()
        with self.db.transaction() as conn:
            t = conn.execute("SELECT * FROM archived_threads WHERE id=?", (thread_id,)).fetchone()
            if t is None or not t["trashed_at"]:
                return None
            retention = t["retention"]
            if retention == "auto":
                expires = self._expiry_for(conn, t["community_id"], t["retained_at"])
                if keep or (expires is not None and expires <= now):
                    retention = "manual"
            conn.execute(
                "UPDATE archived_threads SET trashed_at=NULL, trash_expires_at=NULL, active=1, next_check_at=?, "
                "retention=?, expires_at=CASE WHEN ?='manual' THEN NULL ELSE expires_at END, "
                "promoted_at=CASE WHEN ?='manual' AND retention='auto' THEN ? ELSE promoted_at END WHERE id=?",
                (now, retention, retention, retention, now, thread_id),
            )
            store.add_event(conn, "restored_from_trash", now, thread_id=thread_id,
                            metadata={"retention": retention})
        self.wake.set()
        return retention

    def delete_from_trash(self, thread_ids: list[int] | None = None) -> int:
        """Permanently delete trashed threads now (all of them if ids is None)."""
        now = utcnow()
        files: list = []
        with self.db.transaction() as conn:
            rows = conn.execute("SELECT id FROM archived_threads WHERE trashed_at IS NOT NULL").fetchall()
            ids = [r["id"] for r in rows if thread_ids is None or r["id"] in thread_ids]
            for tid in ids:
                files += store.purge_thread(conn, tid, now, self.media_dir, reason="deleted_from_trash")
        for f in files:
            f.unlink(missing_ok=True)
        return len(ids)

    # -- scheduling helpers -----------------------------------------------
    def _interval(self, conn: Any, community_id: int | None) -> int:
        if community_id:
            f = conn.execute(
                "SELECT poll_interval_minutes FROM community_follows WHERE community_id=? AND active=1",
                (community_id,),
            ).fetchone()
            if f:
                return f["poll_interval_minutes"]
        return self.settings.default_sync_minutes

    def _slow_source(self, conn: Any, community_id: int | None) -> int | None:
        """The slowest re-check interval for threads from Reddit or a feed (None
        for Lemmy/PieFed)."""
        row = conn.execute("SELECT canonical_ap_id FROM communities WHERE id=?", (community_id,)).fetchone()
        if not row:
            return None
        if is_reddit_host(host_of(row["canonical_ap_id"])):
            return self.settings.reddit_poll_minutes
        return self.settings.rss_poll_minutes if is_rss(row["canonical_ap_id"]) else None

    def _thread_interval(self, conn: Any, community_id: int | None, created_at: str | None) -> int:
        """Minutes until a thread's next re-check: the community's interval while
        the post is young, tapering to hourly and then every 6 hours (further
        on Reddit and for feeds, see REDDIT_RECHECK_TAPER)."""
        base, taper = self._interval(conn, community_id), RECHECK_TAPER
        slow = self._slow_source(conn, community_id)
        if slow:
            base, taper = max(base, slow), REDDIT_RECHECK_TAPER
        created = parse_ts(created_at)
        age = parse_ts(utcnow()) - created if created else timedelta(days=365)  # type: ignore[operator]
        for limit, floor in taper:
            if limit is None or age < limit:
                return max(base, floor)
        return base

    def _root_created(self, conn: Any, root_object_id: int | None) -> str | None:
        row = conn.execute("SELECT created_at FROM objects WHERE id=?", (root_object_id,)).fetchone()
        return row["created_at"] if row else None

    @staticmethod
    def _expiry_for(conn: Any, community_id: int, captured_at: str) -> str | None:
        f = conn.execute("SELECT retention_days FROM community_follows WHERE community_id=?",
                         (community_id,)).fetchone()
        if f is None or f["retention_days"] is None:
            return None
        return _plus(captured_at, days=f["retention_days"])

    # -- synchronization ---------------------------------------------------
    @staticmethod
    def _comments_unchanged(t: Any, post: NPost, now: str) -> bool:
        """True when the server's counters say nothing new happened and the last
        full fetch is recent enough to skip re-reading the comment tree."""
        last_full = parse_ts(t["last_full_fetch_at"])
        if last_full is None or parse_ts(now) - last_full >= FULL_FETCH_EVERY:  # type: ignore[operator]
            return False
        if post.comment_count is None:
            return False  # server doesn't report counters; always fetch
        # (Reddit reports a count but no newest-comment time; then the count alone decides.)
        return (post.comment_count == t["remote_comment_count"]
                and post.newest_comment_at == t["remote_newest_comment_at"])

    def sync_thread(self, thread_id: int, force: bool = False) -> store.ApplyResult:
        """Re-observe a thread. The post is always fetched; the comment tree is
        skipped when the post's counters are unchanged, unless `force` or the
        last full fetch is older than FULL_FETCH_EVERY."""
        with self.db.connect() as conn:
            t = conn.execute("SELECT * FROM archived_threads WHERE id=?", (thread_id,)).fetchone()
        if t is None:
            raise KeyError(thread_id)
        domain, local = t["source_domain"], t["source_local_id"]
        now = utcnow()
        result = store.ApplyResult()
        full = True
        try:
            adapter = self.reader(domain, t["community_id"])
            try:
                post = adapter.fetch_post(local)
            except RemoteNotFound:
                post = None
            if post is None and getattr(adapter, "ages_out", False):
                self._aged_out(thread_id)
                return result
            full = post is not None and (force or not self._comments_unchanged(t, post, now))
            comments = adapter.fetch_comments(local) if full else []
        except (RemoteUnavailable, UnsupportedSoftware) as exc:
            self._sync_failed(t, str(exc))
            raise
        with self.db.transaction() as conn:
            store.record_instance_contact(conn, domain, now, True)
            if post is None:
                store.mark_missing(conn, t["root_object_id"], thread_id, now, result)
            else:
                root_id = store.apply_post(conn, thread_id, post, domain, now, False, result)
                if full:
                    store.apply_comments(conn, thread_id, root_id, t["community_id"], comments, domain, now,
                                         False, getattr(comments, "complete", True), result)
                    conn.execute(
                        "UPDATE archived_threads SET last_full_fetch_at=?, remote_comment_count=?, "
                        "remote_newest_comment_at=? WHERE id=?",
                        (now, post.comment_count, post.newest_comment_at, thread_id),
                    )
            conn.execute(
                "UPDATE archived_threads SET last_checked_at=?, last_success_at=?, consecutive_failures=0, "
                "last_error=NULL, next_check_at=? WHERE id=?",
                (now, now, _plus(now, minutes=self._thread_interval(
                    conn, t["community_id"], self._root_created(conn, t["root_object_id"]))), thread_id),
            )
        if post is not None:
            self._enrich_moderation(adapter, result, post.community)
            if full:
                self._maybe_move_source(thread_id, post, domain, local)
        return result

    def _aged_out(self, thread_id: int) -> None:
        """A feed article that's no longer in its feed: feeds only list their
        latest entries, so that's not deletion. Keep what we have and stop
        checking it."""
        now = utcnow()
        with self.db.transaction() as conn:
            conn.execute("UPDATE archived_threads SET active=0, last_checked_at=?, next_check_at=NULL WHERE id=?",
                         (now, thread_id))
            store.add_event(conn, "aged_out", now, thread_id=thread_id)

    def _maybe_move_source(self, thread_id: int, post: NPost, domain: str, local: str) -> None:
        """A thread first seen on a secondary server (e.g. a post you just made
        from your own instance) moves to the community's home server once the
        post has federated there, since that's where every comment and
        moderation action arrives."""
        home = post.community.domain
        if not home or home == domain:
            return
        try:
            new_domain, new_local, _ = self._best_source(post, domain, local, self.adapter_for(domain))
        except RemoteError:
            return
        if new_domain != domain:
            with self.db.transaction() as conn:
                conn.execute("UPDATE archived_threads SET source_domain=?, source_local_id=?, "
                             "last_full_fetch_at=NULL WHERE id=?", (new_domain, new_local, thread_id))
            log.info("thread %s now polled from %s (was %s)", thread_id, new_domain, domain)

    def _sync_failed(self, t: Any, error: str) -> None:
        now = utcnow()
        with self.db.transaction() as conn:
            store.record_instance_contact(conn, t["source_domain"], now, False, error)
            failures = t["consecutive_failures"] + 1
            base = timedelta(minutes=self._thread_interval(
                conn, t["community_id"], self._root_created(conn, t["root_object_id"])))
            delay = min(base * (2 ** min(failures, 10)), MAX_BACKOFF)
            conn.execute(
                "UPDATE archived_threads SET last_checked_at=?, consecutive_failures=?, last_error=?, "
                "next_check_at=? WHERE id=?",
                (now, failures, error, fmt_ts(parse_ts(now) + delay), t["id"]),  # type: ignore[operator]
            )

    def _enrich_moderation(self, adapter: ThreadiverseAdapter, result: store.ApplyResult,
                           community: NCommunity) -> None:
        """Fill in reason/moderator for removal & lock events from the modlog.
        Done after commit so no network I/O happens under the write lock."""
        for p in result.mod_lookups:
            try:
                if p.object_type == "post":
                    actions = adapter.fetch_moderation_state(post_local_id=p.local_id, community=community)
                    kind = "lock_post" if p.event_type in ("locked", "unlocked") else "remove_post"
                else:
                    actions = adapter.fetch_moderation_state(comment_local_id=p.local_id, community=community)
                    kind = "remove_comment"
                want = p.event_type in ("removed", "locked")
                match = next((a for a in reversed(actions) if a.kind == kind and a.active == want), None)
            except RemoteError as exc:
                self._update_event(p.event_id, {"modlog": "unavailable", "modlog_error": str(exc)})
                continue
            if match is None:
                self._update_event(p.event_id, {"modlog": "no_matching_entry"})
                continue
            now = utcnow()
            with self.db.transaction() as conn:
                actor_id = store.upsert_actor(conn, match.moderator, now) if match.moderator else None
                self._update_event(p.event_id, {"modlog": "matched"}, conn=conn, attribution=match.attribution,
                                   actor_id=actor_id, reason=match.reason, remote_timestamp=match.when)

    def _update_event(self, event_id: int, meta: dict[str, Any], conn: Any = None, **cols: Any) -> None:
        def run(c: Any) -> None:
            row = c.execute("SELECT metadata_json FROM state_events WHERE id=?", (event_id,)).fetchone()
            merged = {**json.loads(row["metadata_json"] or "{}"), **meta}
            sets = ", ".join(f"{k}=?" for k in cols)
            c.execute(
                f"UPDATE state_events SET metadata_json=?{', ' + sets if sets else ''} WHERE id=?",
                (json.dumps(merged, sort_keys=True), *cols.values(), event_id),
            )
        if conn is not None:
            run(conn)
        else:
            with self.db.transaction() as c:
                run(c)

    # -- communities -------------------------------------------------------
    def resolve_community(self, text: str) -> tuple[CommunityRef, NCommunity]:
        ref = parse_community_ref(text)
        adapter = self.adapter_for(ref.domain)
        community = adapter.fetch_community(ref)
        if ref.domain == RSS_DOMAIN:  # the feed's own URL (a page may have led to it)
            return CommunityRef(RSS_DOMAIN, community.local_id or ref.name, RSS_DOMAIN), community
        home = community.domain
        if home and home != ref.domain:
            try:  # poll the community's home instance when it speaks a supported API
                home_ref = CommunityRef(home, community.name, home)
                community = self.adapter_for(home).fetch_community(home_ref)
                return home_ref, community
            except RemoteError as exc:
                log.info("home instance %s unusable, polling via %s: %s", home, ref.domain, exc)
        return CommunityRef(ref.domain, community.name, home), community

    def follow_community(self, text: str, poll_interval_minutes: int | None = None,
                         retention_days: int | None = -1, backfill: bool = False) -> int:
        ref, community = self.resolve_community(text)
        now = utcnow()
        interval = self.poll_interval(ref.domain, poll_interval_minutes)
        days = self.settings.default_follow_retention_days if retention_days == -1 else retention_days
        with self.db.transaction() as conn:
            cid = store.upsert_community(conn, community, now)
            conn.execute(
                "INSERT INTO community_follows(community_id, active, followed_at, capture_since, "
                "poll_interval_minutes, retention_days, source_domain, source_ref, next_poll_at) "
                "VALUES (?,1,?,?,?,?,?,?,?) ON CONFLICT(community_id) DO UPDATE SET active=1, "
                "unfollowed_at=NULL, poll_interval_minutes=excluded.poll_interval_minutes, "
                "retention_days=excluded.retention_days, source_domain=excluded.source_domain, "
                "source_ref=excluded.source_ref, capture_since=excluded.capture_since, next_poll_at=?",
                (cid, now, "1970-01-01T00:00:00.000000Z" if backfill else now, interval, days,
                 ref.domain, ref.qualified, now, now),
            )
            self._reapply_expiry(conn, cid)
            store.add_event(conn, "followed", now, community_id=cid,
                            metadata={"poll_interval_minutes": interval, "retention_days": days,
                                      "source_domain": ref.domain})
        self.wake.set()
        return cid

    def poll_interval(self, domain: str, asked: int | None) -> int:
        """How often to check a community: what was asked for, else the default
        for its kind of server. Subreddits are never checked more often than
        every REDDIT_MIN_POLL_MINUTES."""
        if is_reddit_host(domain):
            return max(asked or self.settings.reddit_poll_minutes, REDDIT_MIN_POLL_MINUTES)
        if domain == RSS_DOMAIN:
            return max(asked or self.settings.rss_poll_minutes, RSS_MIN_POLL_MINUTES)
        return asked or self.settings.default_follow_poll_minutes

    def update_follow(self, community_id: int, poll_interval_minutes: int, retention_days: int | None) -> None:
        now = utcnow()
        with self.db.transaction() as conn:
            f = conn.execute("SELECT source_domain FROM community_follows WHERE community_id=?",
                             (community_id,)).fetchone()
            if f:
                poll_interval_minutes = self.poll_interval(f["source_domain"], poll_interval_minutes)
            conn.execute(
                "UPDATE community_follows SET poll_interval_minutes=?, retention_days=?, next_poll_at=? "
                "WHERE community_id=?", (poll_interval_minutes, retention_days, now, community_id),
            )
            self._reapply_expiry(conn, community_id)
            store.add_event(conn, "follow_settings_changed", now, community_id=community_id,
                            metadata={"poll_interval_minutes": poll_interval_minutes,
                                      "retention_days": retention_days})

    def unfollow(self, community_id: int) -> None:
        now = utcnow()
        with self.db.transaction() as conn:
            conn.execute("UPDATE community_follows SET active=0, unfollowed_at=? WHERE community_id=?",
                         (now, community_id))
            store.add_event(conn, "unfollowed", now, community_id=community_id)

    @staticmethod
    def _reapply_expiry(conn: Any, community_id: int) -> None:
        f = conn.execute("SELECT retention_days FROM community_follows WHERE community_id=?",
                         (community_id,)).fetchone()
        for t in conn.execute("SELECT id, retained_at FROM archived_threads WHERE community_id=? "
                              "AND retention='auto'", (community_id,)).fetchall():
            exp = None if f["retention_days"] is None else _plus(t["retained_at"], days=f["retention_days"])
            conn.execute("UPDATE archived_threads SET expires_at=? WHERE id=?", (exp, t["id"]))

    def community_ref(self, community_id: int) -> CommunityRef:
        with self.db.connect() as conn:
            f = conn.execute("SELECT source_domain, source_ref FROM community_follows WHERE community_id=?",
                             (community_id,)).fetchone()
            c = conn.execute("SELECT canonical_ap_id, name FROM communities WHERE id=?",
                             (community_id,)).fetchone()
        if f and f["source_domain"] == RSS_DOMAIN:  # the feed URL, which may contain "@"
            return CommunityRef(RSS_DOMAIN, f["source_ref"], RSS_DOMAIN)
        if f:
            name, _, home = f["source_ref"].partition("@")
            return CommunityRef(f["source_domain"], name, home or f["source_domain"])
        if is_rss(c["canonical_ap_id"]):
            return CommunityRef(RSS_DOMAIN, c["canonical_ap_id"][len(RSS_PREFIX):], RSS_DOMAIN)
        home = host_of(c["canonical_ap_id"])
        return CommunityRef(home, c["name"], home)

    def _already_seen(self, post: NPost, since: Any) -> bool:
        created = parse_ts(post.created_at)
        return bool(self._existing_thread(post.ap_id)) or bool(since and created and created < since)

    def poll_follow(self, community_id: int) -> int:
        with self.db.connect() as conn:
            f = conn.execute("SELECT * FROM community_follows WHERE community_id=?", (community_id,)).fetchone()
        ref = self.community_ref(community_id)
        now = utcnow()
        since = parse_ts(f["capture_since"])
        # First poll: one page (backfill). Later polls: keep paging until we hit a
        # post we already know or one older than capture_since, so busy
        # communities don't drop posts between polls.
        posts = []
        try:
            adapter = self.reader(ref.domain, community_id)
            # Adapters may ask for smaller pages and fewer of them (Reddit does).
            size = getattr(adapter, "poll_page_size", AUTO_CAPTURE_PAGE)
            max_pages = getattr(adapter, "max_poll_pages", MAX_POLL_PAGES) if f["last_polled_at"] else 1
            for page in range(1, max_pages + 1):
                batch = adapter.list_community_posts(ref, sort="New", page=page, limit=size)
                posts.extend(batch)
                # Pinned posts sit at the top of "New" whatever their age, so they
                # say nothing about how far back this page reaches.
                reached_known = any(self._already_seen(p, since) for p in batch if not p.featured)
                if reached_known or len(batch) < size:
                    break
        except RemoteError as exc:
            with self.db.transaction() as conn:
                store.record_instance_contact(conn, ref.domain, now, False, str(exc))
                fails = f["consecutive_failures"] + 1
                delay = min(timedelta(minutes=f["poll_interval_minutes"]) * (2 ** min(fails, 10)), MAX_BACKOFF)
                conn.execute(
                    "UPDATE community_follows SET last_polled_at=?, last_error=?, consecutive_failures=?, "
                    "next_poll_at=? WHERE community_id=?",
                    (now, str(exc), fails, fmt_ts(parse_ts(now) + delay), community_id),  # type: ignore[operator]
                )
            raise
        captured = 0
        for post in reversed(posts):  # oldest first, so feed order matches capture order
            created = parse_ts(post.created_at)
            if created and since and created < since:
                continue
            if self._existing_thread(post.ap_id):
                continue
            try:
                self._ingest_post(post, ref.domain, post.local_id, adapter, source_url=post.ap_id,
                                  retention="auto")
                captured += 1
            except RemoteError as exc:
                log.warning("auto-capture of %s failed: %s", post.ap_id, exc)
        with self.db.transaction() as conn:
            store.record_instance_contact(conn, ref.domain, now, True)
            conn.execute(
                "UPDATE community_follows SET last_polled_at=?, last_error=NULL, consecutive_failures=0, "
                "next_poll_at=? WHERE community_id=?",
                (now, _plus(now, minutes=f["poll_interval_minutes"]), community_id),
            )
        return captured

    def purge_expired(self) -> int:
        now = utcnow()
        with self.db.transaction() as conn:
            rows = conn.execute(
                "SELECT id, CASE WHEN trashed_at IS NOT NULL THEN 'trash_expired' ELSE 'auto_capture_expired' END "
                "AS reason FROM archived_threads WHERE "
                "(trashed_at IS NOT NULL AND trash_expires_at IS NOT NULL AND trash_expires_at < ?) OR "
                "(trashed_at IS NULL AND retention='auto' AND expires_at IS NOT NULL AND expires_at < ?)",
                (now, now),
            ).fetchall()
            orphan_files: list = []
            for r in rows:
                orphan_files += store.purge_thread(conn, r["id"], now, self.media_dir, reason=r["reason"])
        for f in orphan_files:  # only after the purge committed
            f.unlink(missing_ok=True)
        return len(rows)

    # -- jobs --------------------------------------------------------------
    def enqueue(self, kind: str, payload: dict[str, Any]) -> int:
        now = utcnow()
        with self.db.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO jobs(kind, payload_json, created_at, updated_at, run_after) VALUES (?,?,?,?,?)",
                (kind, json.dumps(payload), now, now, now),
            )
        self.wake.set()
        return cur.lastrowid

    def _claim_job(self) -> Any:
        now = utcnow()
        with self.db.transaction() as conn:
            job = conn.execute(
                "SELECT * FROM jobs WHERE status='queued' AND run_after<=? ORDER BY id LIMIT 1", (now,)
            ).fetchone()
            if job:
                conn.execute("UPDATE jobs SET status='running', attempts=attempts+1, updated_at=? WHERE id=?",
                             (now, job["id"]))
            return job

    def _finish_job(self, job_id: int, status: str, result: Any = None, error: str | None = None,
                    retry_in: timedelta | None = None) -> None:
        now = utcnow()
        with self.db.transaction() as conn:
            if retry_in is not None:
                conn.execute("UPDATE jobs SET status='queued', error=?, updated_at=?, run_after=? WHERE id=?",
                             (error, now, fmt_ts(parse_ts(now) + retry_in), job_id))  # type: ignore[operator]
            else:
                conn.execute("UPDATE jobs SET status=?, result_json=?, error=?, updated_at=? WHERE id=?",
                             (status, json.dumps(result), error, now, job_id))

    def run_one_job(self) -> bool:
        job = self._claim_job()
        if not job:
            return False
        payload = json.loads(job["payload_json"])
        try:
            if job["kind"] == "ingest":
                tid = self.ingest_url(payload["url"], payload.get("retention", "manual"))
                self._finish_job(job["id"], "done", {"thread_id": tid})
            elif job["kind"] == "sync":
                self.sync_thread(payload["thread_id"], force=True)
                self._finish_job(job["id"], "done", {"thread_id": payload["thread_id"]})
            else:
                self._finish_job(job["id"], "failed", error=f"unknown job kind {job['kind']}")
        except RemoteUnavailable as exc:
            if job["attempts"] < 6:
                self._finish_job(job["id"], "queued", error=str(exc),
                                 retry_in=timedelta(minutes=2 ** job["attempts"]))
            else:
                self._finish_job(job["id"], "failed", error=str(exc))
        except (RemoteError, ValueError, KeyError) as exc:
            self._finish_job(job["id"], "failed", error=f"{type(exc).__name__}: {exc}")
        except Exception as exc:  # keep the worker alive
            log.error("job %s crashed: %s", job["id"], traceback.format_exc())
            self._finish_job(job["id"], "failed", error=f"{type(exc).__name__}: {exc}")
        return True

    # -- main loop -----------------------------------------------------------
    def tick(self) -> None:
        while self.run_one_job():
            pass
        for hook in self.hooks:
            try:
                hook()
            except Exception:  # keep the worker alive
                log.error("bouncer hook %s crashed: %s", getattr(hook, "__qualname__", hook), traceback.format_exc())
        now = utcnow()
        with self.db.connect() as conn:
            follows = [r["community_id"] for r in conn.execute(
                "SELECT community_id FROM community_follows WHERE active=1 AND "
                "(next_poll_at IS NULL OR next_poll_at<=?)", (now,))]
            threads = [r["id"] for r in conn.execute(
                "SELECT id FROM archived_threads WHERE active=1 AND root_object_id IS NOT NULL AND "
                "(next_check_at IS NULL OR next_check_at<=?) ORDER BY next_check_at LIMIT 25", (now,))]
        for cid in follows:
            if self._stop.is_set():
                return
            try:
                n = self.poll_follow(cid)
                if n:
                    log.info("community %s: auto-captured %d posts", cid, n)
            except RemoteError as exc:
                log.warning("poll of community %s failed: %s", cid, exc)
        for tid in threads:
            if self._stop.is_set():
                return
            while self.run_one_job():  # user requests jump the queue
                pass
            try:
                self.sync_thread(tid)
            except RemoteError as exc:
                log.warning("sync of thread %s failed: %s", tid, exc)
            except Exception:
                log.error("sync of thread %s crashed: %s", tid, traceback.format_exc())
        if not self._media_backfilled:
            with self.db.transaction() as conn:
                media.register_all_existing(conn)
                media.skip_unprobed_links(conn)
            self._media_backfilled = True
        while not self._stop.is_set() and self.media.fetch_pending(limit=10):
            while self.run_one_job():
                pass
        purged = self.purge_expired()
        if purged:
            log.info("purged %d expired auto-captured threads", purged)

    def run_forever(self, idle_seconds: float = 10.0) -> None:
        log.info("bouncer started")
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                log.error("tick crashed: %s", traceback.format_exc())
            self.wake.wait(idle_seconds)
            self.wake.clear()

    def stop(self) -> None:
        self._stop.set()
        self.wake.set()

    def start_thread(self) -> threading.Thread:
        t = threading.Thread(target=self.run_forever, name="bouncer", daemon=True)
        t.start()
        return t
