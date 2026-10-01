"""Live status and concurrent viewers for known YouTube video IDs."""

from __future__ import annotations

from typing import Any

import httpx

from .adapters.base import RemotePaused, RemoteUnavailable
from .adapters.http import HostThrottle
from .traffic import metered
from .youtube import LiveResult, is_video_id, thumbnail_url

BATCH = 50  # videos.list takes up to 50 IDs


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
        result = self.check_many([video_id])[video_id]
        if result is None:
            raise RemoteUnavailable("YouTube returned invalid broadcast data")
        return result

    def check_many(self, video_ids: list[str]) -> dict[str, LiveResult | None]:
        """Up to BATCH public videos' broadcast status in one request (one unit
        of quota, however many), without searching for their IDs. A video
        missing from the answer is gone or private: not live. None: YouTube's
        answer about it didn't make sense."""
        if not self.configured:
            raise RemoteUnavailable("YouTube Data API key is not configured")
        video_ids = list(dict.fromkeys(video_ids))
        if not video_ids:
            return {}
        if len(video_ids) > BATCH or not all(is_video_id(v) for v in video_ids):
            raise ValueError("invalid YouTube video IDs")
        host = "www.googleapis.com"
        self.throttle.wait(host)
        try:
            response = self.client.get(f"https://{host}/youtube/v3/videos",
                                       params={"part": "snippet,liveStreamingDetails", "id": ",".join(video_ids),
                                               "maxResults": str(BATCH)},
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
        out: dict[str, LiveResult | None] = {v: LiveResult(False) for v in video_ids}
        for video in items:
            if isinstance(video, dict) and video.get("id") in out:
                out[video["id"]] = _live(video)
        return out


def _live(video: dict[str, Any]) -> LiveResult | None:
    video_id = video["id"]
    snippet = video.get("snippet")
    details = video.get("liveStreamingDetails") or {}
    if not isinstance(snippet, dict) or not isinstance(details, dict):
        return None
    state = snippet.get("liveBroadcastContent")
    if state not in ("live", "upcoming", "none"):
        return None
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
