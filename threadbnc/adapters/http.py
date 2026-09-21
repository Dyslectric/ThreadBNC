"""Polite shared HTTP client: per-domain throttling, typed errors."""

from __future__ import annotations

import threading
import time
from typing import Any

import httpx

from .base import RemoteNotFound, RemoteUnavailable

_NOT_FOUND_CODES = ("couldnt_find", "not_found", "notfound", "unknown_post", "unknown_comment")


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

    def get_json(self, domain: str, path: str, params: dict[str, Any] | None = None) -> Any:
        self._throttle(domain)
        url = f"https://{domain}{path}"
        clean = {k: v for k, v in (params or {}).items() if v is not None}
        try:
            resp = self._client.get(url, params=clean)
        except httpx.HTTPError as exc:
            raise RemoteUnavailable(f"{domain}: {type(exc).__name__}: {exc}") from exc
        if resp.status_code == 404:
            raise RemoteNotFound(f"{url}: 404")
        if resp.status_code in (429,) or resp.status_code >= 500:
            raise RemoteUnavailable(f"{url}: HTTP {resp.status_code}")
        try:
            data = resp.json()
        except ValueError as exc:
            raise RemoteUnavailable(f"{url}: non-JSON response (HTTP {resp.status_code})") from exc
        if resp.status_code >= 400:
            err = str(data.get("error", "")) if isinstance(data, dict) else ""
            if any(code in err.lower() for code in _NOT_FOUND_CODES):
                raise RemoteNotFound(f"{url}: {err}")
            raise RemoteUnavailable(f"{url}: HTTP {resp.status_code} {err}")
        return data

    def close(self) -> None:
        self._client.close()
