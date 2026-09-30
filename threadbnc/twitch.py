"""Twitch live status and viewer counts through the authenticated Helix API."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import httpx

from .adapters.base import RemotePaused, RemoteUnavailable
from .adapters.http import HostThrottle
from .traffic import metered


@dataclass(frozen=True)
class TwitchLive:
    live: bool
    title: str | None = None
    viewer_count: int | None = None
    video_id: str | None = None
    thumbnail_url: str | None = None


class TwitchClient:
    def __init__(self, client_id: str | None, client_secret: str | None, user_agent: str,
                 timeout: float = 20.0, throttle: HostThrottle | None = None,
                 transport: httpx.BaseTransport | None = None):
        self.client_id, self.client_secret = client_id, client_secret
        self.throttle = throttle or HostThrottle(1.0)
        self.client = httpx.Client(timeout=timeout, transport=metered(transport),
                                   headers={"User-Agent": user_agent})
        self._token: str | None = None
        self._token_until = 0.0

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        host = httpx.URL(url).host
        self.throttle.wait(host)
        try:
            response = self.client.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            raise RemoteUnavailable(f"{host}: {type(exc).__name__}: {exc}") from exc
        self.throttle.note(host, response.status_code, response.headers)
        if response.status_code == 429:
            raise RemotePaused(f"{host}: HTTP 429", self.throttle.paused_for(host))
        if response.status_code >= 400:
            raise RemoteUnavailable(f"{host}: HTTP {response.status_code}")
        return response

    def _access_token(self) -> str:
        if not self.configured:
            raise RemoteUnavailable("Twitch credentials are not configured")
        if self._token and time.monotonic() < self._token_until:
            return self._token
        response = self._request("POST", "https://id.twitch.tv/oauth2/token", data={
            "client_id": self.client_id, "client_secret": self.client_secret,
            "grant_type": "client_credentials"})
        try:
            data = response.json()
            token = data["access_token"]
            expires = int(data["expires_in"])
        except (ValueError, KeyError, TypeError) as exc:
            raise RemoteUnavailable("Twitch did not return an app access token") from exc
        self._token, self._token_until = token, time.monotonic() + max(0, expires - 60)
        return token

    def check(self, login: str) -> TwitchLive:
        """An empty Get Streams result means the channel is offline."""
        token = self._access_token()
        response = self._request("GET", "https://api.twitch.tv/helix/streams",
                                 params={"user_login": login},
                                 headers={"Client-Id": self.client_id, "Authorization": f"Bearer {token}"})
        try:
            rows = response.json()["data"]
        except (ValueError, KeyError, TypeError) as exc:
            raise RemoteUnavailable("Twitch did not return stream data") from exc
        if not isinstance(rows, list):
            raise RemoteUnavailable("Twitch returned invalid stream data")
        stream = next((r for r in rows if isinstance(r, dict) and r.get("user_login", "").lower() == login.lower()
                       and r.get("type") == "live"), None)
        if stream is None:
            return TwitchLive(False)
        count = stream.get("viewer_count")
        viewers = count if isinstance(count, int) and not isinstance(count, bool) and count >= 0 else None
        title = stream.get("title")
        thumb = stream.get("thumbnail_url")
        thumbnail = thumb.replace("{width}", "320").replace("{height}", "180") if isinstance(thumb, str) and \
            thumb.startswith("https://static-cdn.jtvnw.net/") else None
        return TwitchLive(True, title if isinstance(title, str) else None, viewers, None, thumbnail)
