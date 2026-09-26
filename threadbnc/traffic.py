"""What ThreadBNC asks of other sites, and what the streams it listens to send it.

Counted by the hour, in two directions:

- out: requests ThreadBNC makes. Every HTTP client it has is metered (its
  transport counts each request and the bytes each way: bodies as they
  cross the wire, so compressed where the server compressed them, plus
  headers), and so is yt-dlp (every request it makes goes through its
  urlopen). Answers that were errors are counted, and 429s ("too many
  requests") on their own, as the plainest sign of asking too much.
- in: what's pushed to ThreadBNC without it asking each time: the Jetstream's
  messages (jetstream.py), a relay's deliveries of hashtag posts (actor.py)
  and deliveries to your own servers relayed through it (federation.py).

Each count carries the host, the community it was for when there was one
(the one being checked, or the post being opened's) and why it was made
(`tagged`). Counts are kept in memory and added to the `traffic` table about
once a minute by whichever process made them, so the web app and a bouncer
running on its own add to the same rows. Hours older than KEEP_DAYS are
dropped."""

from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

import httpx

log = logging.getLogger(__name__)

HOUR = "%Y-%m-%dT%H"  # UTC
FLUSH_EVERY = 60.0  # seconds between writes of what was counted
KEEP_DAYS = 30
FIELDS = ("requests", "bytes_in", "bytes_out", "errors", "slowed")

# Why a request was made, as the Traffic page says it. Anything not tagged is "".
PURPOSES = {
    "polling": "Checking followed communities",
    "opening": "Opening posts: comments, votes and their article",
    "votes": "Updating votes on recent posts",
    "media": "Saving pictures, videos and audio",
    "articles": "Saving linked articles",
    "previews": "Link previews",
    "discussions": "Looking for discussions of articles",
    "saving": "Saving posts you added",
    "hashtags": "Reading posts that hashtags bring",
    "trends": "Trending: likes and replies of posts talked about, most posted articles",
    "upkeep": "Inboxes, follows and other upkeep",
    "browsing": "While you browse: lookups, posting, voting",
    "": "Other",
}

# Hosts grouped under the service they belong to; any other host is grouped by
# its registrable domain (lemmy.world, i.imgur.com -> imgur.com).
SERVICES = (
    ("Reddit", ("reddit.com", "redd.it", "redditmedia.com", "redditstatic.com")),
    ("YouTube", ("youtube.com", "youtu.be", "googlevideo.com", "ytimg.com", "youtube-nocookie.com", "ggpht.com")),
    ("Bluesky", ("bsky.app", "bsky.network", "bsky.social", "bsky.chat")),
    ("Vimeo", ("vimeo.com", "vimeocdn.com")),
    ("Twitch", ("twitch.tv", "jtvnw.net")),
    ("FediBuzz", ("fedi.buzz",)),
)
_SECOND_LEVEL = {"co", "com", "org", "net", "ac", "gov", "edu", "ne", "or"}  # example.co.uk

_purpose: ContextVar[str] = ContextVar("traffic_purpose", default="")
# A community's id, or its ActivityPub id or its relay's (resolved when counts are written).
_community: ContextVar[int | str | None] = ContextVar("traffic_community", default=None)

Key = tuple[str, str, str, "int | str | None", str]  # hour, direction, host, community, purpose


@contextmanager
def tagged(purpose: str | None = None, community: int | str | None = None) -> Iterator[None]:
    """Requests made inside are counted as for `purpose` and `community`
    (either left as it was when None)."""
    tokens = []
    if purpose is not None:
        tokens.append((_purpose, _purpose.set(purpose)))
    if community is not None:
        tokens.append((_community, _community.set(community)))
    try:
        yield
    finally:
        for var, token in reversed(tokens):
            var.reset(token)


