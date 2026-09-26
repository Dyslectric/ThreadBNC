"""Bluesky's Jetstream: following hashtags there, and counting what's
posted and talked about for the Trending page.

Bluesky can't follow a hashtag either, and nothing on it can be subscribed to
for one: everything posted anywhere on Bluesky goes out on one public stream
instead. Jetstream (https://github.com/bluesky-social/jetstream) serves that
stream as JSON over a WebSocket, and can be asked for new posts only (no
follows or reposts), or posts and likes.

So while a hashtag is followed, or what's posted on Bluesky is counted for the
Trending page (trends.py, on unless you turn it off there), ThreadBNC keeps one
connection open to Jetstream and reads every post made on Bluesky as it's made
(about 25 a second in September 2026: about 0.75 GB a day compressed). Every
post's links are counted, and so are replies (for the post that started the
thread) and quotes; with likes counted from the stream too (a choice on the
Trending page), likes come as well: about 150 a second, another 3.4 GB a day.
Of the posts themselves it keeps only those with a followed hashtag:

1. A new post (not a reply: replies belong under their post) with a followed
   hashtag, in its text or beside it, is noted with the first such hashtag.
2. Every few seconds the posts noted are read from Bluesky's AppView, up to
   25 in one request (a job, so the one worker keeps its pace), and captured
   into their hashtag's feed, beside those the relay brings from the
   fediverse (tags.py). They expire like any other auto-captured post unless
   you keep them. A post the AppView doesn't have yet is asked for once more
   a little later; one it still doesn't show (deleted, or hidden by
   Bluesky's moderation) is left out.
3. Where the stream got to is saved now and then. Reconnecting, or starting
   again, carries on from there if that's under an hour ago, so a short
   outage misses nothing.

The events are asked for compressed (zstd, each one on its own, with a
dictionary Jetstream publishes, which roughly halves the traffic). ThreadBNC
keeps a copy of that dictionary (jetstream_zstd_dictionary, from Bluesky's
jetstream-legacy repository, MIT licensed: jetstream_zstd_dictionary.LICENSE).
Without it, or without zstd in this Python, or when events stop decompressing
with it (Jetstream changed its dictionary), they're read uncompressed.

Nothing is sent to Jetstream but the request to listen. With no hashtag
followed and nothing counted, the connection is closed."""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import traceback
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse

from websockets.exceptions import WebSocketException
from websockets.sync.client import connect

from .adapters import BSKY_DOMAIN
from . import trends
from .adapters.bluesky import GET_POSTS, LIKE, POST, record_tags, web_url
from .bouncer import Bouncer
from .db import parse_ts, utcnow
from .traffic import record

try:
    from compression import zstd
except ImportError:  # a Python built without zstd: events are read uncompressed
    zstd = None  # type: ignore[assignment]

log = logging.getLogger(__name__)

JOB = "bluesky_tagged"
CURSOR = "jetstream_cursor"  # setting: the time (microseconds) of the last event seen
RESUME_WITHIN = 3600 * 1_000_000  # microseconds: an older place isn't gone back to
FIRST_TRY = timedelta(seconds=15)  # the AppView may be a moment behind the stream
RETRY_AFTER = timedelta(minutes=2)
FLUSH_EVERY = 5.0  # seconds between batches of posts noted
SAVE_EVERY = 30.0  # seconds between saves of where the stream got to
TAGS_EVERY = 60.0  # the followed hashtags (and what's counted) are read again at least this often
MAX_BACKOFF = 300.0
DICTIONARY = Path(__file__).with_name("jetstream_zstd_dictionary")
MAX_EVENT = 2 ** 20  # bytes: the most one event may decompress to
BAD_FRAMES = 20  # this many in a row that won't decompress: the dictionary no longer fits
_TIME_US = re.compile(r'"time_us"\s*:\s*(\d+)')
FOLLOWED_SQL = ("SELECT c.name, f.capture_since FROM community_follows f JOIN communities c ON c.id=f.community_id "
                "WHERE f.active=1 AND c.canonical_ap_id LIKE 'tag:%'")


