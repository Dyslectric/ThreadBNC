"""Livestream links (Twitch, YouTube live, Owncast) open the stream's player in a box."""

from __future__ import annotations

import json
from urllib.parse import parse_qs

import httpx
import pytest
from fastapi.testclient import TestClient

from threadbnc import livestream, twitch, youtube
from threadbnc.youtube_api import YouTubeDataClient
from threadbnc.adapters import RSS_DOMAIN
from threadbnc.adapters.http import HostThrottle
from threadbnc.adapters.rss import FeedFetcher, RssAdapter
from threadbnc.livestream import Stream, stream_of
from threadbnc.render import render_markdown
from threadbnc.web import create_app
from threadbnc.db import utcnow
from threadbnc.db import open_database
from threadbnc.vault import TokenVault

CHANNEL = "UCS0N5baNlQWJCUrhCEo8WlA"
VIDEO = "dQw4w9WgXcQ"


def live_youtube_page(live=True):
    player = {"videoDetails": {"videoId": VIDEO, "channelId": CHANNEL, "title": "Studio live",
                               "isLiveContent": True, "isLive": live}}
    data = {"contents": {"videoViewCountRenderer": {"viewCount": {"simpleText": "1,234 watching now"}}}}
    return (f"<script>var ytInitialPlayerResponse = {json.dumps(player)};</script>"
            f"<script>var ytInitialData = {json.dumps(data)};</script>")


def test_live_status_parsers_do_not_mistake_a_recording_for_live():
    live = youtube.parse_live_page(live_youtube_page(), channel_id=CHANNEL)
    assert live.live and live.video_id == VIDEO and live.viewer_count == 1234
    assert not youtube.parse_live_page(live_youtube_page(False), video_id=VIDEO).live
    assert youtube.parse_live_page("<html>sign in</html>") is None
    assert youtube.parse_live_page(live_youtube_page(), channel_id="UCxxxxxxxxxxxxxxxxxxxxxx") is None
    assert livestream.parse_owncast_live({"online": True, "versionNumber": "0.2", "viewerCount": 3,
                                          "streamTitle": "Hi"}).viewer_count == 3
    assert livestream.parse_owncast_live({"status": "fine"}) is None


def test_twitch_helix_requires_credentials_and_reports_current_viewers():
    requests = []

    def handle(request):
        requests.append(request)
        if request.url.host == "id.twitch.tv":
            return httpx.Response(200, json={"access_token": "secret-token", "expires_in": 3600})
        return httpx.Response(200, json={"data": [{"user_login": "vinesauce", "type": "live",
                                                  "title": "Playing now", "viewer_count": 456,
                                                  "thumbnail_url": "https://static-cdn.jtvnw.net/previews-ttv/live_user_vinesauce-{width}x{height}.jpg"}]})

    client = twitch.TwitchClient("client-id", "client-secret", "test", throttle=HostThrottle(0),
                                 transport=httpx.MockTransport(handle))
    assert client.check("vinesauce") == twitch.TwitchLive(
        True, "Playing now", 456, None,
        "https://static-cdn.jtvnw.net/previews-ttv/live_user_vinesauce-320x180.jpg")
    assert client.check("vinesauce").viewer_count == 456
    assert len([r for r in requests if r.url.host == "id.twitch.tv"]) == 1
    assert requests[-1].headers["client-id"] == "client-id"
    assert requests[-1].headers["authorization"] == "Bearer secret-token"


def test_twitch_credentials_can_be_saved_in_settings_and_refreshed_across_processes(settings, bouncer):
    client = TestClient(create_app(settings, bouncer))
    assert client.post("/settings/twitch", data={"client_id": "x", "client_secret": "private"},
                       follow_redirects=False).status_code == 303  # sign-in required
    client.post("/login", data={"password": "pw"})
    assert "Twitch" in client.get("/settings").text
    assert client.post("/settings/twitch", data={"client_id": " ", "client_secret": "private"}).status_code == 200
    assert not bouncer.twitch.configured

    response = client.post("/settings/twitch", data={"client_id": "first", "client_secret": "private"})
    assert response.status_code == 200
    assert bouncer.twitch.configured
    assert 'value="first"' in response.text and "private" not in response.text
    with bouncer.db.connect() as conn:
        raw = conn.execute("SELECT value FROM app_settings WHERE key='twitch_credentials'").fetchone()["value"]
    assert "private" not in raw and "first" in raw

    # Simulate a separate bouncer process with its own DB handle and cached token.
    other_db = open_database(settings)
    other = twitch.TwitchCredentials(other_db, TokenVault(settings.credentials_key, settings.data_dir), None, None)
    tokens = []

    def handle(request):
        if request.url.host == "id.twitch.tv":
            tokens.append(parse_qs(request.content.decode())["client_id"][0])
            return httpx.Response(200, json={"access_token": tokens[-1], "expires_in": 3600})
        assert request.headers["client-id"] == tokens[-1]
        return httpx.Response(200, json={"data": []})

    checker = twitch.TwitchClient(None, None, "test", throttle=HostThrottle(0),
                                  transport=httpx.MockTransport(handle), credentials=other)
    assert checker.configured and checker.check("vinesauce") == twitch.TwitchLive(False)
    client.post("/settings/twitch", data={"client_id": "second", "client_secret": "new-secret"})
    assert checker.check("vinesauce") == twitch.TwitchLive(False)
    assert tokens == ["first", "second"]
    client.post("/settings/twitch/remove")
    assert not checker.configured and not bouncer.twitch.configured