class Meter:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counts: dict[Key, list[int]] = {}
        self.db: Any = None
        self._thread: threading.Thread | None = None
        self._pruned = 0.0

    def record(self, direction: str, host: str | None, *, requests: int = 1, bytes_in: int = 0, bytes_out: int = 0,
               errors: int = 0, slowed: int = 0, community: int | str | None = None,
               purpose: str | None = None) -> None:
        key = (time.strftime(HOUR, time.gmtime()), direction, (host or "?").lower(),
               community if community is not None else _community.get(),
               purpose if purpose is not None else _purpose.get())
        with self._lock:
            counts = self._counts.get(key)
            if counts is None:
                counts = self._counts[key] = [0, 0, 0, 0, 0]
            counts[0] += requests
            counts[1] += bytes_in
            counts[2] += bytes_out
            counts[3] += errors
            counts[4] += slowed

    def attach(self, db: Any) -> None:
        """Write counts to `db` from now on, about once a minute."""
        self.db = db
        with self._lock:
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="traffic", daemon=True)
                self._thread.start()

    def _run(self) -> None:
        while True:
            time.sleep(FLUSH_EVERY)
            self.flush()

    def flush(self, db: Any = None) -> None:
        """Add what's been counted to the `traffic` table."""
        db = db or self.db
        if db is None:
            return
        with self._lock:
            counts, self._counts = self._counts, {}
        if not counts:
            return
        try:
            with db.transaction() as conn:
                ids: dict[str, int] = {}
                for ap_id in {k[3] for k in counts if isinstance(k[3], str)}:
                    row = (conn.execute("SELECT id FROM communities WHERE canonical_ap_id=?", (ap_id,)).fetchone()
                           or conn.execute("SELECT community_id AS id FROM community_follows WHERE push_actor=?",
                                           (ap_id,)).fetchone())
                    ids[ap_id] = row["id"] if row else 0
                merged: dict[tuple[str, str, str, int, str], list[int]] = {}
                for (hour, direction, host, community, purpose), c in counts.items():
                    cid = ids[community] if isinstance(community, str) else int(community or 0)
                    total = merged.setdefault((hour, direction, host, cid, purpose), [0, 0, 0, 0, 0])
                    for i, n in enumerate(c):
                        total[i] += n
                conn.executemany(
                    "INSERT INTO traffic(hour, direction, host, community_id, purpose, requests, bytes_in, bytes_out, "
                    "errors, slowed) VALUES (?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(hour, direction, host, community_id, purpose) DO UPDATE SET "
                    + ", ".join(f"{f}=traffic.{f}+excluded.{f}" for f in FIELDS),
                    [(*k, *c) for k, c in merged.items()])
                if time.monotonic() - self._pruned > 3600:
                    conn.execute("DELETE FROM traffic WHERE hour<?", (_hour_ago(KEEP_DAYS * 24),))
                    self._pruned = time.monotonic()
        except Exception as exc:  # counts are nice to have: never worth breaking anything over
            log.warning("couldn't save traffic counts: %s", exc)


METER = Meter()
record = METER.record


