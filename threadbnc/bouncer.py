"""The bouncer: a long-running worker that observes retained threads and
followed communities and appends what it sees to the archive.

It asks other servers as little as a browser would. Lemmy and PieFed posts
arrive by push (federation.py); a community is only checked on a schedule
when you turn that on, feeds and subreddits always are, and subreddits only
while you're using ThreadBNC. A post's comments, linked article and videos
are fetched when it's opened or kept (open_threads), its article and the
audio file it links to when it's scrolled into view in a feed too
(fetch_articles, fetch_audio), as are a subreddit post's text, pictures and
votes and a YouTube video's description and likes (fetch_previews), and its votes on a
schedule that slows with age and stops after a week (check_votes), from one
community listing where it can. A server that answers 429 is left alone for
as long as it asks (adapters/http.py)."""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import replace
import traceback
from datetime import timedelta
from typing import Any, Callable

from . import articles, media, store, thumbs, youtube
from . import feed as feed_mod
from .adapters import (
    CommentList,
    CommunityRef,
    HttpClient,
    RSS_DOMAIN,
    RSS_PREFIX,
    TAG_DOMAIN,
    NActor,
    NComment,
    NCommunity,
    NPost,
    RemoteError,
    RemoteNotFound,
    RemotePaused,
    RemoteUnavailable,
    ThreadiverseAdapter,
    UnsupportedSoftware,
    adapter_class,
    detect_software,
    host_of,
    is_reddit_host,
    is_rss,
    is_tag,
    parse_community_ref,
    parse_thread_url,
)
from .actor import Actor
from .adapters.activitypub import ActivityPubAdapter
from .adapters.reddit import RedditAdapter
from .adapters.rss import FeedFetcher, RssAdapter
from .config import REDDIT_MIN_POLL_MINUTES, RSS_MIN_POLL_MINUTES, Settings
from .db import Database, fmt_ts, parse_ts, utcnow
from .reddit import RedditConnection
from .vault import TokenVault
from .youtube import YouTubeSession, is_youtube_feed

log = logging.getLogger("threadbnc.bouncer")

SOFTWARE_RECHECK = timedelta(days=1)
MAX_BACKOFF = timedelta(hours=24)
AUTO_CAPTURE_PAGE = 30
MAX_POLL_PAGES = 5  # read further back until we reach known posts, up to this many pages
# How often a post's votes are updated, by its age: (age under, minutes
# between checks). Most votes come in a post's first hours. Past the last age,
# they aren't updated any more, except when the post is opened.
VOTE_SCHEDULE = [(timedelta(minutes=30), 5), (timedelta(hours=1), 10), (timedelta(hours=6), 30),
                 (timedelta(days=1), 60), (timedelta(days=7), 1440)]
# When a post's comment count and newest-comment time are unchanged, skip
# re-fetching the comment tree -- but still do a full fetch at least this often,
# since edits and some deletions don't change those counters.
FULL_FETCH_EVERY = timedelta(hours=6)
# Communities whose changes are pushed through your own server (federation.py)
# are still checked this often, to catch anything a delivery missed.
PUSHED_POLL_MINUTES = 360
# Opening a post re-reads its comments unless they were read this recently.
OPEN_CACHE = timedelta(minutes=5)
# Subreddits and YouTube channels (whose pages are read, see youtube.py) are
# only checked while you're using ThreadBNC: a tab was interacted with this
# recently (app.js reports it, see note_active). Each kind's checks are spread
# out, one at a time, never closer together than this.
ACTIVE_WINDOW = timedelta(minutes=30)
REDDIT_MIN_SPACING = 30.0  # seconds


def _plus(ts: str, **delta: float) -> str:
    return fmt_ts(parse_ts(ts) + timedelta(**delta))  # type: ignore[operator]


def vote_minutes(created_at: str | None, now: str) -> int | None:
    """Minutes until the votes on a post this old are next updated (see
    VOTE_SCHEDULE), or None when it's too old for them to be."""
    created = parse_ts(created_at)
    if created is None:
        return None
    age = parse_ts(now) - created  # type: ignore[operator]
    return next((every for limit, every in VOTE_SCHEDULE if age < limit), None)


