"""Following hashtags on Bluesky, through its Jetstream.

Bluesky can't follow a hashtag either, and nothing on it can be subscribed to
for one: everything posted anywhere on Bluesky goes out on one public stream
instead. Jetstream (https://github.com/bluesky-social/jetstream) serves that
stream as JSON over a WebSocket, and can be asked for new posts only (no
likes, follows or reposts).

So while at least one hashtag is followed, ThreadBNC keeps one connection
open to Jetstream and reads every post made on Bluesky as it's made (about 30
a second in September 2026: about 1 GB a day compressed), keeping only those
with a followed hashtag:

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
followed, the connection is closed."""

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
from urllib.parse import urlencode

from websockets.exceptions import WebSocketException
from websockets.sync.client import connect

from .adapters import BSKY_DOMAIN
from .adapters.bluesky import GET_POSTS, POST, record_tags, web_url
from .bouncer import Bouncer
from .db import parse_ts, utcnow

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
TAGS_EVERY = 60.0  # the followed hashtags are read again at least this often
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


def match(raw: str | bytes, tags: set[str]) -> tuple[str, str] | None:
    """(at:// address, hashtag) when an event from the stream is a new post,
    not a reply, with one of `tags`; else None."""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    if "#tag" not in raw and '"tags"' not in raw:  # most posts have no hashtag: not worth reading
        return None
    try:
        event = json.loads(raw)
    except ValueError:
        return None
    commit = event.get("commit") if isinstance(event, dict) else None
    if not isinstance(commit, dict) or commit.get("operation") != "create" or commit.get("collection") != POST:
        return None
    record = commit.get("record")
    if not isinstance(record, dict) or record.get("reply") or not event.get("did") or not commit.get("rkey"):
        return None
    tag = next((t for t in record_tags(record) if t in tags), None)
    return (f"at://{event['did']}/{POST}/{commit['rkey']}", tag) if tag else None


class BlueskyTags:
    def __init__(self, bouncer: Bouncer, url: str, user_agent: str):
        self.bouncer, self.db, self.url, self.user_agent = bouncer, bouncer.db, url, user_agent
        self.wake = threading.Event()  # the hashtags followed changed
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

    # -- listening -----------------------------------------------------------------
    def run_forever(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            tags = self.followed()
            if not tags:
                self.wake.wait(TAGS_EVERY)
                self.wake.clear()
                continue
            try:
                self._listen(tags)
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

    def _listen(self, tags: set[str]) -> None:
        """Read the stream until no hashtag is followed any more, or ThreadBNC stops."""
        params = [("wantedCollections", POST)]
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
            log.info("Jetstream: listening for %d hashtags%s", len(tags), ", compressed" if compressed else "")
            flushed = saved = refreshed = time.monotonic()
            try:
                while not self._stop.is_set():
                    try:
                        raw = ws.recv(timeout=1.0)
                    except TimeoutError:
                        raw = None
                    text = self._decode(raw) if raw is not None else None
                    if compressed and self.dictionary is None:
                        return  # the dictionary stopped fitting: connect again, uncompressed
                    if text is not None:
                        last = text
                        hit = match(text, tags)
                        if hit:
                            self._noted[hit[0]] = hit[1]
                    now = time.monotonic()
                    if now - flushed >= FLUSH_EVERY:
                        self._flush()
                        flushed = now
                    if last is not None and now - saved >= SAVE_EVERY:
                        self._save(last)
                        saved = now
                    if self.wake.is_set() or now - refreshed >= TAGS_EVERY:
                        self.wake.clear()
                        tags, refreshed = self.followed(), now
                        if not tags:
                            return
            finally:
                self.connected_since = None
                self._flush()
                if last is not None:
                    self._save(last)

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
        with self.db.transaction() as conn:
            conn.execute("INSERT INTO app_settings(key, value) VALUES (?, ?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (CURSOR, m.group(1)))

    def _flush(self) -> None:
        """Ask for the posts noted, GET_POSTS to a job."""
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
            if since and created and created < since:
                continue
            self.bouncer._ingest_post(post, BSKY_DOMAIN, post.local_id, adapter, capture=True,
                                      source_url=post.ap_id, retention="auto")
            captured += 1
        found = {p.ap_id for p in posts}
        missing = {uri: tag for uri, tag in wanted.items() if web_url(uri) not in found}
        if missing and not payload.get("again"):
            self.bouncer.enqueue(JOB, {"posts": missing, "again": True}, delay=RETRY_AFTER)
        return {"captured": captured, "missing": len(missing)}
