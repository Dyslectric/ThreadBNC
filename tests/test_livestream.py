"""Livestream links (Twitch, YouTube live, Owncast) open the stream's player in a box."""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from threadbnc import livestream
from threadbnc.adapters.http import HostThrottle
from threadbnc.adapters.rss import FeedFetcher, RssAdapter
from threadbnc.livestream import Stream, stream_of
from threadbnc.render import render_markdown
from threadbnc.web import create_app

CHANNEL = "UCS0N5baNlQWJCUrhCEo8WlA"


def test_links_that_are_livestreams():
    assert stream_of("https://www.twitch.tv/Vinesauce") == Stream("twitch", "vinesauce")
    assert stream_of("https://m.twitch.tv/vinesauce/") == Stream("twitch", "vinesauce")
    assert stream_of("https://www.twitch.tv/videos/2254911234") == Stream("twitch-video", "2254911234")
    assert stream_of("https://www.youtube.com/live/dQw4w9WgXcQ?si=x") == Stream("youtube", "dQw4w9WgXcQ")
    assert stream_of(f"https://www.youtube.com/channel/{CHANNEL}/live") == Stream("youtube", CHANNEL)
    for url in ("https://www.twitch.tv/directory", "https://www.twitch.tv/vinesauce/videos",
                "https://www.twitch.tv/", "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                "https://www.youtube.com/@BenEater/live", "https://nottwitch.tv/vinesauce", "https://owncast.example/",
                None, "ftp://www.twitch.tv/x"):
        assert stream_of(url) is None, url


def test_players():
    assert Stream("twitch", "vinesauce").embed == "https://player.twitch.tv/?channel=vinesauce&autoplay=true"
    assert Stream("twitch", "vinesauce").needs_parent
    assert Stream("youtube", "dQw4w9WgXcQ").embed == "https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ?autoplay=1"
    assert Stream("youtube", CHANNEL).embed.endswith(f"live_stream?channel={CHANNEL}&autoplay=1")
    assert Stream("owncast", "watch.owncast.online").embed == "https://watch.owncast.online/embed/video"
    assert livestream.from_key("owncast", "not a host") is None
    assert livestream.from_key("twitch", "directory") is None
    assert livestream.from_key("youtube", "short") is None


def test_livestream_links_open_their_box():
    out = render_markdown("on now: https://www.twitch.tv/vinesauce and [the VOD](https://www.twitch.tv/videos/12345)",
                          titles=lambda _v: None)
    assert ('<a href="/live/twitch/vinesauce" class="live-link" title="Twitch live stream: watch it here" '
            'rel="noopener noreferrer nofollow">https://www.twitch.tv/vinesauce</a>') in out
    assert '<a href="/live/twitch-video/12345" class="live-link"' in out and ">the VOD</a>" in out
    assert 'target="_blank"' not in out
    out = render_markdown("https://www.youtube.com/live/dQw4w9WgXcQ", titles=lambda _v: None)
    assert 'href="/live/youtube/dQw4w9WgXcQ"' in out and "video-link" not in out


def test_livestream_links_in_articles():
    from threadbnc import articles
    html = '<p>Watch <a href="https://www.twitch.tv/vinesauce" target="_blank">live</a>.</p>'
    out = articles.render(html, lambda _u: None, "https://blog.example/a", None, None)
    assert '<a href="/live/twitch/vinesauce" class="live-link"' in out and "_blank" not in out


class FakeSites:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host == "watch.owncast.online" and request.url.path == "/api/status":
            return httpx.Response(200, json={"online": True, "viewerCount": 3, "versionNumber": "0.2.0",
                                             "streamTitle": "hi"})
        if request.url.host == "blog.example" and request.url.path == "/api/status":
            return httpx.Response(200, json={"status": "fine"})
        return httpx.Response(404)


@pytest.fixture
def sites(bouncer):
    fake = FakeSites()
    bouncer.rss_adapter = RssAdapter(FeedFetcher("test", throttle=HostThrottle(0), check_host=False,
                                                 transport=httpx.MockTransport(fake.handle)))
    return fake


def logged_in(settings, bouncer):
    client = TestClient(create_app(settings, bouncer))
    client.post("/login", data={"password": "pw"})
    return client


def test_owncast_servers_are_asked_once(settings, bouncer, sites):
    client = logged_in(settings, bouncer)
    hosts = [("hosts", "watch.owncast.online"), ("hosts", "blog.example"), ("hosts", "gone.example"),
             ("hosts", "not a host")]
    assert client.get("/live/owncast", params=hosts).json() == ["watch.owncast.online"]
    assert len(sites.requests) == 3
    assert client.get("/live/owncast", params=hosts).json() == ["watch.owncast.online"]
    assert len(sites.requests) == 3  # remembered, found or not

    box = client.get("/live/owncast/watch.owncast.online?pane=1").text
    assert '<article class="live-box" id="live-owncast-watch.owncast.online"' in box and "<html" not in box
    assert 'data-embed="https://watch.owncast.online/embed/video"' in box and "data-parent" not in box
    assert client.get("/live/owncast/blog.example").status_code == 404


def test_the_box(settings, bouncer, sites):
    client = logged_in(settings, bouncer)
    box = client.get("/live/twitch/vinesauce?pane=1").text
    assert 'data-embed="https://player.twitch.tv/?channel=vinesauce&amp;autoplay=true" data-parent' in box
    assert 'href="https://www.twitch.tv/vinesauce"' in box and "Close" in box
    page = client.get("/live/youtube/dQw4w9WgXcQ").text
    assert "<html" in page and 'href="/youtube/v/dQw4w9WgXcQ"' in page  # it can be saved too
    assert "frame-src https:" in client.get("/live/twitch/vinesauce").headers["content-security-policy"]
    assert client.get("/live/twitch/directory").status_code == 404
    assert client.get("/live/nope/x").status_code == 404
    # Nothing asked of Twitch; of YouTube, only the video's title.
    assert [r.url.path for r in sites.requests] == ["/oembed"]