class Bouncer:
    def __init__(self, db: Database, settings: Settings, http: HttpClient | None = None,
                 adapter_factory: Callable[[str], ThreadiverseAdapter] | None = None,
                 reddit: RedditConnection | None = None):
        self.db = db
        self.settings = settings
        self.http = http or HttpClient(settings.user_agent, settings.http_timeout,
                                       settings.min_request_interval)
        vault = TokenVault(settings.credentials_key, settings.data_dir)
        self.reddit = reddit or RedditConnection(
            db, vault, timeout=settings.http_timeout, min_interval=settings.reddit_min_request_interval)
        self.youtube = YouTubeSession(db, vault)
        self.reddit_adapter = RedditAdapter(self.reddit)
        self.rss_adapter = RssAdapter(FeedFetcher(settings.user_agent, settings.http_timeout, self.http.throttle))
        # ThreadBNC's own ActivityPub identity, and the posts hashtags bring (tags.py).
        self.actor = Actor(settings.actor_domain, db, vault, self.http) if settings.actor_domain else None
        self.tag_adapter = ActivityPubAdapter(self.actor)
        self._adapter_factory = adapter_factory
        self._adapters: dict[str, tuple[ThreadiverseAdapter, str]] = {}  # domain -> (adapter, chosen at)
        # Extra work for each pass that needs accounts (e.g. private community join requests).
        self.hooks: list[Callable[[], Any]] = []
        # Other kinds of job, by kind: payload -> result (e.g. tags.py's "relayed").
        self.job_handlers: dict[str, Callable[[dict[str, Any]], Any]] = {}
        # Called with the community id after following / unfollowing (federation.py subscribes).
        self.follow_hooks: list[Callable[[int], Any]] = []
        self.unfollow_hooks: list[Callable[[int], Any]] = []
        # (domain, community id) -> a member's session token, for communities only
        # members can read (see private.py). None: read anonymously, as usual.
        self.read_token: Callable[[str, int], str | None] | None = None
        self.media_dir = settings.media_dir or (settings.data_dir / "media")
        self.media = media.MediaFetcher(db, self.media_dir, settings.user_agent, settings.media_max_bytes,
                                        timeout=max(settings.http_timeout, 30.0), throttle=self.http.throttle,
                                        transcode_default=settings.media_transcode,
                                        transcode_source_max_bytes=settings.media_transcode_source_max_bytes,
                                        youtube_session=self.youtube)
        self.articles = articles.ArticleFetcher(db, settings.user_agent, enabled=settings.archive_articles,
                                                timeout=max(settings.http_timeout, 30.0), throttle=self.http.throttle)
        self._media_backfilled = False
        self._articles_swept = 0.0  # monotonic time of the last sweep of articles read from links
        # "reddit" / "youtube" -> monotonic time of the last check of one (None: you're away)
        self._paced_clock: dict[str, float | None] = {}
        self._clock: Callable[[], float] = time.monotonic
        self.wake = threading.Event()
        self._stop = threading.Event()

    # -- adapters ----------------------------------------------------------
    def adapter_for(self, domain: str) -> ThreadiverseAdapter:
        if is_reddit_host(domain):
            return self.reddit_adapter
        if domain == RSS_DOMAIN:
            return self.rss_adapter
        if domain == TAG_DOMAIN:
            return self.tag_adapter
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
                     source_url: str, retention: str, capture: bool = False) -> int:
        """Store a post as a new thread (or return the one it already is).

        `capture`: a post arriving in a followed community (a push, or a
        community check). Only the post is stored, as read from where it
        arrived: its comments are read when it's opened (open_threads), and
        nothing else is asked of any server. Otherwise (you asked for this
        post) it's read from the best server along with its comments."""
        existing = self._existing_thread(post.ap_id)
        if existing:
            if retention == "manual" and existing["trashed_at"]:
                self.restore_from_trash(existing["id"], keep=True)
            elif retention == "manual" and existing["retention"] == "auto":
                self.promote(existing["id"])
            return existing["id"]

        if capture:
            src_domain, src_local, src_adapter = domain, local_id, adapter
            comments: list[NComment] = []
        else:
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
            if not capture:  # its comments are stored as of now
                conn.execute(
                    "UPDATE archived_threads SET last_full_fetch_at=?, remote_comment_count=?, "
                    "remote_newest_comment_at=? WHERE id=?",
                    (now, post.comment_count, post.newest_comment_at, tid),
                )
            conn.execute(
                "UPDATE archived_threads SET root_object_id=?, community_id=?, expires_at=?, next_check_at=? "
                "WHERE id=?",
                (root_id, cid, expires, self._next_vote_check(conn, cid, post.created_at, now), tid),
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
    @staticmethod
    def _follow_every(f: Any) -> int:
        """Minutes between checks of a followed community: as set, or rarely once
        its changes are pushed (federation.py), just to reconcile."""
        every = f["poll_interval_minutes"]
        return max(every, PUSHED_POLL_MINUTES) if f["push_state"] == "subscribed" else every

    @staticmethod
    def _votes_elsewhere(conn: Any, community_id: int | None) -> bool:
        """Feed articles have no votes, a subreddit's come with its listing
        while you're using ThreadBNC (poll_follow), and a hashtag's posts are
        from anywhere, so their votes are read only when opened: none gets vote checks."""
        row = conn.execute("SELECT canonical_ap_id FROM communities WHERE id=?", (community_id,)).fetchone()
        return bool(row) and (is_rss(row["canonical_ap_id"]) or is_tag(row["canonical_ap_id"])
                              or is_reddit_host(host_of(row["canonical_ap_id"])))

    def _next_vote_check(self, conn: Any, community_id: int | None, created_at: str | None,
                         now: str | None = None) -> str | None:
        """When a thread's votes are next updated (VOTE_SCHEDULE), or None."""
        now = now or utcnow()
        minutes = None if self._votes_elsewhere(conn, community_id) else vote_minutes(created_at, now)
        return _plus(now, minutes=minutes) if minutes else None

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

    def sync_thread(self, thread_id: int, force: bool = False, comments: bool = True) -> store.ApplyResult:
        """Re-observe a thread. The post is always fetched; the comment tree is
        skipped when the post's counters are unchanged, unless `force` or the
        last full fetch is older than FULL_FETCH_EVERY, and always when not
        `comments` (a vote check)."""
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
            full = comments and post is not None and (force or not self._comments_unchanged(t, post, now))
            tree = adapter.fetch_comments(local) if full else []
        except RemotePaused as exc:  # the server asked us to wait: check again when it said
            with self.db.transaction() as conn:
                conn.execute("UPDATE archived_threads SET next_check_at=? WHERE id=?",
                             (_plus(now, seconds=exc.seconds), thread_id))
            raise
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
                    store.apply_comments(conn, thread_id, root_id, t["community_id"], tree, domain, now,
                                         False, getattr(tree, "complete", True), result)
                    conn.execute(
                        "UPDATE archived_threads SET last_full_fetch_at=?, remote_comment_count=?, "
                        "remote_newest_comment_at=? WHERE id=?",
                        (now, post.comment_count, post.newest_comment_at, thread_id),
                    )
            conn.execute(
                "UPDATE archived_threads SET last_checked_at=?, last_success_at=?, consecutive_failures=0, "
                "last_error=NULL, next_check_at=? WHERE id=?",
                (now, now, self._next_vote_check(conn, t["community_id"],
                                                 self._root_created(conn, t["root_object_id"]), now), thread_id),
            )
            self._server_answers(conn, t["community_id"], domain, now)
        if post is not None:
            self._enrich_moderation(adapter, result, post.community)
            if full:
                self._maybe_move_source(thread_id, post, domain, local)
        return result

    # -- opening -----------------------------------------------------------
    def open_threads(self, thread_ids: list[int]) -> None:
        """What opening (or keeping) posts fetches, as a browser would: each
        one's comments and votes unless read within OPEN_CACHE, and the article
        it links to if it isn't saved yet. Videos in them are no longer held
        (media.py), so the next media pass downloads them."""
        first_error: RemoteError | None = None
        for tid in thread_ids:
            with self.db.connect() as conn:
                t = conn.execute("SELECT * FROM archived_threads WHERE id=?", (tid,)).fetchone()
            if t is None or t["trashed_at"] or not t["root_object_id"]:
                continue
            if self.comments_stale(t):
                try:
                    if video := self._youtube_video(t):
                        self.fetch_youtube_comments(t, video)
                    else:
                        self.sync_thread(tid, force=True)
                except RemoteError as exc:
                    first_error = first_error or exc
            self.fetch_article(t["root_object_id"])
        self.wake.set()
        if first_error:
            raise first_error

    def comments_stale(self, t: Any) -> bool:
        """True when opening this thread should re-read its comments. Feed
        articles have none; YouTube videos do."""
        if t["source_domain"] == RSS_DOMAIN and not self._youtube_video(t):
            return False
        last = parse_ts(t["last_full_fetch_at"])
        return last is None or parse_ts(utcnow()) - last >= OPEN_CACHE  # type: ignore[operator]

    def _youtube_video(self, t: Any) -> str | None:
        """The id of the video a post from a followed YouTube channel is, else None."""
        if t["source_domain"] != RSS_DOMAIN or not t["root_object_id"]:
            return None
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT c.canonical_ap_id AS c_ap, r.url FROM archived_threads t "
                "JOIN communities c ON c.id=t.community_id JOIN objects o ON o.id=t.root_object_id "
                "JOIN revisions r ON r.object_id=o.id AND r.seq=o.revision_count WHERE t.id=?", (t["id"],)).fetchone()
        return youtube.video_id(row["url"]) if row and is_youtube_feed(row["c_ap"]) else None

    def fetch_youtube_comments(self, t: Any, video: str) -> None:
        """A YouTube video's post, opened: its top comments and some of their
        replies (FeedFetcher.youtube_discussion), and its description and likes
        from the same page read. Comments not in what was read are only taken
        as gone when that was all of them."""
        watch, found, complete = self.rss_adapter.fetcher.youtube_discussion(video)
        now = utcnow()
        tree = CommentList(NComment(
            ap_id=f"rss:yt:comment:{c.id}", local_id=c.id, parent_local_id=c.parent_id,
            body=youtube.description_markdown(c.text) or "", created_at=fmt_ts(c.published) if c.published else None,
            updated_at=None, deleted=False, removed=False,
            author=NActor(f"https://www.youtube.com/channel/{c.channel_id}" if c.channel_id
                          else f"https://www.youtube.com/{c.author}", c.author, "youtube.com"),
            metadata={"edited": True} if c.edited else {}, score=c.likes, upvotes=c.likes) for c in found)
        result = store.ApplyResult()
        with self.db.transaction() as conn:
            store.observe_text(conn, t["root_object_id"], watch.description, now)
            if watch.likes is not None:
                conn.execute("UPDATE objects SET score=?, upvotes=?, downvotes=NULL, last_seen_at=? WHERE id=?",
                             (watch.likes, watch.likes, now, t["root_object_id"]))
            store.apply_comments(conn, t["id"], t["root_object_id"], t["community_id"], tree, RSS_DOMAIN, now,
                                 False, complete, result)
            conn.execute("UPDATE archived_threads SET last_full_fetch_at=?, previewed_at=?, last_checked_at=? "
                         "WHERE id=?", (now, now, now, t["id"]))

    def article_for(self, root_object_id: int) -> Any:
        with self.db.connect() as conn:
            o = conn.execute("SELECT r.url FROM objects o JOIN revisions r ON r.object_id=o.id "
                             "AND r.seq=o.revision_count WHERE o.id=?", (root_object_id,)).fetchone()
            return articles.for_object(conn, root_object_id, o["url"] if o else None)

    def fetch_article(self, root_object_id: int) -> None:
        """Save the article a post links to, if it's waiting to be."""
        a = self.article_for(root_object_id)
        if a is not None and a["status"] == "pending" and self.articles.enabled:
            self.articles.fetch_one(a)

    def fetch_articles(self, thread_ids: list[int]) -> None:
        """Posts scrolled into view in a feed: the articles they link to, and
        the pictures in them, so the feed can show those too. Only the posts
        the reader looked at, as a browser showing link previews would."""
        for tid in thread_ids:
            if self._stop.is_set():
                return
            with self.db.connect() as conn:
                t = conn.execute("SELECT root_object_id, trashed_at FROM archived_threads WHERE id=?",
                                 (tid,)).fetchone()
            if t is None or t["trashed_at"] or not t["root_object_id"]:
                continue
            self.fetch_article(t["root_object_id"])
            a = self.article_for(t["root_object_id"])
            if a is None or a["status"] != "ok":
                continue
            with self.db.connect() as conn:
                pics = conn.execute(
                    "SELECT m.* FROM article_media am JOIN media m ON m.id=am.media_id WHERE am.article_id=? "
                    "AND m.status='pending' AND m.held=0 AND (m.next_attempt_at IS NULL OR m.next_attempt_at<=?) "
                    "ORDER BY m.id", (a["id"], utcnow())).fetchall()
            for m in pics:
                self.media.fetch_one(m)

    def fetch_audio(self, thread_ids: list[int]) -> None:
        """Posts scrolled into view in a feed that link to an audio file: download
        it, so it's there to play in the post's player bar. Only the posts the
        reader looked at, as a browser showing a player would."""
        for tid in thread_ids:
            if self._stop.is_set():
                return
            with self.db.connect() as conn:
                t = conn.execute("SELECT root_object_id, trashed_at FROM archived_threads WHERE id=?",
                                 (tid,)).fetchone()
                if t is None or t["trashed_at"] or not t["root_object_id"]:
                    continue
                o = conn.execute("SELECT r.url FROM objects o JOIN revisions r ON r.object_id=o.id "
                                 "AND r.seq=o.revision_count WHERE o.id=?", (t["root_object_id"],)).fetchone()
                sound = feed_mod.audio(conn, [(t["root_object_id"], o["url"] if o else None)])
                a = sound.get(t["root_object_id"])
                m = conn.execute("SELECT * FROM media WHERE id=? AND status='pending' "
                                 "AND (next_attempt_at IS NULL OR next_attempt_at<=?)",
                                 (a["id"], utcnow())).fetchone() if a else None
            if m is not None:
                self.media.fetch_one(m, wanted=True)

    def fetch_previews(self, thread_ids: list[int]) -> None:
        """Posts from subreddits and YouTube channels scrolled into view in a
        feed: their text, pictures and votes, as a browser showing the post
        would. A subreddit's listing is stored without the text (poll_follow)
        and a channel's page has no descriptions or likes. Subreddit posts are
        read in one request for all of them, a video's details from its page.
        Ones read within feed.PREVIEW_FRESH aren't read again. Each is marked
        read (previewed_at, which the feed watches for) once its pictures are
        in too, so its entry is shown again only once, complete."""
        now = utcnow()
        with self.db.connect() as conn:
            rows = [r for r in feed_mod.preview_rows(conn, thread_ids)
                    if feed_mod.preview_due(r["kind"], r["previewed_at"], now)]
        reddit = [r for r in rows if r["kind"] == "reddit"]
        if reddit and self._reddit_previews(reddit):
            self._fetch_pictures([r["oid"] for r in reddit])
            self._previewed([r["id"] for r in reddit])
        for r in rows:
            if self._stop.is_set():
                return
            if r["kind"] == "youtube" and self._youtube_preview(r):
                self._fetch_pictures([r["oid"]])
                self._previewed([r["id"]])

    def _previewed(self, thread_ids: list[int]) -> None:
        now = utcnow()
        with self.db.transaction() as conn:
            for tid in thread_ids:
                conn.execute("UPDATE archived_threads SET previewed_at=? WHERE id=?", (now, tid))

    def _reddit_previews(self, rows: list[dict[str, Any]]) -> bool:
        """Read these subreddit posts, in one request. False if Reddit didn't answer."""
        now = utcnow()
        try:
            posts = self.reddit_adapter.fetch_posts([r["source_local_id"] for r in rows])
        except RemoteError as exc:
            log.warning("reading %d subreddit posts for the feed failed: %s", len(rows), exc)
            return False
        by_id = {p.local_id: p for p in posts}
        result = store.ApplyResult()
        with self.db.transaction() as conn:
            store.record_instance_contact(conn, rows[0]["source_domain"], now, True)
            for r in rows:
                # One not returned is gone from Reddit: opening it finds out what happened.
                if post := by_id.get(r["source_local_id"]):
                    store.apply_post(conn, r["id"], post, r["source_domain"], now, False, result)
        if posts:
            self._enrich_moderation(self.reddit_adapter, result, posts[0].community)
        return True

    def _youtube_preview(self, r: dict[str, Any]) -> bool:
        """Read a video's description and likes from its page. False if YouTube didn't answer."""
        now = utcnow()
        try:
            watch = self.rss_adapter.fetcher.youtube_video(youtube.video_id(r["url"]) or "")
        except RemoteNotFound:
            watch = None  # gone, or never there: don't ask again until it's due
        except RemoteError as exc:
            log.warning("reading the YouTube video %s for the feed failed: %s", r["url"], exc)
            return False
        if watch is not None:
            with self.db.transaction() as conn:
                store.observe_text(conn, r["oid"], watch.description, now)
                if watch.likes is not None:
                    conn.execute("UPDATE objects SET score=?, upvotes=?, downvotes=NULL, last_seen_at=? WHERE id=?",
                                 (watch.likes, watch.likes, now, r["oid"]))
        return True

    def _fetch_pictures(self, object_ids: list[int]) -> None:
        """Pictures in these posts not downloaded yet, so the feed can show them."""
        if not object_ids:
            return
        with self.db.connect() as conn:
            pics = conn.execute(
                f"SELECT DISTINCT m.* FROM media_refs mr JOIN media m ON m.id=mr.media_id "
                f"WHERE mr.object_id IN ({','.join('?' * len(object_ids))}) AND mr.from_article=0 "
                f"AND m.status='pending' AND m.held=0 AND m.kept_only=0 "
                f"AND (m.next_attempt_at IS NULL OR m.next_attempt_at<=?) ORDER BY m.id",
                [*object_ids, utcnow()]).fetchall()
        for m in pics:
            if self._stop.is_set():
                return
            self.media.fetch_one(m)

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
        if not home or home == domain or domain in self.http.throttle.exempt:
            return  # already home, or on your own server, which pushes bring everything to
        try:
            new_domain, new_local, _ = self._best_source(post, domain, local, self.adapter_for(domain))
        except RemoteError:
            return
        if new_domain != domain:
            with self.db.transaction() as conn:
                conn.execute("UPDATE archived_threads SET source_domain=?, source_local_id=?, "
                             "last_full_fetch_at=NULL WHERE id=?", (new_domain, new_local, thread_id))
            log.info("thread %s now polled from %s (was %s)", thread_id, new_domain, domain)

    @staticmethod
    def _server_answers(conn: Any, community_id: int, domain: str, now: str) -> None:
        """A thread synced from the server a failing community check is backed
        off on: check the community again at its usual interval rather than
        after the backoff (up to a day), so a warning that no longer applies clears."""
        f = conn.execute("SELECT last_polled_at, poll_interval_minutes, next_poll_at FROM community_follows "
                         "WHERE community_id=? AND active=1 AND consecutive_failures>0 AND source_domain=?",
                         (community_id, domain)).fetchone()
        if not f or not f["last_polled_at"]:
            return
        due = max(_plus(f["last_polled_at"], minutes=f["poll_interval_minutes"]), now)
        if f["next_poll_at"] and f["next_poll_at"] > due:
            conn.execute("UPDATE community_follows SET next_poll_at=? WHERE community_id=?", (due, community_id))

    def check_follow_now(self, community_id: int) -> None:
        with self.db.transaction() as conn:
            conn.execute("UPDATE community_follows SET next_poll_at=? WHERE community_id=?", (utcnow(), community_id))
        self.wake.set()

    def _sync_failed(self, t: Any, error: str) -> None:
        now = utcnow()
        with self.db.transaction() as conn:
            store.record_instance_contact(conn, t["source_domain"], now, False, error)
            failures = t["consecutive_failures"] + 1
            minutes = vote_minutes(self._root_created(conn, t["root_object_id"]), now)
            if minutes is None or self._votes_elsewhere(conn, t["community_id"]):
                nxt = None  # no more vote checks to back off from
            else:
                delay = min(timedelta(minutes=max(minutes, 5)) * (2 ** min(failures, 10)), MAX_BACKOFF)
                nxt = fmt_ts(parse_ts(now) + delay)  # type: ignore[operator]
            conn.execute(
                "UPDATE archived_threads SET last_checked_at=?, consecutive_failures=?, last_error=?, "
                "next_check_at=? WHERE id=?",
                (now, failures, error, nxt, t["id"]),
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

    @staticmethod
    def always_polled(domain: str) -> bool:
        """Feeds and subreddits can't push, so following them means checking them."""
        return domain == RSS_DOMAIN or is_reddit_host(domain)

    def follow_community(self, text: str, poll_interval_minutes: int | None = None,
                         retention_days: int | None = -1, backfill: bool = False,
                         polling: bool | None = None, keep_existing: bool = False) -> int:
        """Follow a community. Lemmy and PieFed communities arrive by push
        through your own server (the follow hooks subscribe) and aren't checked
        on a schedule unless `polling`; feeds and subreddits always are.
        With `keep_existing`, one already followed is left as it is."""
        ref, community = self.resolve_community(text)
        now = utcnow()
        interval = self.poll_interval(ref.domain, poll_interval_minutes)
        days = self.settings.default_follow_retention_days if retention_days == -1 else retention_days
        polled = self.always_polled(ref.domain) or bool(polling)
        with self.db.transaction() as conn:
            cid = store.upsert_community(conn, community, now)
            if keep_existing and conn.execute("SELECT 1 FROM community_follows WHERE community_id=? AND active=1",
                                              (cid,)).fetchone():
                return cid
            conn.execute(
                "INSERT INTO community_follows(community_id, active, followed_at, capture_since, "
                "poll_interval_minutes, retention_days, source_domain, source_ref, next_poll_at, polling) "
                "VALUES (?,1,?,?,?,?,?,?,?,?) ON CONFLICT(community_id) DO UPDATE SET active=1, "
                "unfollowed_at=NULL, last_error=NULL, consecutive_failures=0, "
                "poll_interval_minutes=excluded.poll_interval_minutes, "
                "retention_days=excluded.retention_days, source_domain=excluded.source_domain, "
                "source_ref=excluded.source_ref, capture_since=excluded.capture_since, next_poll_at=?, "
                "polling=excluded.polling",
                (cid, now, "1970-01-01T00:00:00.000000Z" if backfill else now, interval, days,
                 ref.domain, ref.qualified, now, int(polled), now),
            )
            self._reapply_expiry(conn, cid)
            store.add_event(conn, "followed", now, community_id=cid,
                            metadata={"poll_interval_minutes": interval, "retention_days": days,
                                      "source_domain": ref.domain})
        self.wake.set()
        self._run_hooks(self.follow_hooks, cid)
        return cid

    @staticmethod
    def _run_hooks(hooks: list[Callable[[int], Any]], community_id: int) -> None:
        for hook in hooks:
            try:
                hook(community_id)
            except Exception:  # following worked; a hook's trouble is its own
                log.error("follow hook %s crashed: %s", getattr(hook, "__qualname__", hook), traceback.format_exc())

    def poll_interval(self, domain: str, asked: int | None) -> int:
        """How often to check a community: what was asked for, else the default
        for its kind of server. Subreddits are never checked more often than
        every REDDIT_MIN_POLL_MINUTES."""
        if is_reddit_host(domain):
            return max(asked or self.settings.reddit_poll_minutes, REDDIT_MIN_POLL_MINUTES)
        if domain == RSS_DOMAIN:
            return max(asked or self.settings.rss_poll_minutes, RSS_MIN_POLL_MINUTES)
        return asked or self.settings.default_follow_poll_minutes

    def set_polling(self, community_id: int, on: bool) -> None:
        """Check a Lemmy or PieFed community on a schedule (when its posts can't
        be pushed), or stop. Feeds and subreddits are always checked."""
        now = utcnow()
        with self.db.transaction() as conn:
            f = conn.execute("SELECT source_domain FROM community_follows WHERE community_id=?",
                             (community_id,)).fetchone()
            if f is None or self.always_polled(f["source_domain"]):
                return
            conn.execute("UPDATE community_follows SET polling=?, next_poll_at=?, last_error=NULL, "
                         "consecutive_failures=0 WHERE community_id=?", (int(on), now, community_id))
            store.add_event(conn, "follow_settings_changed", now, community_id=community_id,
                            metadata={"polling": on})
        self.wake.set()

    @staticmethod
    def arriving(f: Any) -> bool:
        """Whether a follow's posts arrive at all: pushed, or checked."""
        return f["push_state"] == "subscribed" or bool(f["polling"])

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
        self._run_hooks(self.unfollow_hooks, community_id)

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
        if f["push_state"] == "subscribed" and f["push_domain"]:
            # Pushed: catch anything a delivery missed by reading the community
            # as your own server has it, which asks nothing of anyone else.
            ref = CommunityRef(f["push_domain"], ref.name, ref.home or ref.domain)
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
        except RemotePaused as exc:  # not a failure: the server asked us to wait, so we do
            with self.db.transaction() as conn:
                conn.execute("UPDATE community_follows SET next_poll_at=? WHERE community_id=?",
                             (_plus(now, seconds=exc.seconds), community_id))
            raise
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
        with self.db.transaction() as conn:  # the listing's votes for posts already stored, free
            for post in posts:
                store.update_counts(conn, post, now)
        captured = 0
        for post in reversed(posts):  # oldest first, so feed order matches capture order
            created = parse_ts(post.created_at)
            if created and since and created < since:
                continue
            if self._existing_thread(post.ap_id):
                continue
            if is_reddit_host(ref.domain) and post.body:
                # A subreddit's posts are stored as their title, link and pictures;
                # the text is read with the comments when the post is opened.
                post = replace(post, body=None, metadata={**post.metadata, store.BODY_DEFERRED: True})
            try:
                self._ingest_post(post, ref.domain, post.local_id, adapter, source_url=post.ap_id,
                                  retention="auto", capture=True)
                captured += 1
            except RemoteError as exc:
                log.warning("auto-capture of %s failed: %s", post.ap_id, exc)
        with self.db.transaction() as conn:
            store.record_instance_contact(conn, ref.domain, now, True)
            conn.execute(
                "UPDATE community_follows SET last_polled_at=?, last_error=NULL, consecutive_failures=0, "
                "next_poll_at=? WHERE community_id=?",
                (now, _plus(now, minutes=self._follow_every(f)), community_id),
            )
        return captured

    # -- you, using ThreadBNC --------------------------------------------------
    def note_active(self) -> None:
        """Someone is using ThreadBNC (a tab was interacted with)."""
        with self.db.transaction() as conn:
            conn.execute("INSERT INTO app_settings(key, value) VALUES ('last_active_at', ?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (utcnow(),))

    def user_active(self) -> bool:
        seen = parse_ts(self.db.get_setting("last_active_at"))
        return seen is not None and parse_ts(utcnow()) - seen < ACTIVE_WINDOW  # type: ignore[operator]

    @staticmethod
    def paced_kind(follow: Any) -> str | None:
        """"reddit" or "youtube" for follows only checked while you're using
        ThreadBNC (see _next_paced), None for the rest."""
        if is_reddit_host(follow["source_domain"]):
            return "reddit"
        if follow["source_domain"] == RSS_DOMAIN and is_youtube_feed(RSS_PREFIX + (follow["source_ref"] or "")):
            return "youtube"
        return None

    def _next_paced(self, kind: str, due: list[Any], followed: list[Any]) -> int | None:
        """The subreddit (or YouTube channel) to check now, if any: only while
        you're using ThreadBNC, and one at a time, spread across the check
        interval so coming back doesn't set off a burst. None when it's not
        time yet."""
        if not self.user_active():
            self._paced_clock[kind] = None  # away: when you're back, start counting from then
            return None
        clock = self._clock()
        last = self._paced_clock.get(kind)
        if last is None:
            self._paced_clock[kind] = clock
            return None
        every = min(f["poll_interval_minutes"] for f in followed) * 60
        spacing = max(REDDIT_MIN_SPACING, every / len(followed))
        if not due or clock - last < spacing:
            return None
        self._paced_clock[kind] = clock
        return due[0]["community_id"]

    # -- votes ---------------------------------------------------------------
    def check_votes(self, limit: int = 200) -> int:
        """Update the votes on posts due for it (VOTE_SCHEDULE), asking as
        little as possible: a pushed community's from your own server's copy,
        a checked community's from one listing for all its due posts, and only
        a post kept on its own (no followed community to list) by itself.
        Returns how many posts were due."""
        now = utcnow()
        with self.db.connect() as conn:
            due = conn.execute(
                "SELECT t.id, t.community_id, t.source_domain, o.canonical_ap_id AS ap_id, o.created_at, "
                "f.community_id AS followed, f.polling, f.push_state, f.push_domain "
                "FROM archived_threads t JOIN objects o ON o.id=t.root_object_id "
                "LEFT JOIN community_follows f ON f.community_id=t.community_id AND f.active=1 "
                "WHERE t.active=1 AND t.trashed_at IS NULL AND t.next_check_at IS NOT NULL AND t.next_check_at<=? "
                "ORDER BY t.next_check_at LIMIT ?", (now, limit)).fetchall()
        by_community: dict[int, list[Any]] = {}
        alone: list[int] = []
        nowhere: list[Any] = []
        for r in due:
            if r["source_domain"] in (RSS_DOMAIN, TAG_DOMAIN) or is_reddit_host(r["source_domain"]):
                nowhere.append(r)  # their votes come another way (_votes_elsewhere)
            elif r["followed"] is None:
                alone.append(r["id"])
            elif r["push_state"] == "subscribed" and r["push_domain"] or r["polling"]:
                by_community.setdefault(r["community_id"], []).append(r)
            else:
                nowhere.append(r)  # followed, but neither pushed nor checked: nowhere to ask
        if nowhere:
            with self.db.transaction() as conn:
                for r in nowhere:
                    conn.execute("UPDATE archived_threads SET next_check_at=NULL WHERE id=?", (r["id"],))
        for cid, rows in by_community.items():
            if self._stop.is_set():
                break
            self._votes_from_listing(cid, rows)
        for tid in alone:
            if self._stop.is_set():
                break
            while self.run_one_job():  # opening a post jumps the queue
                pass
            try:
                self.sync_thread(tid, comments=False)
            except RemoteError as exc:
                log.warning("vote check of thread %s failed: %s", tid, exc)
        return len(due)

    def _votes_from_listing(self, community_id: int, due: list[Any]) -> None:
        """One community listing (newest first) for all its due posts: as your
        own server has it when it's pushed, else from where it's checked."""
        with self.db.connect() as conn:
            f = conn.execute("SELECT * FROM community_follows WHERE community_id=?", (community_id,)).fetchone()
        ref = self.community_ref(community_id)
        if f["push_state"] == "subscribed" and f["push_domain"]:
            ref = CommunityRef(f["push_domain"], ref.name, ref.home or ref.domain)
        now = utcnow()
        wanted = {r["ap_id"] for r in due}
        oldest = min((r["created_at"] for r in due if r["created_at"]), default=None)
        seen: dict[str, NPost] = {}
        try:
            adapter = self.reader(ref.domain, community_id)
            size = getattr(adapter, "poll_page_size", AUTO_CAPTURE_PAGE)
            for page in range(1, MAX_POLL_PAGES + 1):
                batch = adapter.list_community_posts(ref, sort="New", page=page, limit=size)
                seen.update((p.ap_id, p) for p in batch)
                reached = [p.created_at for p in batch if not p.featured and p.created_at]
                if wanted <= seen.keys() or len(batch) < size or (oldest and reached and min(reached) < oldest):
                    break
        except RemotePaused as exc:  # the server asked us to wait: ask again when it said
            with self.db.transaction() as conn:
                for r in due:
                    conn.execute("UPDATE archived_threads SET next_check_at=? WHERE id=?",
                                 (_plus(now, seconds=exc.seconds), r["id"]))
            return
        except RemoteError as exc:  # try at the next time on the schedule
            log.warning("vote check of community %s failed: %s", community_id, exc)
            seen = {}
        with self.db.transaction() as conn:
            for post in seen.values():
                store.update_counts(conn, post, now)
            for r in due:  # found or not (too far down the listing), each moves on to its next time
                conn.execute("UPDATE archived_threads SET next_check_at=?, last_checked_at=? WHERE id=?",
                             (self._next_vote_check(conn, community_id, r["created_at"], now), now, r["id"]))

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
            if time.monotonic() - self._articles_swept > 3600:  # articles read from links, no longer wanted
                self._articles_swept = time.monotonic()
                if articles.collect_orphans(conn, now):
                    orphan_files += media.collect_orphans(conn, self.media_dir)
        for f in orphan_files:  # only after the purge committed
            f.unlink(missing_ok=True)
            thumbs.remove_for(self.media_dir, f.stem)  # its smaller copies too (files are named by hash)
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
                    retry_in: timedelta | None = None, refund: bool = False) -> None:
        """`refund`: this attempt doesn't count (the server asked us to wait)."""
        now = utcnow()
        with self.db.transaction() as conn:
            if retry_in is not None:
                conn.execute("UPDATE jobs SET status='queued', error=?, updated_at=?, run_after=?, "
                             "attempts=attempts-? WHERE id=?",
                             (error, now, fmt_ts(parse_ts(now) + retry_in), int(refund), job_id))  # type: ignore[operator]
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
                if payload.get("retention", "manual") == "manual":
                    self.open_threads([tid])  # kept: its article too
                self._finish_job(job["id"], "done", {"thread_id": tid})
            elif job["kind"] == "open":
                self.open_threads(payload["thread_ids"])
                self._finish_job(job["id"], "done", {"thread_ids": payload["thread_ids"]})
            elif job["kind"] == "articles":
                self.fetch_articles(payload["thread_ids"])
                self._finish_job(job["id"], "done", {"thread_ids": payload["thread_ids"]})
            elif job["kind"] == "audio":
                self.fetch_audio(payload["thread_ids"])
                self._finish_job(job["id"], "done", {"thread_ids": payload["thread_ids"]})
            elif job["kind"] == "previews":
                self.fetch_previews(payload["thread_ids"])
                self._finish_job(job["id"], "done", {"thread_ids": payload["thread_ids"]})
            elif job["kind"] == "sync":
                self.sync_thread(payload["thread_id"], force=True)
                self._finish_job(job["id"], "done", {"thread_id": payload["thread_id"]})
            elif job["kind"] == "poll":  # Refresh on a checked community: now, not at its turn
                with self.db.connect() as conn:
                    followed = conn.execute("SELECT 1 FROM community_follows WHERE community_id=? AND active=1",
                                            (payload["community_id"],)).fetchone()
                captured = self.poll_follow(payload["community_id"]) if followed else 0
                self._finish_job(job["id"], "done", {"community_id": payload["community_id"], "captured": captured})
            elif job["kind"] in self.job_handlers:
                self._finish_job(job["id"], "done", self.job_handlers[job["kind"]](payload))
            else:
                self._finish_job(job["id"], "failed", error=f"unknown job kind {job['kind']}")
        except RemotePaused as exc:
            self._finish_job(job["id"], "queued", error=str(exc), retry_in=timedelta(seconds=exc.seconds),
                             refund=True)
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
            # Checked: those with polling on, and pushed ones now and then (on
            # your own server). Anything else waits for pushes.
            checked = conn.execute(
                "SELECT community_id, source_domain, source_ref, poll_interval_minutes, next_poll_at "
                "FROM community_follows "
                "WHERE active=1 AND (polling=1 OR push_state='subscribed') ORDER BY next_poll_at").fetchall()
        due = [r for r in checked if not r["next_poll_at"] or r["next_poll_at"] <= now]
        follows = [r["community_id"] for r in due if not self.paced_kind(r)]
        for kind in ("reddit", "youtube"):
            followed = [r for r in checked if self.paced_kind(r) == kind]
            if followed:
                cid = self._next_paced(kind, [r for r in due if self.paced_kind(r) == kind], followed)
                if cid is not None:
                    follows.append(cid)
        for cid in follows:
            if self._stop.is_set():
                return
            try:
                n = self.poll_follow(cid)
                if n:
                    log.info("community %s: auto-captured %d posts", cid, n)
            except RemoteError as exc:
                log.warning("poll of community %s failed: %s", cid, exc)
        # Comments are never checked in the background: they're read when a
        # post is opened (open_threads, a job), like a browser would. Votes are
        # updated for a week, less often as posts age.
        if not self._stop.is_set():
            self.check_votes()
        if not self._media_backfilled:
            with self.db.transaction() as conn:
                media.register_all_existing(conn)
                media.skip_unprobed_links(conn)
                media.register_youtube_links(conn)
                thumbs.request_pass_if_needed(conn)
                articles.register_all_existing(conn)
            self._media_backfilled = True
        # Linked articles aren't fetched in the background either: opening or
        # keeping a post saves its article (open_threads), and so does
        # scrolling to it in a feed (fetch_articles).
        while not self._stop.is_set() and self.media.fetch_pending(limit=10):
            while self.run_one_job():
                pass
        # Archived files over limits just lowered, transcoded down one at a time.
        while not self._stop.is_set() and self.media.convert_some():
            while self.run_one_job():
                pass
        # Smaller copies of pictures for browsing, after their widths changed.
        while not self._stop.is_set() and thumbs.run_some(self.db, self.media_dir):
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
