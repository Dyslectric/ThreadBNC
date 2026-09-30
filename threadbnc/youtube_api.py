"""Live status and concurrent viewers for known YouTube video IDs."""

from __future__ import annotations

import httpx

from .adapters.base import RemotePaused, RemoteUnavailable
from .adapters.http import HostThrottle
from .traffic import metered
from .youtube import LiveResult, is_video_id, thumbnail_url


class YouTubeDataClient:
    def __init__(self, api_key: str | None, user_agent: str, timeout: float = 20.0,
                 throttle: HostThrottle | None = None, transport: httpx.BaseTransport | None = None):
        self.api_key = api_key
        self.throttle = throttle or HostThrottle(1.0)
        self.client = httpx.Client(timeout=timeout, transport=metered(transport),
                                   headers={"User-Agent": user_agent})

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def check(self, video_id: str) -> LiveResult:
        """Check a public video's broadcast status without searching for its ID."""
        if not self.configured:
            raise RemoteUnavailable("YouTube Data API key is not configured")
        if not is_video_id(video_id):
            raise ValueError("invalid YouTube video ID")
        host = "www.googleapis.com"
        self.throttle.wait(host)
        try:
            response = self.client.get(f"https://{host}/youtube/v3/videos",
                                       params={"part": "snippet,liveStreamingDetails", "id": video_id},
                                       headers={"X-Goog-Api-Key": self.api_key})
        except httpx.HTTPError as exc:
            raise RemoteUnavailable(f"{host}: {type(exc).__name__}: {exc}") from exc
        self.throttle.note(host, response.status_code, response.headers)
        if response.status_code == 429:
            raise RemotePaused(f"{host}: HTTP 429", self.throttle.paused_for(host))
        if response.status_code >= 400:
            raise RemoteUnavailable(f"{host}: HTTP {response.status_code}")
        try:
            items = response.json()["items"]
        except (ValueError, KeyError, TypeError) as exc:
            raise RemoteUnavailable("YouTube did not return video data") from exc
        if not isinstance(items, list):
            raise RemoteUnavailable("YouTube returned invalid video data")
        if not items:
            return LiveResult(False)
        video = items[0]
        if not isinstance(video, dict) or video.get("id") != video_id:
            raise RemoteUnavailable("YouTube returned the wrong video")
        snippet = video.get("snippet")
        details = video.get("liveStreamingDetails") or {}
        if not isinstance(snippet, dict) or not isinstance(details, dict):
            raise RemoteUnavailable("YouTube returned invalid broadcast data")
        state = snippet.get("liveBroadcastContent")
        if state not in ("live", "upcoming", "none"):
            raise RemoteUnavailable("YouTube did not return broadcast status")
        live = state == "live" and not details.get("actualEndTime")
        title = snippet.get("title")
        channel = snippet.get("channelId")
        count = details.get("concurrentViewers") if live else None
        valid_count = (isinstance(count, str) and count.isdecimal() or
                       isinstance(count, int) and not isinstance(count, bool) and count >= 0)
        viewers = int(count) if valid_count else None
        return LiveResult(live, video_id, title if isinstance(title, str) else None,
                          channel if isinstance(channel, str) else None, viewers,
                          thumbnail_url(video_id) if live else None)