def _hour_ago(hours: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime(HOUR)


# -- counting HTTP ------------------------------------------------------------------

def _headers_size(headers: Any) -> int:
    return sum(len(k) + len(v) + 4 for k, v in headers.raw)


class _Counted(httpx.SyncByteStream):
    """A response body that counts itself as it's read, and says how much when closed."""

    def __init__(self, stream: Any, done: Callable[[int], None]):
        self.stream, self.done, self.read, self.finished = stream, done, 0, False

    def __iter__(self) -> Iterator[bytes]:
        for chunk in self.stream:
            self.read += len(chunk)
            yield chunk

    def close(self) -> None:
        try:
            close = getattr(self.stream, "close", None)
            if close:
                close()
        finally:
            if not self.finished:
                self.finished = True
                self.done(self.read)


class MeteredTransport(httpx.BaseTransport):
    """An httpx transport counting each request through it (see the top)."""

    def __init__(self, inner: httpx.BaseTransport | None = None):
        self.inner = inner or httpx.HTTPTransport()

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        community, purpose = _community.get(), _purpose.get()
        sent = (len(request.method) + len(request.url.raw_path) + 12 + _headers_size(request.headers)
                + int(request.headers.get("content-length") or 0))
        try:
            response = self.inner.handle_request(request)
        except Exception:
            record("out", host, bytes_out=sent, errors=1, community=community, purpose=purpose)
            raise
        status, head = response.status_code, 17 + _headers_size(response.headers)

        def done(body: int) -> None:
            record("out", host, bytes_in=head + body, bytes_out=sent, errors=int(status >= 400),
                   slowed=int(status == 429), community=community, purpose=purpose)

        if hasattr(response, "_content"):  # already read (made from bytes, as in tests): never streamed
            done(len(response.content))
        else:
            response.stream = _Counted(response.stream, done)
        return response

    def close(self) -> None:
        self.inner.close()


def metered(transport: httpx.BaseTransport | None = None) -> MeteredTransport:
    """`transport` (or httpx's own), counted."""
    return transport if isinstance(transport, MeteredTransport) else MeteredTransport(transport)


def meter_ydl(ydl: Any) -> Any:
    """A YoutubeDL whose requests are counted: extracting a video's details
    and downloading it both go through its urlopen. (A download handed to
    ffmpeg, as some live recordings are, isn't counted.)"""
    urlopen = getattr(ydl, "urlopen", None)
    if urlopen is None:  # not a real YoutubeDL (tests)
        return ydl
    community, purpose = _community.get(), _purpose.get()

    def counted(req: Any) -> Any:
        url = req if isinstance(req, str) else getattr(req, "url", "")
        try:
            resp = urlopen(req)
        except Exception:
            record("out", urlparse(url).hostname, errors=1, community=community, purpose=purpose)
            raise
        host = urlparse(getattr(resp, "url", None) or url).hostname
        status = int(getattr(resp, "status", 200) or 200)
        record("out", host, errors=int(status >= 400), slowed=int(status == 429), community=community,
               purpose=purpose)
        read = resp.read

        def counted_read(*args: Any, **kwargs: Any) -> bytes:
            data = read(*args, **kwargs)
            if data:
                record("out", host, requests=0, bytes_in=len(data), community=community, purpose=purpose)
            return data

        resp.read = counted_read
        return resp

    ydl.urlopen = counted
    return ydl


def delivered(activity: Any, size: int) -> None:
    """Count an ActivityPub delivery of `size` bytes, as from its actor's
    host (as it says) and for the community it is, or whose relay it is."""
    actor = activity.get("actor") if isinstance(activity, dict) else None
    actor = actor[0] if isinstance(actor, list) and actor else actor
    actor = actor.get("id") if isinstance(actor, dict) else actor
    actor = actor if isinstance(actor, str) else None
    record("in", urlparse(actor).hostname if actor else None, bytes_in=size, community=actor or 0, purpose="")


# -- the Traffic page ---------------------------------------------------------------

def service(host: str) -> str:
    """The service a host belongs to: Reddit, YouTube, ... or its domain."""
    host = host.lower().removeprefix("www.")
    for name, suffixes in SERVICES:
        if any(host == s or host.endswith("." + s) for s in suffixes):
            return name
    if re.fullmatch(r"[\d.]+|\[?[0-9a-f:]+\]?", host):
        return host
    labels = host.split(".")
    keep = 3 if len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in _SECOND_LEVEL else 2
    return ".".join(labels[-keep:])


@dataclass
class Tally:
    requests: int = 0
    bytes_in: int = 0
    bytes_out: int = 0
    errors: int = 0
    slowed: int = 0

    def add(self, row: Any) -> None:
        for f in FIELDS:
            setattr(self, f, getattr(self, f) + int(row[f] or 0))  # (Postgres sums BIGINTs as Decimal)

    @property
    def total(self) -> int:
        return self.bytes_in + self.bytes_out


@dataclass
class Group:
    label: str
    tally: Tally = field(default_factory=Tally)
    parts: dict[Any, "Group"] = field(default_factory=dict)
    community: Any = None  # the communities row, for a community's group

    def part(self, key: Any, label: str | None = None) -> "Group":
        if key not in self.parts:
            self.parts[key] = Group(label if label is not None else str(key))
        return self.parts[key]

    def sorted(self, by: str = "requests") -> list["Group"]:
        return sorted(self.parts.values(), key=lambda g: (-getattr(g.tally, by), g.label))


@dataclass
class Report:
    days: int
    since: str
    out: Group
    streams: Group
    services: Group
    communities: Group
    purposes: Group
    timeline: list[tuple[str, Tally, Tally]]  # (hour, or day for longer periods; out, in), oldest first


def report(db: Any, days: int) -> Report:
    """What was counted in the last `days` days: the last 24 hours by the
    hour, longer by the (UTC) day."""
    now = datetime.now(timezone.utc)
    if days <= 1:
        keys = [(now - timedelta(hours=h)).strftime(HOUR) for h in range(23, -1, -1)]
    else:
        keys = [(now - timedelta(days=d)).strftime("%Y-%m-%d") for d in range(days - 1, -1, -1)]
    since = keys[0] if days <= 1 else keys[0] + "T00"
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT hour, direction, host, community_id, purpose, " + ", ".join(f"SUM({f}) AS {f}" for f in FIELDS)
            + " FROM traffic WHERE hour>=? GROUP BY hour, direction, host, community_id, purpose", (since,)).fetchall()
        ids = sorted({r["community_id"] for r in rows if r["community_id"]})
        communities = {c["id"]: c for c in conn.execute(
            f"SELECT id, name, canonical_ap_id FROM communities WHERE id IN ({','.join('?' * len(ids))})",
            ids)} if ids else {}
    out, streams = Group("Asked of others"), Group("Pushed to ThreadBNC")
    services, by_community, purposes = Group("Services"), Group("Communities"), Group("Why")
    buckets = {k: (Tally(), Tally()) for k in keys}
    for r in rows:
        cid = r["community_id"] or 0
        pair = buckets.get(r["hour"] if days <= 1 else r["hour"][:10]) or (Tally(), Tally())
        if r["direction"] == "in":
            streams.tally.add(r)
            pair[1].add(r)
            stream = streams.part((r["host"], cid), r["host"])
            stream.community = communities.get(cid)
            stream.tally.add(r)
            continue
        out.tally.add(r)
        pair[0].add(r)
        svc = services.part(service(r["host"]))
        svc.tally.add(r)
        svc.part(r["host"]).tally.add(r)
        c = by_community.part(cid, "" if not cid else "gone")
        c.community = communities.get(cid)
        c.tally.add(r)
        purposes.part(r["purpose"], PURPOSES.get(r["purpose"], r["purpose"])).tally.add(r)
    timeline = [(k, *buckets[k]) for k in keys]
    return Report(days, since, out, streams, services, by_community, purposes, timeline)
