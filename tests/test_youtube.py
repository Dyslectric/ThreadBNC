"""YouTube: channels followed as feeds, and videos saved with yt-dlp only for kept posts."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from threadbnc import youtube
from threadbnc.adapters import parse_community_ref, rss
from threadbnc.adapters.http import HostThrottle
from threadbnc.adapters.rss import FeedFetcher, RssAdapter
from threadbnc.db import utcnow
from threadbnc.web import create_app

CHANNEL = "UCS0N5baNlQWJCUrhCEo8WlA"
FEED = f"{youtube.FEED}?channel_id={CHANNEL}"
VIDEO = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 100

def lockup(vid, title, ago, channel=None):
    md = {"title": {"content": title},
          "metadata": {"contentMetadataViewModel": {"metadataRows": [{"metadataParts": [
              {"text": {"content": "222K views"}}, {"text": {"content": ago}, "accessibilityLabel": ago}]}]}}}
    if channel:
        md["image"] = {"decoratedAvatarViewModel": {"a11yLabel": f"Go to channel {channel}"}}
    return {"richItemRenderer": {"content": {"lockupViewModel": {
        "contentId": vid, "contentType": "LOCKUP_CONTENT_TYPE_VIDEO",
        "contentImage": {"thumbnailViewModel": {"image": {"sources": [{"url": f"https://i.ytimg.com/vi/{vid}/hq720.jpg?sqp=x"}]}}},
        "metadata": {"lockupMetadataViewModel": md}}}}}


def yt_page(items, meta):
    data = {"metadata": meta, "contents": {"twoColumnBrowseResultsRenderer": {"tabs": [
        {"tabRenderer": {"content": {"richGridRenderer": {"contents": items}}}}]}}}
    return (f"<html><head><title>x</title></head><body><script>var ytInitialData = {json.dumps(data)};"
            "</script><script>var other = {};</script></body></html>")


VIDEOS_PAGE = yt_page([
    lockup("dQw4w9WgXcQ", "Hello, world from a 6502", "3 hours ago"),
    lockup("gnmrKDpTM7o", "Breadboard clock part 3", "2 weeks ago"),
    {"richItemRenderer": {"content": {"lockupViewModel": {  # a premiere: not out yet
        "contentId": "aaaaaaaaaaa", "contentType": "LOCKUP_CONTENT_TYPE_VIDEO",
        "metadata": {"lockupMetadataViewModel": {"title": {"content": "Coming soon"}, "metadata": {
            "contentMetadataViewModel": {"metadataRows": [{"metadataParts": [
                {"text": {"content": "Premieres 10/1/26, 9:00 AM"}}]}]}}}}}}}},
], {"channelMetadataRenderer": {"title": "Ben Eater", "description": "Electronics.",
                                "channelUrl": f"https://www.youtube.com/channel/{CHANNEL}"}})

PLAYLIST_PAGE = yt_page([
    lockup("LnzuMJLZRdU", "6502 part 1", "7 years ago", channel="Ben Eater"),
    lockup("yl8vPW5hydQ", "6502 part 2", "6 years ago", channel="Ben Eater"),
], {"playlistMetadataRenderer": {"title": "Build a 65c02-based computer"}})

PAGE = f"""<html><head><link rel="canonical" href="https://www.youtube.com/channel/{CHANNEL}">
<script>var x = {{"channelId":"UCxxxxxxxxxxxxxxxxxxxxxx"}};</script></head><body></body></html>"""


class FakeYouTube:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/feeds/videos.xml":  # YouTube's feeds, lately
            return httpx.Response(404, text="<html>Error 404</html>", headers={"content-type": "text/html"})
        if request.url.path == f"/channel/{CHANNEL}/videos":
            return httpx.Response(200, text=VIDEOS_PAGE, headers={"content-type": "text/html"})
        if request.url.path == "/playlist":
            return httpx.Response(200, text=PLAYLIST_PAGE, headers={"content-type": "text/html"})
        if request.url.path == "/@BenEater":
            return httpx.Response(200, text=PAGE, headers={"content-type": "text/html"})
        return httpx.Response(404)


@pytest.fixture
def yt(bouncer, monkeypatch):
    fake = FakeYouTube()
    bouncer.rss_adapter = RssAdapter(FeedFetcher("test", throttle=HostThrottle(0), check_host=False,
                                                 transport=httpx.MockTransport(fake.handle)))
    monkeypatch.setattr(rss, "FEED_CACHE", 0)
    return fake


def one(b, sql, *args):
    with b.db.connect() as conn:
        return conn.execute(sql, args).fetchone()


def logged_in(settings, bouncer):
    client = TestClient(create_app(settings, bouncer))
    client.post("/login", data={"password": "pw"})
    return client


# -- links -----------------------------------------------------------------------------------

def test_channel_links_become_feeds():
    assert youtube.feed_url(f"https://www.youtube.com/channel/{CHANNEL}/videos") == FEED
    assert youtube.feed_url("youtube.com/user/techconnections") == f"{youtube.FEED}?user=techconnections"
    assert youtube.feed_url("https://www.youtube.com/playlist?list=PLabc") == f"{youtube.FEED}?playlist_id=PLabc"
    assert youtube.feed_url("https://www.youtube.com/@BenEater") is None  # needs the page
    assert youtube.feed_url("https://blog.example/channel/x") is None
    assert youtube.channel_page("https://m.youtube.com/@BenEater/videos") == "https://www.youtube.com/@BenEater"
    assert youtube.channel_id_in(PAGE) == CHANNEL  # its own id, not another channel's mentioned later


def test_video_ids():
    for url in (VIDEO, "https://youtu.be/dQw4w9WgXcQ", "https://www.youtube.com/shorts/dQw4w9WgXcQ",
                "https://m.youtube.com/watch?v=dQw4w9WgXcQ&t=10", "https://www.youtube.com/live/dQw4w9WgXcQ?si=x"):
        assert youtube.video_id(url) == "dQw4w9WgXcQ", url
    for url in ("https://www.youtube.com/@BenEater", "https://notyoutube.com/watch?v=dQw4w9WgXcQ",
                "https://www.youtube.com/watch?v=short", None):
        assert youtube.video_id(url) is None, url


def test_youtube_refs_are_feeds():
    for text in ("youtube.com/@BenEater", "https://www.youtube.com/@BenEater", "www.youtube.com/c/Foo"):
        ref = parse_community_ref(text)
        assert ref.domain == "rss" and ref.name.startswith("https://"), text


def test_cookies_are_turned_into_a_cookie_file():
    jar = youtube.cookies_txt("Cookie: SID=abc; HSID=def")
    assert jar.startswith("# Netscape HTTP Cookie File\n")
    assert ".youtube.com\tTRUE\t/\tTRUE\t2000000000\tSID\tabc\n" in jar and "\tHSID\tdef" in jar
    exported = "# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t1\tSID\tx\n"
    assert youtube.cookies_txt(exported) == exported
    assert youtube.cookies_txt("not cookies at all") == ""


# -- following -------------------------------------------------------------------------------

def test_follow_a_channel_by_its_handle(settings, bouncer, yt):
    cid = bouncer.follow_community("youtube.com/@BenEater", None, 30, backfill=True)
    f = one(bouncer, "SELECT source_domain, source_ref FROM community_follows WHERE community_id=?", cid)
    assert tuple(f) == ("rss", FEED)
    assert one(bouncer, "SELECT name, canonical_ap_id FROM communities WHERE id=?", cid)[:] == \
        ("Ben Eater", f"rss:{FEED}")
    page_fetch = next(r for r in yt.requests if r.url.path == "/@BenEater")
    assert "SOCS=CAI" in page_fetch.headers["cookie"]  # not the EU consent page
    bouncer.poll_follow(cid)
    assert not any(r.url.path == "/feeds/videos.xml" for r in yt.requests)  # the page, not the feed
    row = one(bouncer, "SELECT r.title, r.body, r.url, o.thumbnail_url, o.created_at, a.username "
                       "FROM objects o JOIN revisions r ON r.object_id=o.id JOIN actors a ON a.id=o.author_id "
                       "WHERE o.canonical_ap_id='rss:yt:video:dQw4w9WgXcQ'")
    assert row["title"] == "Hello, world from a 6502" and row["url"] == VIDEO and row["body"] is None
    assert row["thumbnail_url"] == "https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg"
    assert row["username"] == "Ben Eater"
    age = datetime.now(timezone.utc) - datetime.fromisoformat(row["created_at"].replace("Z", "+00:00"))
    assert timedelta(hours=2, minutes=59) < age < timedelta(hours=3, minutes=1)  # "3 hours ago"
    assert one(bouncer, "SELECT COUNT(*) FROM objects")[0] == 2  # the premiere waits until it's out
    client = logged_in(settings, bouncer)
    assert "· YouTube" in client.get("/").text
    edit = client.get("/feeds/new").text
    assert '<legend class="small muted">YouTube</legend>' in edit


# -- saving videos ---------------------------------------------------------------------------

@pytest.fixture
def downloads(bouncer, monkeypatch, tmp_path):
    calls = []

    def fake_download(url, workdir, session, max_bytes):
        calls.append((url, max_bytes, session.secrets()))
        path = workdir / ".yt-test.mp4"
        workdir.mkdir(parents=True, exist_ok=True)
        path.write_bytes(MP4)
        return path, 1080

    monkeypatch.setattr(youtube, "download", fake_download)
    return calls


def video_media(bouncer):
    return one(bouncer, "SELECT * FROM media WHERE url=?", VIDEO)


def test_videos_are_saved_only_when_kept(settings, bouncer, yt, downloads):
    cid = bouncer.follow_community(f"https://www.youtube.com/channel/{CHANNEL}", None, 30, backfill=True)
    bouncer.poll_follow(cid)
    m = video_media(bouncer)
    assert (m["kept_only"], m["held"], m["status"]) == (1, 1, "pending")
    tid = one(bouncer, "SELECT t.id FROM archived_threads t JOIN objects o ON o.id=t.root_object_id "
                       "WHERE o.canonical_ap_id='rss:yt:video:dQw4w9WgXcQ'")[0]

    bouncer.media.fetch_pending()
    with bouncer.db.transaction() as conn:  # opened, not kept: still not downloaded
        conn.execute("UPDATE archived_threads SET opened_at=? WHERE id=?", (utcnow(), tid))
    bouncer.media.fetch_pending()
    assert downloads == [] and video_media(bouncer)["status"] == "pending"
    client = logged_in(settings, bouncer)
    assert "Keep this post to save the video." in client.get(f"/t/{tid}").text

    with bouncer.db.transaction() as conn:
        conn.execute("UPDATE archived_threads SET retention='manual' WHERE id=?", (tid,))
    bouncer.media.fetch_pending()
    assert [(u, n) for u, n, _ in downloads] == [(VIDEO, youtube.DEFAULT_MAX_MB * 1_000_000)]
    m = video_media(bouncer)
    assert (m["status"], m["content_type"]) == ("ok", "video/mp4")
    assert (bouncer.media_dir / m["storage_path"]).read_bytes() == MP4
    page = client.get(f"/t/{tid}").text
    assert f'<video class="media" src="/media/{m["id"]}" controls playsinline' in page  # with sound, no loop


def test_communities_saving_only_pictures_skip_videos(bouncer, yt, downloads):
    cid = bouncer.follow_community(f"https://www.youtube.com/channel/{CHANNEL}", None, 30, backfill=True)
    bouncer.poll_follow(cid)
    with bouncer.db.transaction() as conn:
        conn.execute("UPDATE communities SET media_archive='images' WHERE id=?", (cid,))
        conn.execute("UPDATE archived_threads SET retention='manual'")
    bouncer.media.fetch_pending()
    assert downloads == [] and video_media(bouncer)["status"] == "skipped"


def test_a_video_youtube_wont_serve_without_a_session(settings, bouncer, yt, monkeypatch):
    def refuse(url, workdir, session, max_bytes):
        raise youtube.NeedsSession("YouTube wants a signed-in session for this video.")

    monkeypatch.setattr(youtube, "download", refuse)
    cid = bouncer.follow_community(f"https://www.youtube.com/channel/{CHANNEL}", None, 30, backfill=True)
    bouncer.poll_follow(cid)
    with bouncer.db.transaction() as conn:
        conn.execute("UPDATE archived_threads SET retention='manual'")
    bouncer.media.fetch_pending()
    assert video_media(bouncer)["status"] == "failed"
    client = logged_in(settings, bouncer)
    assert "wants a signed-in session" in client.get("/youtube").text

    # Saving a session tries it again, and it's used for the download.
    seen = []
    monkeypatch.setattr(youtube, "download", lambda url, wd, s, mb: (seen.append(s.secrets()), refuse(url, wd, s, mb)))
    r = client.post("/youtube/session", data={"cookies": "SID=secret1; HSID=h", "po_token": "tok"})
    assert r.status_code == 200 and "trying 2 that failed again" in r.text  # both kept videos
    assert "secret1" not in r.text and "secret1" not in client.get("/youtube").text
    assert video_media(bouncer)["status"] == "pending"
    bouncer.media.fetch_pending()
    cookies, po_token = seen[0]
    assert "\tSID\tsecret1" in cookies and po_token == "tok"
    stored = one(bouncer, "SELECT value FROM app_settings WHERE key='youtube_session'")[0]
    assert "secret1" not in stored  # encrypted


def test_session_form_checks_what_was_pasted(settings, bouncer):
    client = logged_in(settings, bouncer)
    assert "aren&#39;t from a signed-in" in client.post("/youtube/session", data={"cookies": "PREF=f6=40"}).text
    client.post("/youtube/session", data={"cookies": "SID=a; HSID=b"})
    assert bouncer.youtube.status()["has_cookies"]
    client.post("/youtube/forget")
    assert not bouncer.youtube.status()["has_cookies"]
    client.post("/youtube/limits", data={"max_mb": "500", "max_height": "720"})
    assert (bouncer.youtube.status()["max_mb"], bouncer.youtube.status()["max_height"]) == (500, 720)


def test_youtube_links_in_other_posts_are_saved_when_kept(server, bouncer, downloads):
    from .conftest import DOMAIN
    server.add_post("1", "neat video", "")
    server.edit_post("1", url="https://youtu.be/dQw4w9WgXcQ")
    bouncer.ingest_url(f"https://{DOMAIN}/post/1")  # kept by link
    bouncer.media.fetch_pending()
    m = one(bouncer, "SELECT * FROM media WHERE url='https://youtu.be/dQw4w9WgXcQ'")
    assert (m["kept_only"], m["status"]) == (1, "ok") and downloads


def test_a_playlist_is_read_from_its_page(bouncer, yt):
    cid = bouncer.follow_community("https://www.youtube.com/playlist?list=PLabc", None, 30, backfill=True)
    assert one(bouncer, "SELECT name FROM communities WHERE id=?", cid)[0] == "Build a 65c02-based computer"
    bouncer.poll_follow(cid)
    with bouncer.db.connect() as conn:
        titles = [r[0] for r in conn.execute(
            "SELECT r.title FROM objects o JOIN revisions r ON r.object_id=o.id ORDER BY o.created_at")]
    assert titles == ["6502 part 1", "6502 part 2"]


def test_the_page_parser_takes_the_older_layout_too():
    page = yt_page([{"richItemRenderer": {"content": {"videoRenderer": {
        "videoId": "dQw4w9WgXcQ", "title": {"runs": [{"text": "Old style"}]},
        "publishedTimeText": {"simpleText": "Streamed 1 day ago"}}}}}],
        {"channelMetadataRenderer": {"title": "Chan"}})
    now = datetime(2026, 9, 23, tzinfo=timezone.utc)
    listed = youtube.parse_page(page, "https://www.youtube.com/channel/x/videos", now)
    (v,) = listed.videos
    assert (v.id, v.title, v.published, v.author) == ("dQw4w9WgXcQ", "Old style", now - timedelta(days=1), "Chan")
    assert youtube.parse_page("<html>no data</html>", "x") is None


def test_channels_are_checked_like_subreddits(bouncer, yt):
    bouncer.follow_community(f"https://www.youtube.com/channel/{CHANNEL}", None, 30)
    now = [1000.0]
    bouncer._clock = lambda: now[0]

    def page_reads():
        return sum(1 for r in yt.requests if r.url.path.endswith("/videos"))

    before = page_reads()
    bouncer.tick()
    assert page_reads() == before  # nobody's using ThreadBNC
    bouncer.note_active()
    bouncer.tick()
    assert page_reads() == before  # just back: not at once
    now[0] += 3610  # one channel, checked hourly
    bouncer.tick()
    assert page_reads() == before + 1