def test_youtube_data_api_checks_known_video_ids_and_current_viewers(settings, bouncer):
    requests = []
    response = {"items": [{"id": VIDEO, "snippet": {"title": "Studio live", "channelId": CHANNEL,
                                                "liveBroadcastContent": "live"},
                           "liveStreamingDetails": {"actualStartTime": "2026-09-29T12:00:00Z",
                                                    "concurrentViewers": "1234"}}]}

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json=response)

    bouncer.youtube_api = YouTubeDataClient("api-key", "test", throttle=HostThrottle(0),
                                             transport=httpx.MockTransport(handle))
    bouncer.request_live_checks()
    with bouncer.db.transaction() as conn:
        livestream.register_live_checks(conn, [("youtube-video", VIDEO)])
    assert bouncer.check_live_once()
    with bouncer.db.connect() as conn:
        status = livestream.current_statuses(conn, utcnow())[("youtube-video", VIDEO)]
    assert status["status"] == "live" and status["viewer_count"] == 1234
    assert status["title"] == "Studio live"
    assert len(requests) == 1
    assert requests[0].url.path == "/youtube/v3/videos"
    assert requests[0].url.params["id"] == VIDEO
    assert requests[0].url.params["part"] == "snippet,liveStreamingDetails"
    assert requests[0].headers["x-goog-api-key"] == "api-key"
    assert "api-key" not in str(requests[0].url)

    response["items"][0]["snippet"]["liveBroadcastContent"] = "none"
    assert not bouncer.youtube_api.check(VIDEO).live
    response["items"][0]["snippet"]["liveBroadcastContent"] = "live"
    response["items"][0]["liveStreamingDetails"] = {"actualEndTime": "2026-09-29T13:00:00Z"}
    assert not bouncer.youtube_api.check(VIDEO).live


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
        if request.url.host == "www.youtube.com" and request.url.path == f"/channel/{CHANNEL}/live":
            return httpx.Response(200, text=live_youtube_page())
        if request.url.host == "blog.example" and request.url.path == "/api/status":
            return httpx.Response(200, json={"status": "fine"})
        if request.url.host == "i.ytimg.com" and request.url.path == f"/vi/{VIDEO}/hqdefault.jpg":
            return httpx.Response(200, content=b"jpeg", headers={"Content-Type": "image/jpeg"})
        if request.url.host == "watch.owncast.online" and request.url.path == "/thumbnail.jpg":
            return httpx.Response(200, content=b"jpeg", headers={"Content-Type": "image/jpeg"})
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


def test_followed_youtube_channel_is_shown_with_checked_viewers(settings, bouncer, sites):
    now = utcnow()
    feed = f"https://www.youtube.com/feeds/videos.xml?channel_id={CHANNEL}"
    with bouncer.db.transaction() as conn:
        cid = conn.execute("INSERT INTO communities(canonical_ap_id, name, first_seen_at, last_seen_at) "
                           "VALUES (?,?,?,?)", ("rss:" + feed, "Studio", now, now)).lastrowid
        conn.execute("INSERT INTO community_follows(community_id, followed_at, capture_since, "
                     "poll_interval_minutes, source_domain, source_ref) VALUES (?,?,?,?,?,?)",
                     (cid, now, now, 30, RSS_DOMAIN, feed))
    bouncer.request_live_checks()
    assert bouncer.check_live_once()
    page = logged_in(settings, bouncer).get("/live").text
    assert "Live from channels you follow" in page
    assert "Studio live" in page and "1,234 watching" in page
    assert f'href="/live/youtube/{VIDEO}"' in page
    assert f'src="/live/thumbnail/youtube/{VIDEO}"' in page
    thumb = logged_in(settings, bouncer).get(f"/live/thumbnail/youtube/{VIDEO}")
    assert thumb.status_code == 200 and thumb.content == b"jpeg" and thumb.headers["content-type"] == "image/jpeg"
    with bouncer.db.connect() as conn:
        livestream.register_live_checks(conn, [("owncast", "watch.owncast.online")])
    assert bouncer.check_live_once()
    with bouncer.db.connect() as conn:
        status = livestream.current_statuses(conn, utcnow())[("owncast", "watch.owncast.online")]
    assert status["status"] == "live" and status["viewer_count"] == 3
    with bouncer.db.transaction() as conn:
        livestream.save_owncast(conn, "watch.owncast.online", True, utcnow())
    assert logged_in(settings, bouncer).get("/live/thumbnail/owncast/watch.owncast.online").content == b"jpeg"
    assert logged_in(settings, bouncer).get("/live/thumbnail/owncast/blog.example").status_code == 404
