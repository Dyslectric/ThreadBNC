"""Polite shared HTTP client: per-domain throttling, typed errors."""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Mapping

import httpx

from ..traffic import metered
from .base import RemoteAuthError, RemoteNotFound, RemotePaused, RemoteRejected, RemoteUnavailable

log = logging.getLogger("threadbnc.http")

_NOT_FOUND_CODES = ("couldnt_find", "not_found", "notfound", "unknown_post", "unknown_comment")
_AUTH_CODES = ("not_logged_in", "incorrect_login", "missing_totp_token", "incorrect_totp_token",
               "email_not_verified", "registration_application", "deleted", "invalid_token", "jwt")

# A server that says "too many requests" without saying how long to wait gets
# a minute, doubling each time it says so again, up to an hour. One that does
# say is believed, up to a day.
DEFAULT_PAUSE = 60.0
MAX_DEFAULT_PAUSE = 3600.0
MAX_PAUSE = 86400.0


def retry_after(value: str | None) -> float | None:
    """Seconds to wait from a Retry-After header: a number of seconds or an
    HTTP date. None when missing or unreadable."""
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())


class HostThrottle:
    """Keeps at least `min_interval` seconds between requests to the same host.
    Shared by API calls and media downloads, so e.g. lemmy.world's API and its
    image server are spaced together.

    It also remembers servers that answered 429 Too Many Requests (or 503 with
    a Retry-After) and refuses to contact them until the time they asked for
    has passed: `wait` raises RemotePaused at once instead of sleeping, so the
    worker moves on to other servers."""

    def __init__(self, min_interval: float = 1.0):
        self.min_interval = min_interval
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()
        self.exempt: set[str] = set()  # your own servers (federation.py): no need to be polite
        self._paused: dict[str, float] = {}  # host -> monotonic time it may be contacted again
        self._strikes: dict[str, int] = {}  # host -> 429s in a row without a Retry-After

    def paused_for(self, host: str) -> float:
        """Seconds until `host` may be contacted again (0 when it may now)."""
        with self._lock:
            until = self._paused.get(host)
        return max(0.0, until - time.monotonic()) if until else 0.0

    def check(self, host: str) -> None:
        """Raise RemotePaused if `host` asked us to wait and the time isn't up."""
        wait = self.paused_for(host)
        if wait > 0:
            raise RemotePaused(f"{host} asked us to slow down; waiting {int(wait) + 1} s before asking again", wait)

    def note(self, host: str, status: int, headers: Mapping[str, str]) -> None:
        """Record a response from `host`: a 429 (or a 503 that says when to come
        back) pauses it; anything else clears its run of 429s."""
        asked = retry_after(headers.get("retry-after"))
        if status != 429 and not (status == 503 and asked is not None):
            if host in self._strikes:
                with self._lock:
                    self._strikes.pop(host, None)
            return
        with self._lock:
            if asked is None:
                strikes = self._strikes.get(host, 0) + 1
                self._strikes[host] = strikes
                pause = min(DEFAULT_PAUSE * 2 ** (strikes - 1), MAX_DEFAULT_PAUSE)
            else:
                pause = min(max(asked, 1.0), MAX_PAUSE)
            until = time.monotonic() + pause
            self._paused[host] = max(until, self._paused.get(host, 0.0))
        log.warning("%s answered HTTP %d; not contacting it for %.0f s", host, status, pause)

    def wait(self, host: str) -> None:
        self.check(host)
        if host in self.exempt:
            return
        with self._lock:
            now = time.monotonic()
            slot = max(now, self._last.get(host, float("-inf")) + self.min_interval)
            self._last[host] = slot
        if slot > now:
            time.sleep(slot - now)


