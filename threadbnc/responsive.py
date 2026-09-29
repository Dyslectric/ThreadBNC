"""Making pages feel quick: Server-Timing (timing.py), revalidation, compression, prefetching.

- Every response says where its time went (Server-Timing), and slow ones are logged.
- A page (HTML, 200, GET) is sent with an ETag and `private, no-cache`, instead
  of `no-store`: the browser keeps it, so going back to a page is instant, and
  asking for it again is answered 304 (no body) when it hasn't changed.
- Text (pages, styles, scripts, JSON) is gzipped for browsers that take it.
  Nothing else is: pictures and video are compressed already, and a partial
  answer (Range) mustn't be changed.
- Pages that only read (PREFETCHABLE) can be fetched ahead, as a link is
  pointed at (app.js): that answer is kept for PREFETCH_FRESH seconds, so the
  click that follows needs no request. Prefetching anything else is refused
  (204), so a page that marks things as read when it's opened never is by a
  prefetch."""

from __future__ import annotations

import gzip
import hashlib
import re
import time
from typing import Any

from . import timing

PREFETCH_HEADER = "x-threadbnc-prefetch"
PREFETCH_FRESH = 20  # seconds a prefetched page is used without asking again
PREFETCHABLE = re.compile(r"^/(trending(/(loops|articles|stories|tags))?|articles|communities)?$")
PAGE_CACHE = "private, no-cache"
TEXT = ("text/html", "text/css", "text/javascript", "application/javascript", "application/json", "image/svg+xml")
MIN_GZIP = 1000  # bytes: smaller isn't worth it


def _weak(tag: str) -> str:
    return tag.strip().removeprefix("W/")


class Responsive:
    def __init__(self, app: Any):
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        method, path = scope["method"], scope["path"]
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        prefetch = PREFETCH_HEADER in headers
        if prefetch and not (method == "GET" and PREFETCHABLE.match(path)):
            await send({"type": "http.response.start", "status": 204, "headers": []})
            await send({"type": "http.response.body", "body": b""})
            return
        started = time.perf_counter()
        counters = timing.begin()
        wanted = [_weak(w) for w in headers.get("if-none-match", "").split(",") if w.strip()]
        gzips = "gzip" in headers.get("accept-encoding", "") and "range" not in headers
        held: dict[str, Any] = {"start": None, "chunks": [], "page": False}

        def finish(head: dict[str, Any]) -> dict[str, Any]:
            """`head` with how long the request took so far (its time to first byte, as the headers go out)."""
            total = time.perf_counter() - started
            timing.log_slow(method, path, head["status"], counters, total)
            return {**head, "headers": [*head["headers"], (b"server-timing", timing.header(counters, total).encode())]}

        async def wrapped(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                given = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in message["headers"]}
                kind = given.get("content-type", "").split(";")[0].strip()
                if method == "GET" and message["status"] == 200 and kind in TEXT and "content-encoding" not in given:
                    held.update(start=message, page=kind == "text/html")  # held until its body is here
                    return
                await send(finish(message))
            elif held["start"] is not None:
                held["chunks"].append(message.get("body", b""))
                if message.get("more_body"):
                    return
                body, head = b"".join(held["chunks"]), held["start"]
                drop = {b"content-length"}
                extra: list[tuple[bytes, bytes]] = []
                if held["page"]:
                    etag = '"' + hashlib.blake2b(body, digest_size=12).hexdigest() + '"'
                    cache = f"private, max-age={PREFETCH_FRESH}" if prefetch else PAGE_CACHE
                    drop |= {b"cache-control", b"etag"}
                    extra += [(b"cache-control", cache.encode()), (b"etag", etag.encode())]
                    if etag in wanted:
                        kept = [(k, v) for k, v in head["headers"] if k.lower() not in drop | {b"content-type"}]
                        await send(finish({"type": "http.response.start", "status": 304, "headers": kept + extra}))
                        await send({"type": "http.response.body", "body": b""})
                        return
                if gzips and len(body) >= MIN_GZIP:
                    body = gzip.compress(body, compresslevel=5, mtime=0)
                    drop.add(b"content-encoding")
                    extra += [(b"content-encoding", b"gzip"), (b"vary", b"Accept-Encoding")]
                kept = [(k, v) for k, v in head["headers"] if k.lower() not in drop]
                head = {"type": "http.response.start", "status": 200,
                        "headers": [*kept, *extra, (b"content-length", str(len(body)).encode())]}
                await send(finish(head))
                await send({"type": "http.response.body", "body": body})
            else:
                await send(message)

        await self.app(scope, receive, wrapped)
