"""Polite shared HTTP client: per-domain throttling, typed errors."""

from __future__ import annotations

import threading
import time
from typing import Any

import httpx

from .base import RemoteAuthError, RemoteNotFound, RemoteRejected, RemoteUnavailable

_NOT_FOUND_CODES = ("couldnt_find", "not_found", "notfound", "unknown_post", "unknown_comment")
_AUTH_CODES = ("not_logged_in", "incorrect_login", "missing_totp_token", "incorrect_totp_token",
               "email_not_verified", "registration_application", "deleted", "invalid_token", "jwt")


class HostThrottle:
    """Keeps at least `min_interval` seconds between requests to the same host.
    Shared by API calls and media downloads, so e.g. lemmy.world's API and its
    image server are spaced together."""

    def __init__(self, min_interval: float = 1.0):
        self.min_interval = min_interval
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()

    def wait(self, host: str) -> None:
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
        (archiving, polling) keeps the per-server spacing."""
        if throttle:
            self._throttle(domain)
        url = f"https://{domain}{path}"
        clean = {k: v for k, v in (params or {}).items() if v is not None}
        headers = {"Authorization": f"Bearer {token}"} if token else None
        body = {k: v for k, v in (json or {}).items() if v is not None} if json is not None else None
        try:
            resp = self._client.request(method, url, params=clean, json=body, headers=headers)
        except httpx.HTTPError as exc:
            raise RemoteUnavailable(f"{domain}: {type(exc).__name__}: {exc}") from exc
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

    def close(self) -> None:
        self._client.close()
