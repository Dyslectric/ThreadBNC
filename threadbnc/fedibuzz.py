"""FediBuzz's firehose, counted for Trending.

FediBuzz (fedi.buzz, which also runs the hashtag relays tags.py follows)
gathers what's posted publicly across the fediverse and serves it as one
stream, the way Mastodon serves its public timeline: server-sent events at
https://fedi.buzz/api/v1/streaming/public, to anyone, no account needed. It
asks that anyone wanting that much take it from there rather than follow
thousands of its relays. About 8 posts a second arrive, some 3 GB a day, so
it's only listened to once it's turned on on the Trending page.

Each post's hashtags and links are counted as Mastodon's (trends.py), in the
same Tally as your Mastodon server's public timeline: an author counts once
an hour for each hashtag or link, so a post that arrives both ways counts
once. Replies aren't counted: a post's id there is its own server's, not one
your server can be asked about. Nothing is captured or kept, and posts made
while it's down are missed (Mastodon's streams can't carry on from where
they left off)."""

from __future__ import annotations

import contextlib
import json
import logging
import threading
import time
import traceback
from typing import Any, Callable, ContextManager, Iterable, Iterator
from urllib.parse import urlparse

import httpx

from . import trends
from .db import utcnow
from .mastodon_stream import status_links, status_tags
from .traffic import record

log = logging.getLogger(__name__)

URL = "https://fedi.buzz/api/v1/streaming/public"
FLUSH_EVERY = 5.0
REFRESH_EVERY = 60.0  # seconds: whether it's still wanted is read again this often
READ_TIMEOUT = 90.0  # seconds without a byte (it sends a comment now and then) before connecting again
MAX_BACKOFF = 300.0


def events(chunks: Iterable[bytes]) -> Iterator[tuple[str, str]]:
    """Server-sent events, (name, data), from a stream's bytes. A line ends
    only at a newline: a post's text can hold other line separators."""
    buf, event, data = b"", "message", []
    for chunk in chunks:
        buf += chunk
        *lines, buf = buf.split(b"\n")
        for raw in lines:
            line = raw.rstrip(b"\r").decode("utf-8", "replace")
            if not line:  # the end of an event
                if data:
                    yield event, "\n".join(data)
                event, data = "message", []
            elif not line.startswith(":"):  # (a comment: it's still there)
                field, _, value = line.partition(":")
                value = value[1:] if value.startswith(" ") else value
                if field == "event":
                    event = value
                elif field == "data":
                    data.append(value)


class FediBuzzStream:
    def __init__(self, bouncer: Any, tally: trends.Tally, user_agent: str, url: str = URL,
                 open_stream: Callable[[], ContextManager[Iterable[bytes]]] | None = None):
        """`tally`: your Mastodon server's timeline's (mastodon_stream.py), so
        what arrives both ways counts once. `open_stream` (tests): () -> a
        context manager giving the stream's bytes."""
        self.db, self.tally, self.user_agent, self.url = bouncer.db, tally, user_agent, url
        self.open_stream = open_stream or self._open
        self.wake = threading.Event()  # turned on or off
        self._stop = threading.Event()
        # What the Trending and Traffic pages say of it.
        self.connected_since: str | None = None
        self.last_error: str | None = None
        self.last_error_at: str | None = None

    def wanted(self) -> bool:
        return bool(trends.settings(self.db)["fedibuzz"])

    def run_forever(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            if not self.wanted():
                self.wake.wait(REFRESH_EVERY)
                self.wake.clear()
                continue
            try:
                self._listen()
                backoff = 1.0
                continue
            except (httpx.HTTPError, OSError) as exc:
                log.warning("FediBuzz stream: %s; trying again in %.0f s", exc, backoff)
                self._trouble(str(exc) or type(exc).__name__)
            except Exception as exc:  # keep listening
                log.error("FediBuzz stream crashed: %s", traceback.format_exc())
                self._trouble(f"{type(exc).__name__}: {exc}")
            self._stop.wait(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF)

    def _trouble(self, error: str) -> None:
        self.last_error, self.last_error_at = error[:300], utcnow()

    @contextlib.contextmanager
    def _open(self) -> Iterator[Iterable[bytes]]:
        timeout = httpx.Timeout(20.0, read=READ_TIMEOUT)
        with httpx.Client(timeout=timeout, headers={"User-Agent": self.user_agent}) as client:
            with client.stream("GET", self.url, headers={"Accept": "text/event-stream"}) as response:
                response.raise_for_status()
                yield response.iter_bytes()

    def _listen(self) -> None:
        host = urlparse(self.url).hostname
        received = [0]

        def counted(chunks: Iterable[bytes]) -> Iterator[bytes]:
            for chunk in chunks:
                received[0] += len(chunk)
                yield chunk

        with self.open_stream() as chunks:
            self.connected_since, self.last_error = utcnow(), None
            log.info("FediBuzz stream: counting from %s", self.url)
            flushed = refreshed = time.monotonic()
            messages = 0
            try:
                for event, data in events(counted(chunks)):
                    messages += 1
                    if event == "update":
                        self.take(data)
                    now = time.monotonic()
                    if now - flushed >= FLUSH_EVERY:
                        self.tally.flush(self.db)
                        record("in", host, requests=messages, bytes_in=received[0])
                        flushed, messages, received[0] = now, 0, 0
                    if self.wake.is_set() or now - refreshed >= REFRESH_EVERY:
                        self.wake.clear()
                        refreshed = now
                        if self._stop.is_set() or not self.wanted():
                            return
                if not self._stop.is_set():
                    raise ConnectionError("FediBuzz ended the stream")
            finally:
                self.connected_since = None
                self.tally.flush(self.db)
                record("in", host, requests=messages, bytes_in=received[0])

    def take(self, data: str) -> None:
        """One post from the stream: its hashtags and links counted."""
        try:
            status = json.loads(data)
        except ValueError:
            return
        if not isinstance(status, dict) or status.get("reblog") or not status.get("uri"):
            return
        account = status.get("account") if isinstance(status.get("account"), dict) else {}
        author = account.get("url") or account.get("uri") or account.get("acct")
        self.tally.post_links(status_links(status), author)
        self.tally.post_tags(status_tags(status), author)

    def start_thread(self) -> threading.Thread:
        t = threading.Thread(target=self.run_forever, name="fedibuzz-stream", daemon=True)
        t.start()
        return t

    def stop(self) -> None:
        self._stop.set()
        self.wake.set()
