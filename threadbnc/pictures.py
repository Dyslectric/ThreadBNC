"""Trending posts' pictures, downloaded the moment a page asks for them.

A page (app.js) asks for the pictures of the posts on screen, or under the pointer. They're fetched
here, on threads of their own, so nothing the bouncer is busy with (a feed poll, votes, other
downloads) is ahead of them, and the request made last is looked at first: the page in front of you
comes before what you scrolled past. Each picture that arrives is announced to every page listening
(web.py: /trending/pictures/stream), which shows it at once; nothing asks again.

Downloads are spaced FAST_INTERVAL apart per host (media.py), so a few threads keep that full."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from typing import Any, Callable

from . import trends
from .adapters.base import RemoteError
from .db import Database, utcnow

log = logging.getLogger("threadbnc.pictures")

WORKERS = 4


class PictureDesk:
    """`media`: the bouncer's MediaFetcher. `refresh(source, ref, now)`: read a post again from its
    site (it was last read before its pictures' addresses were kept)."""

    def __init__(self, db: Database, media: Any, refresh: Callable[[str, str, str], None], workers: int = WORKERS):
        self.db, self.media, self.refresh, self.workers = db, media, refresh, workers
        self._posts: list[tuple[str, str]] = []  # the last asked for is at the end, and taken first
        self._cv = threading.Condition()
        self._busy: set[int] = set()  # media being downloaded
        self._threads: list[threading.Thread] = []
        self._listeners: list[tuple[asyncio.AbstractEventLoop, asyncio.Queue]] = []
        self._lock = threading.Lock()

    # -- asking ------------------------------------------------------------------------
    def request(self, posts: list[tuple[str, str]]) -> None:
        """These posts' pictures are wanted now, ahead of every post asked about before (the first of
        `posts` first of them)."""
        if not posts:
            return
        with self._cv:
            if not self._threads:
                self._threads = [threading.Thread(target=self._work, name=f"pictures-{n}", daemon=True)
                                 for n in range(self.workers)]
                for t in self._threads:
                    t.start()
            for pair in reversed(posts):
                if pair in self._posts:
                    self._posts.remove(pair)
                self._posts.append(pair)
            self._cv.notify(len(posts))

    def _work(self) -> None:
        while True:
            with self._cv:
                while not self._posts:
                    self._cv.wait()
                source, ref = self._posts.pop()
            try:
                self._fetch(source, ref)
            except Exception:
                log.exception("fetching the pictures of %s %s", source, ref)

    def _fetch(self, source: str, ref: str) -> None:
        now = utcnow()
        with self.db.connect() as conn:
            row = conn.execute("SELECT view_json FROM stream_posts WHERE source=? AND ref=?", (source, ref)).fetchone()
        if not row or not row["view_json"]:
            return
        if trends.unread_pictures(json.loads(row["view_json"])):
            try:
                self.refresh(source, ref, now)
            except RemoteError as exc:  # it's shown without, this time
                log.info("reading %s %s again for its pictures: %s", source, ref, exc)
        with self.db.transaction() as conn:
            trends.want_pictures(conn, source, ref, now)
        with self.db.connect() as conn:
            pending = conn.execute(
                "SELECT m.* FROM stream_post_media s JOIN media m ON m.id=s.media_id WHERE s.source=? AND s.ref=? "
                "AND s.position >= 0 AND m.status='pending' AND (m.next_attempt_at IS NULL OR m.next_attempt_at<=?) "
                "ORDER BY s.position", (source, ref, now)).fetchall()
        for m in pending:
            with self._lock:
                if m["id"] in self._busy:  # another post's, or another thread has it
                    continue
                self._busy.add(m["id"])
            try:
                self.media.fetch_one(m)
            finally:
                with self._lock:
                    self._busy.discard(m["id"])
            self.announce(source, ref)

    # -- telling pages -------------------------------------------------------------------
    def listen(self) -> asyncio.Queue:
        """A queue of (source, ref) for each post that has a picture newly downloaded. Call from the
        event loop; give it back with `unlisten`."""
        queue: asyncio.Queue = asyncio.Queue()
        with self._lock:
            self._listeners.append((asyncio.get_running_loop(), queue))
        return queue

    def unlisten(self, queue: asyncio.Queue) -> None:
        with self._lock:
            self._listeners = [(loop, q) for loop, q in self._listeners if q is not queue]

    def announce(self, source: str, ref: str) -> None:
        with self._lock:
            listeners = list(self._listeners)
        for loop, queue in listeners:
            try:
                loop.call_soon_threadsafe(queue.put_nowait, (source, ref))
            except RuntimeError:  # its loop has closed
                self.unlisten(queue)