def load_dictionary() -> Any:
    """Jetstream's zstd dictionary, ready to decompress events with, or None
    when this Python has no zstd or ThreadBNC's copy is missing or broken."""
    if zstd is None:
        log.warning("Jetstream: this Python has no zstd, so events are read uncompressed")
        return None
    try:
        return zstd.ZstdDict(DICTIONARY.read_bytes()).as_digested_dict
    except (OSError, zstd.ZstdError) as exc:
        log.warning("Jetstream: its dictionary can't be read (%s), so events are read uncompressed", exc)
        return None


def _event(raw: str | bytes) -> dict[str, Any] | None:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    try:
        event = json.loads(raw)
    except ValueError:
        return None
    return event if isinstance(event, dict) else None


def _created(event: dict[str, Any], collection: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """(commit, record) when an event is something new in `collection`."""
    commit = event.get("commit")
    if not isinstance(commit, dict) or commit.get("operation") != "create" or commit.get("collection") != collection:
        return None
    record = commit.get("record")
    if not isinstance(record, dict) or not event.get("did") or not commit.get("rkey"):
        return None
    return commit, record


def tagged(event: dict[str, Any], tags: set[str]) -> tuple[str, str] | None:
    """(at:// address, hashtag) when an event is a new post, not a reply,
    with one of `tags`; else None."""
    made = _created(event, POST) if tags else None
    if made is None or made[1].get("reply"):
        return None
    tag = next((t for t in record_tags(made[1]) if t in tags), None)
    return (f"at://{event['did']}/{POST}/{made[0]['rkey']}", tag) if tag else None


def match(raw: str | bytes, tags: set[str]) -> tuple[str, str] | None:
    """tagged(), for an event as the stream sends it."""
    event = _event(raw)
    return tagged(event, tags) if event is not None else None


def _recent(uri: Any) -> str | None:
    """When a post was made, if it's one made recently enough to count
    replies to and likes of (trends.MAX_POST_AGE)."""
    made = trends.post_time(uri) if isinstance(uri, str) else None
    moment = parse_ts(made)
    if moment is None or moment < parse_ts(utcnow()) - trends.MAX_POST_AGE:  # type: ignore[operator]
        return None
    return made


def _uri(value: Any) -> Any:
    return value.get("uri") if isinstance(value, dict) else None


def count(event: dict[str, Any], tally: trends.Tally) -> None:
    """Count what an event says for the Trending page (trends.py): a new
    post's links and hashtags, and the post it replies to (its thread's
    first) or quotes; a like of a recent post."""
    made = _created(event, POST)
    if made is not None:
        record = made[1]
        root = _uri(record["reply"].get("root")) if isinstance(record.get("reply"), dict) else None
        if (at := _recent(root)) is not None:
            tally.reply(root, at)
        quoted = trends.bluesky_quoted(record)
        if (at := _recent(quoted)) is not None:
            tally.quote(quoted, at)  # type: ignore[arg-type]
        tally.post_links(trends.bluesky_links(record), event.get("did"))
        tally.post_tags(record_tags(record), event.get("did"))
        return
    liked = _created(event, LIKE)
    if liked is not None:
        subject = _uri(liked[1].get("subject"))
        if (at := _recent(subject)) is not None:
            tally.like(subject, at)


class BlueskyStream:
    def __init__(self, bouncer: Bouncer, url: str, user_agent: str):
        self.bouncer, self.db, self.url, self.user_agent = bouncer, bouncer.db, url, user_agent
        self.wake = threading.Event()  # the hashtags followed, or what's counted, changed
        self.tally = trends.Tally("bluesky")
        self._counted_to = 0  # the time (microseconds) of the last event counted: one read again isn't
        self._stop = threading.Event()
        self._noted: dict[str, str] = {}  # at:// address -> hashtag, not yet asked for
        # What the community page says of it: connected since when, and the last trouble.
        self.connected_since: str | None = None
        self.last_error: str | None = None
        self.last_error_at: str | None = None
        self.dictionary: Any = load_dictionary()  # None: events are read uncompressed
        self._bad = 0  # events in a row that wouldn't decompress
        bouncer.follow_hooks.append(self._changed)
        bouncer.unfollow_hooks.append(self._changed)
        bouncer.job_handlers[JOB] = self.capture

    def _changed(self, community_id: int) -> None:
        self.wake.set()

    def followed(self) -> set[str]:
        with self.db.connect() as conn:
            return {r["name"] for r in conn.execute(FOLLOWED_SQL)}

    def wanted(self) -> tuple[set[str], bool, bool]:
        """(the hashtags followed, whether what's posted is counted, whether likes are too)."""
        chosen = trends.settings(self.db)
        counting = bool(chosen["bluesky"])
        return self.followed(), counting, counting and chosen["bluesky_likes"] == "stream"

    def listening(self) -> bool:
        """Whether the stream is wanted at all (for pages saying so)."""
        tags, counting, _ = self.wanted()
        return bool(tags) or counting

    # -- listening -----------------------------------------------------------------
    def run_forever(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            tags, counting, likes = self.wanted()
            if not tags and not counting:
                self.wake.wait(TAGS_EVERY)
                self.wake.clear()
                continue
            try:
                self._listen(tags, counting, likes)
                backoff = 1.0
                continue
            except (OSError, WebSocketException) as exc:
                log.warning("Jetstream: %s; trying again in %.0f s", exc, backoff)
                self._trouble(str(exc) or type(exc).__name__)
            except Exception as exc:  # keep listening
                log.error("Jetstream crashed: %s", traceback.format_exc())
                self._trouble(f"{type(exc).__name__}: {exc}")
            self._stop.wait(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF)

    def _trouble(self, error: str) -> None:
        self.last_error, self.last_error_at = error[:300], utcnow()

    def _listen(self, tags: set[str], counting: bool = False, likes: bool = False) -> None:
        """Read the stream until it's no longer wanted, what's wanted of it
        changes (likes, or not), or ThreadBNC stops."""
        params = [("wantedCollections", POST)] + ([("wantedCollections", LIKE)] if likes else [])
        compressed = self.dictionary is not None
        if compressed:
            params.append(("compress", "true"))
        cursor = self._resume_from()
        if cursor:
            params.append(("cursor", str(cursor)))
        last = None
        with connect(f"{self.url}?{urlencode(params)}", user_agent_header=self.user_agent, open_timeout=20,
                     max_size=2 ** 20) as ws:
            self.connected_since, self.last_error, self._bad = utcnow(), None, 0
            log.info("Jetstream: listening for %d hashtags%s%s%s", len(tags), ", counting" if counting else "",
                     " with likes" if likes else "", ", compressed" if compressed else "")
            flushed = saved = refreshed = time.monotonic()
            host, messages, received = urlparse(self.url).hostname, 0, 0  # counted (traffic.py) at each flush
            try:
                while not self._stop.is_set():
                    try:
                        raw = ws.recv(timeout=1.0)
                    except TimeoutError:
                        raw = None
                    if raw is not None:
                        messages, received = messages + 1, received + len(raw)
                    text = self._decode(raw) if raw is not None else None
                    if compressed and self.dictionary is None:
                        return  # the dictionary stopped fitting: connect again, uncompressed
                    if text is not None:
                        last = text
                        self._take(text, tags, counting)
                    now = time.monotonic()
                    if now - flushed >= FLUSH_EVERY:
                        self._flush()
                        record("in", host, requests=messages, bytes_in=received)
                        flushed, messages, received = now, 0, 0
                    if last is not None and now - saved >= SAVE_EVERY:
                        self._save(last)
                        saved = now
                    if self.wake.is_set() or now - refreshed >= TAGS_EVERY:
                        self.wake.clear()
                        (tags, counting, now_likes), refreshed = self.wanted(), now
                        if (not tags and not counting) or now_likes != likes:
                            return
            finally:
                self.connected_since = None
                self._flush()
                record("in", host, requests=messages, bytes_in=received)
                if last is not None:
                    self._save(last)

    def _take(self, text: str, tags: set[str], counting: bool) -> None:
        """One event: noted if it's a post with a followed hashtag, and counted."""
        if not counting and "#tag" not in text and '"tags"' not in text:  # most have no hashtag: not worth reading
            return
        event = _event(text)
        if event is None:
            return
        hit = tagged(event, tags)
        if hit:
            self._noted[hit[0]] = hit[1]
        if counting:
            when = event.get("time_us")
            if isinstance(when, int):
                if when <= self._counted_to:  # read again, after connecting again
                    return
                self._counted_to = when
            count(event, self.tally)

    def _decode(self, raw: str | bytes) -> str | None:
        """An event's JSON: sent as text uncompressed, or as a zstd frame
        compressed. None for one that won't decompress; after BAD_FRAMES in
        a row, the dictionary is given up on."""
        if isinstance(raw, str):
            return raw
        if self.dictionary is None:
            return raw.decode("utf-8", "replace")
        try:
            decompressor = zstd.ZstdDecompressor(zstd_dict=self.dictionary)
            data = decompressor.decompress(raw, max_length=MAX_EVENT)
            if not decompressor.eof:
                raise zstd.ZstdError(f"an event over {MAX_EVENT} bytes")
        except zstd.ZstdError as exc:
            self._bad += 1
            if self._bad >= BAD_FRAMES:
                log.warning("Jetstream: its events no longer decompress with ThreadBNC's copy of its dictionary "
                            "(%s), so they're read uncompressed from now on", exc)
                self.dictionary = None
            return None
        self._bad = 0
        return data.decode("utf-8", "replace")

    def _resume_from(self) -> int | None:
        saved = self.db.get_setting(CURSOR)
        if not saved or not saved.isdigit():
            return None
        return int(saved) if time.time() * 1_000_000 - int(saved) < RESUME_WITHIN else None

    def _save(self, raw: str | bytes) -> None:
        m = _TIME_US.search(raw if isinstance(raw, str) else raw.decode("utf-8", "replace"))
        if not m:
            return
        with self.db.transaction(exclusive=False) as conn:
            conn.execute("INSERT INTO app_settings(key, value) VALUES (?, ?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (CURSOR, m.group(1)))

    def _flush(self) -> None:
        """Ask for the posts noted, GET_POSTS to a job, and write what was counted."""
        self.tally.flush(self.db)
        noted, self._noted = list(self._noted.items()), {}
        for i in range(0, len(noted), GET_POSTS):
            self.bouncer.enqueue(JOB, {"posts": dict(noted[i:i + GET_POSTS])}, delay=FIRST_TRY)

    def start_thread(self) -> threading.Thread:
        t = threading.Thread(target=self.run_forever, name="jetstream", daemon=True)
        t.start()
        return t

    def stop(self) -> None:
        self._stop.set()
        self.wake.set()

    # -- capturing -----------------------------------------------------------------
    def capture(self, payload: dict[str, Any]) -> dict[str, Any]:
        """A job: read the posts noted from the AppView and capture each into
        its hashtag's feed, unless it's older than the follow."""
        with self.db.connect() as conn:
            followed = {r["name"]: parse_ts(r["capture_since"]) for r in conn.execute(FOLLOWED_SQL)}
        wanted = {uri: tag for uri, tag in payload["posts"].items()
                  if tag in followed and not self.bouncer._existing_thread(web_url(uri))}
        if not wanted:
            return {"captured": 0}
        adapter = self.bouncer.bluesky_adapter
        posts = adapter.tagged_posts(wanted)
        captured = 0
        for post in posts:
            since, created = followed[post.community.name], parse_ts(post.created_at)
            if (since and created and created < since) or self.bouncer.hidden(post):
                continue
            self.bouncer._ingest_post(post, BSKY_DOMAIN, post.local_id, adapter, capture=True,
                                      source_url=post.ap_id, retention="auto")
            captured += 1
        found = {p.ap_id for p in posts}
        missing = {uri: tag for uri, tag in wanted.items() if web_url(uri) not in found}
        if missing and not payload.get("again"):
            self.bouncer.enqueue(JOB, {"posts": missing, "again": True}, delay=RETRY_AFTER)
        return {"captured": captured, "missing": len(missing)}
