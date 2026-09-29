"""Where a page's time goes: Server-Timing, and a log line for slow requests.

Each request gets a set of counters (a ContextVar, which the thread pool that
runs the handlers copies along), and the places that wait add to them: the
database (db.py), other servers (traffic.py's transport, the host throttle) and
template rendering (web.py). The browser's dev tools show them under a
request's Timing tab; a request slower than SLOW is also logged with them, so
the slow ones can be found without opening dev tools."""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

log = logging.getLogger(__name__)

SLOW = 0.5  # seconds: a request slower than this is logged

_current: ContextVar[dict[str, list[float]] | None] = ContextVar("request_timing", default=None)
NAMES = {"db": "database", "remote": "other servers", "wait": "spacing between calls", "render": "template"}


def begin() -> dict[str, list[float]]:
    counters: dict[str, list[float]] = {}
    _current.set(counters)
    return counters


def add(kind: str, seconds: float) -> None:
    """Count one `kind` of wait taking `seconds`, for the request being served (if any)."""
    counters = _current.get()
    if counters is not None:
        entry = counters.setdefault(kind, [0.0, 0.0])
        entry[0] += 1
        entry[1] += seconds


@contextmanager
def span(kind: str) -> Iterator[None]:
    start = time.perf_counter()
    try:
        yield
    finally:
        add(kind, time.perf_counter() - start)


def header(counters: dict[str, list[float]], total: float) -> str:
    """The Server-Timing header's value."""
    parts = [f'{kind};dur={secs * 1000:.1f};desc="{NAMES.get(kind, kind)}, {int(n)} call{"s" if n != 1 else ""}"'
             for kind, (n, secs) in counters.items()]
    parts.append(f"total;dur={total * 1000:.1f}")
    return ", ".join(parts)


def log_slow(method: str, path: str, status: int, counters: dict[str, list[float]], total: float) -> None:
    if total >= SLOW:
        detail = " ".join(f"{k}={v[1] * 1000:.0f}ms/{int(v[0])}" for k, v in counters.items())
        log.warning("slow request: %s %s -> %s in %.0f ms (%s)", method, path, status, total * 1000, detail or "-")
