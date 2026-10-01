"""Twitch live status and viewer counts through the authenticated Helix API."""

from __future__ import annotations

import time
import json
from dataclasses import dataclass
from typing import Any

import httpx

from .adapters.base import RemotePaused, RemoteUnavailable
from .adapters.http import HostThrottle
from .traffic import metered
from .db import Database
from .vault import TokenVault


CREDENTIALS_SETTING = "twitch_credentials"
BATCH = 100  # Get Streams takes up to 100 user_login values


class TwitchCredentials:
    """App credentials saved for all processes, with environment defaults."""

    def __init__(self, db: Database, vault: TokenVault, client_id: str | None, client_secret: str | None):
        self.db, self.vault = db, vault
        self.defaults = (client_id, client_secret)

    def current(self) -> tuple[str | None, str | None, str]:
        # Read directly: Database.get_setting caches SQLite values in each process.
        with self.db.connect() as conn:
            row = conn.execute("SELECT value FROM app_settings WHERE key=?", (CREDENTIALS_SETTING,)).fetchone()
        if row is None:
            return *self.defaults, "environment" if all(self.defaults) else "none"
        saved = json.loads(row["value"])
        return saved["client_id"], self.vault.decrypt(saved["secret_enc"]), "app"

    def save(self, client_id: str, client_secret: str) -> None:
        client_id, client_secret = client_id.strip(), client_secret.strip()
        if not client_id or not client_secret:
            raise ValueError("Enter both the Twitch client ID and client secret.")
        value = json.dumps({"client_id": client_id, "secret_enc": self.vault.encrypt(client_secret)})
        with self.db.transaction() as conn:
            conn.execute("INSERT INTO app_settings(key, value) VALUES (?, ?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (CREDENTIALS_SETTING, value))

    def remove(self) -> None:
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM app_settings WHERE key=?", (CREDENTIALS_SETTING,))


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
                 transport: httpx.BaseTransport | None = None,
                 credentials: TwitchCredentials | None = None):
        self.client_id, self.client_secret = client_id, client_secret
        self.credentials = credentials
        self.source = "environment" if client_id and client_secret else "none"
        self.throttle = throttle or HostThrottle(1.0)
        self.client = httpx.Client(timeout=timeout, transport=metered(transport),
                                   headers={"User-Agent": user_agent})
        self._token: str | None = None
        self._token_until = 0.0

    @property
    def configured(self) -> bool:
        self._sync_credentials()
        return bool(self.client_id and self.client_secret)

    def _sync_credentials(self) -> None:
        if self.credentials is None:
            return
        client_id, client_secret, source = self.credentials.current()
        if (client_id, client_secret) != (self.client_id, self.client_secret):
            self.client_id, self.client_secret = client_id, client_secret
            self._token, self._token_until = None, 0.0
        self.source = source

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
        return self.check_many([login])[login.lower()]

    def check_many(self, logins: list[str]) -> dict[str, TwitchLive]:
        """Up to BATCH channels in one Get Streams request, by lowercased login.
        A channel missing from the result is offline."""
        logins = list(dict.fromkeys(login.lower() for login in logins))
        if not logins:
            return {}
        if len(logins) > BATCH:
            raise ValueError(f"at most {BATCH} Twitch channels per request")
        token = self._access_token()
        response = self._request("GET", "https://api.twitch.tv/helix/streams",
                                 params=[*(("user_login", login) for login in logins), ("first", str(BATCH))],
                                 headers={"Client-Id": self.client_id, "Authorization": f"Bearer {token}"})
        try:
            rows = response.json()["data"]
        except (ValueError, KeyError, TypeError) as exc:
            raise RemoteUnavailable("Twitch did not return stream data") from exc
        if not isinstance(rows, list):
            raise RemoteUnavailable("Twitch returned invalid stream data")
        found = {r["user_login"].lower(): r for r in rows if isinstance(r, dict)
                 and isinstance(r.get("user_login"), str) and r.get("type") == "live"}
        return {login: _live(found[login]) if login in found else TwitchLive(False) for login in logins}


def _live(stream: dict[str, Any]) -> TwitchLive:
    count = stream.get("viewer_count")
    viewers = count if isinstance(count, int) and not isinstance(count, bool) and count >= 0 else None
    title = stream.get("title")
    thumb = stream.get("thumbnail_url")
    thumbnail = thumb.replace("{width}", "320").replace("{height}", "180") if isinstance(thumb, str) and \
        thumb.startswith("https://static-cdn.jtvnw.net/") else None
    return TwitchLive(True, title if isinstance(title, str) else None, viewers, None, thumbnail)