class HttpClient:
    def __init__(self, user_agent: str, timeout: float = 20.0, min_interval: float = 1.0):
        self._client = httpx.Client(
            timeout=timeout,
            headers={"User-Agent": user_agent, "Accept": "application/json"},
            follow_redirects=True,
            transport=metered(),
        )
        self.throttle = HostThrottle(min_interval)

    def _throttle(self, domain: str) -> None:
        self.throttle.wait(domain)

    def get_json(self, domain: str, path: str, params: dict[str, Any] | None = None,
                 token: str | None = None) -> Any:
        return self.request_json("GET", domain, path, params=params, token=token)

    def request_json(self, method: str, domain: str, path: str, *, params: dict[str, Any] | None = None,
                     json: dict[str, Any] | None = None, token: str | None = None, throttle: bool = True) -> Any:
        """One API call. Reads raise RemoteUnavailable for anything retryable;
        writes (non-GET) raise RemoteRejected/RemoteAuthError with the server's
        error code so the UI can say what went wrong.

        `throttle=False` is for calls made as one of your accounts while you
        wait for a page, like any Lemmy client would; background fetching
        (archiving, polling) keeps the per-server spacing. Either way, a
        server that asked us to wait (429) isn't contacted until it said."""
        if throttle:
            self._throttle(domain)
        else:
            self.throttle.check(domain)
        url = f"https://{domain}{path}"
        clean = {k: v for k, v in (params or {}).items() if v is not None}
        headers = {"Authorization": f"Bearer {token}"} if token else None
        body = {k: v for k, v in (json or {}).items() if v is not None} if json is not None else None
        try:
            resp = self._client.request(method, url, params=clean, json=body, headers=headers)
        except httpx.HTTPError as exc:
            raise RemoteUnavailable(f"{domain}: {type(exc).__name__}: {exc}") from exc
        self.throttle.note(domain, resp.status_code, resp.headers)
        if resp.status_code == 429:
            raise RemotePaused(f"{url}: HTTP 429 (too many requests)", self.throttle.paused_for(domain))
        if resp.status_code == 404:
            raise RemoteNotFound(f"{url}: 404")
        if resp.status_code == 429 or resp.status_code >= 500:
            raise RemoteUnavailable(f"{url}: HTTP {resp.status_code}")
        try:
            data = resp.json()
        except ValueError as exc:
            raise RemoteUnavailable(f"{url}: non-JSON response (HTTP {resp.status_code})") from exc
        if resp.status_code >= 400:
            err = ""
            if isinstance(data, dict):
                err = str(data.get("error") or data.get("message") or data.get("detail") or "")
            low = err.lower()
            if any(code in low for code in _NOT_FOUND_CODES):
                raise RemoteNotFound(f"{url}: {err}")
            if resp.status_code in (401, 403) or any(code in low for code in _AUTH_CODES):
                raise RemoteAuthError(f"{domain}: {err or 'HTTP %d' % resp.status_code}", err)
            if method != "GET":
                raise RemoteRejected(f"{domain} refused: {err or 'HTTP %d' % resp.status_code}", err)
            raise RemoteUnavailable(f"{url}: HTTP {resp.status_code} {err}")
        return data

    def send(self, method: str, url: str, *, headers: dict[str, str] | None = None, content: bytes | None = None,
             throttle: bool = True) -> httpx.Response:
        """One request whose headers the caller builds (signed ActivityPub
        requests, actor.py), spaced and paused like the rest. Raises
        RemoteUnavailable when the server can't be reached and RemotePaused on
        429; any other answer is the caller's to read."""
        host = (httpx.URL(url).host or "").lower()
        if throttle:
            self._throttle(host)
        else:
            self.throttle.check(host)
        try:
            resp = self._client.request(method, url, headers=headers, content=content)
        except httpx.HTTPError as exc:
            raise RemoteUnavailable(f"{host}: {type(exc).__name__}: {exc}") from exc
        self.throttle.note(host, resp.status_code, resp.headers)
        if resp.status_code == 429:
            raise RemotePaused(f"{url}: HTTP 429 (too many requests)", self.throttle.paused_for(host))
        return resp

    def redirect_target(self, domain: str, path: str) -> str | None:
        """Where https://{domain}{path} redirects to (its Location), without following it."""
        self.throttle.check(domain)
        try:
            resp = self._client.get(f"https://{domain}{path}", follow_redirects=False)
        except httpx.HTTPError as exc:
            raise RemoteUnavailable(f"{domain}: {type(exc).__name__}: {exc}") from exc
        self.throttle.note(domain, resp.status_code, resp.headers)
        return resp.headers.get("location") if resp.is_redirect else None

    def close(self) -> None:
        self._client.close()
