"""YouTube: channels followed as feeds, and videos saved with yt-dlp only for kept posts."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from threadbnc import media, youtube
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


def watch_page(description, likes):
    player = {"videoDetails": {"videoId": "dQw4w9WgXcQ", "shortDescription": description,
                               "title": "Hello, world from a 6502", "author": "Ben Eater", "channelId": CHANNEL},
              "microformat": {"playerMicroformatRenderer": {"publishDate": "2026-09-01T07:00:00-07:00"}}}
    data = {"contents": {"twoColumnWatchNextResults": {"results": {"results": {"contents": [
        {"videoPrimaryInfoRenderer": {"videoActions": {"menuRenderer": {"topLevelButtons": [
            {"segmentedLikeDislikeButtonViewModel": {"likeButtonViewModel": {"likeButtonViewModel": {
                "toggleButtonViewModel": {"toggleButtonViewModel": {"defaultButtonViewModel": {"buttonViewModel": {
                    "title": "1.2K"}}}},
                "likeCountEntity": {"likeCountIfIndifferentNumber": str(likes)}}}}}]}}}}]}}}}}
    data["contents"]["twoColumnWatchNextResults"]["results"]["results"]["contents"].append(
        {"itemSectionRenderer": {"sectionIdentifier": "comment-item-section", "contents": [
            {"continuationItemRenderer": {"continuationEndpoint": {"continuationCommand": {"token": "TOP"}}}}]}})
    cfg = {"INNERTUBE_API_KEY": "k", "INNERTUBE_CONTEXT": {"client": {
        "clientName": "WEB", "clientVersion": "2.20260922", "hl": "en", "remoteHost": "1.2.3.4"}}}
    return (f"<html><body><script>ytcfg.set({json.dumps(cfg)});</script>"
            f"<script>var ytInitialPlayerResponse = {json.dumps(player)};</script>"
            f"<script>var ytInitialData = {json.dumps(data)};</script></body></html>")


WATCH_PAGE = watch_page("Building a computer.\nParts: https://eater.net/6502_kit\n\n1. clock\n* not bold *", 1234)


def comment_page(comments, next_token=None, replies=None, action="reloadContinuationItemsCommand"):
    """A page from YouTube's comments API: (id, text, author, when, likes) each;
    `replies`: comment id -> the token for its replies."""
    replies = replies or {}
    items = []
    for cid, *_ in comments:
        view = {"commentViewModel": {"commentViewModel": {"commentId": cid}}}
        if "." in cid:
            items.append(view)
            continue
        thread = dict(view)
        if cid in replies:
            thread["replies"] = {"commentRepliesRenderer": {"subThreads": [{"continuationItemRenderer": {
                "continuationEndpoint": {"continuationCommand": {"token": replies[cid]}}}}]}}
        items.append({"commentThreadRenderer": thread})
    if next_token:
        items.append({"continuationItemRenderer": {"continuationEndpoint": {"continuationCommand": {"token": next_token}}}})
    mutations = [{"payload": {"commentEntityPayload": {
        "properties": {"commentId": cid, "content": {"content": text}, "publishedTime": when},
        "author": {"channelId": f"UC{author}", "displayName": f"@{author}"},
        "toolbar": {"likeCountNotliked": likes}}}} for cid, text, author, when, likes in comments]
    return {"onResponseReceivedEndpoints": [{action: {"continuationItems": items}}],
            "frameworkUpdates": {"entityBatchUpdate": {"mutations": mutations}}}


COMMENT_PAGES = {
    "TOP": comment_page([("Ugw1", "First! see https://x.test/a_b", "ann", "2 days ago", "14K"),
                         ("Ugw2", "*nice* video", "bob", "1 hour ago (edited)", "")],
                        next_token="P2", replies={"Ugw1": "R1"}),
    "P2": comment_page([("Ugw3", "third", "cat", "3 hours ago", "5")]),
    "R1": comment_page([("Ugw1.r1", "a reply", "dan", "1 day ago", "2")], action="appendContinuationItemsAction"),
}


class FakeYouTube:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.method == "POST" and request.url.path == "/youtubei/v1/next":
            ask = json.loads(request.content)
            assert ask["context"] == {"client": {"clientName": "WEB", "clientVersion": "2.20260922", "hl": "en"}}
            return httpx.Response(200, json=COMMENT_PAGES[ask["continuation"]])
        if request.url.path == "/watch" and request.url.params.get("v") == "dQw4w9WgXcQ":
            return httpx.Response(200, text=WATCH_PAGE, headers={"content-type": "text/html"})
        if request.url.path == "/feeds/videos.xml":  # YouTube's feeds, lately
            return httpx.Response(404, text="<html>Error 404</html>", headers={"content-type": "text/html"})
        if request.url.path == f"/channel/{CHANNEL}/videos":
            return httpx.Response(200, text=VIDEOS_PAGE, headers={"content-type": "text/html"})
        if request.url.path == "/playlist":
            return httpx.Response(200, text=PLAYLIST_PAGE, headers={"content-type": "text/html"})
        if request.url.path == "/@BenEater":
            return httpx.Response(200, text=PAGE, headers={"content-type": "text/html"})
        if request.url.path == "/oembed" and request.url.params.get("url") == VIDEO:
            return httpx.Response(200, json={"title": "Never Gonna Give You Up", "author_name": "Rick Astley"})
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
    page = client.get(f"/t/{tid}").text  # YouTube's player, and below it the button to save it
    assert '<div class="video-player" data-embed="https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ"' in page
    assert page.index("video-player") < page.index('action="/youtube/v/dQw4w9WgXcQ/save"')
    assert "Download and archive" in page and "Kept post" not in page  # (the post is its own)
    assert f'class="act read-post" href="/t/{tid}#post-text" title="Show the video' in client.get(f"/c/{cid}").text

    with bouncer.db.transaction() as conn:
        conn.execute("UPDATE archived_threads SET retention='manual' WHERE id=?", (tid,))
    page = client.get(f"/t/{tid}").text  # kept: it's on its way
    assert "Downloading…" in page and "Download and archive" not in page and "data-video-saving" in page
    bouncer.media.fetch_pending()
    assert [(u, n) for u, n, _ in downloads] == [(VIDEO, youtube.DEFAULT_MAX_MB * 1_000_000)]
    m = video_media(bouncer)
    assert (m["status"], m["content_type"]) == ("ok", "video/mp4")
    assert (bouncer.media_dir / m["storage_path"]).read_bytes() == MP4
    page = client.get(f"/t/{tid}").text
    assert f'<video class="media" src="/media/{m["id"]}"' in page
    assert "controls playsinline preload" in page  # with sound, no loop
    for view in ("list", "pictures", "tiles"):  # its text button shows the player too (app.js)
        feed = client.get(f"/c/{cid}?view={view}").text
        assert f'class="act read-post" href="/t/{tid}#post-text" title="Show the video' in feed, view
    assert "data-embed" not in page and "Download and archive" not in page

    # With the thumbnail saved too, it stays the picture: in the feed, and as the player's cover.
    thumb = one(bouncer, "SELECT id FROM media WHERE url='https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg'")[0]
    with bouncer.db.transaction() as conn:
        conn.execute("UPDATE media SET status='ok', content_type='image/jpeg' WHERE id=?", (thumb,))
    for view in ("list", "pictures", "tiles"):
        feed = client.get(f"/c/{cid}?view={view}").text
        assert f'src="/media/{thumb}?w=' in feed and f'src="/media/{m["id"]}"' not in feed, view
    page = client.get(f"/t/{tid}").text
    assert f'<video class="media" src="/media/{m["id"]}" poster="/media/{thumb}" controls playsinline' in page


def test_youtube_videos_go_by_the_youtube_resolution(settings, bouncer, yt, monkeypatch):
    big = MP4 + b"\x00" * 1_500_000

    def fake_download(url, workdir, session, max_bytes):
        path = workdir / ".yt-test.mp4"
        workdir.mkdir(parents=True, exist_ok=True)
        path.write_bytes(big)
        return path, 1080

    heights = []

    def to_height(src, height, workdir):  # the downloads are 1080p
        heights.append(height)
        if height >= 1080:
            return None
        out = workdir / f".tc-yt{len(heights)}.mp4"
        out.write_bytes(MP4 + b"%dp %d" % (height, len(heights)))
        return out

    monkeypatch.setattr(youtube, "download", fake_download)
    monkeypatch.setattr(media.transcode, "to_height", to_height)
    monkeypatch.setattr(media.transcode, "at_bitrate", lambda *a: pytest.fail("not the Videos settings"))
    monkeypatch.setattr(media.transcode, "shrink", lambda *a: pytest.fail("not the Videos settings"))
    monkeypatch.setattr(media.MediaFetcher, "can_transcode", lambda self: True)
    cid = bouncer.follow_community(f"https://www.youtube.com/channel/{CHANNEL}", None, 30, backfill=True)
    bouncer.poll_follow(cid)
    client = logged_in(settings, bouncer)
    # The Videos settings would re-encode anything over 1 MB: YouTube's aren't theirs.
    client.post("/storage/media-defaults", data={"video_keep": "1", "video_bigger": "rate", "video_rate": "2"})
    with bouncer.db.transaction() as conn:
        conn.execute("UPDATE archived_threads SET retention='manual'")
    bouncer.media.fetch_pending()
    while bouncer.media.convert_some():
        pass
    m = video_media(bouncer)
    assert (m["status"], m["size_bytes"], m["original_bytes"]) == ("ok", len(big), None)
    assert heights == [1080, 1080]  # looked at, at the resolution they were saved at: left as they are
    saved = one(bouncer, "SELECT COUNT(*) FROM media WHERE kept_only=1 AND status='ok'")[0]
    assert saved == 2

    # A lower resolution on the YouTube page scales down the ones saved at more than it.
    r = client.post("/youtube/limits", data={"max_mb": "2000", "max_height": "720"})
    assert "saved at more than 720p are scaled down to it" in r.text and '<option value="720" selected>' in r.text
    while bouncer.media.convert_some():
        pass
    assert heights == [1080, 1080, 720, 720]
    m = video_media(bouncer)
    assert m["size_bytes"] < 1000 and m["original_bytes"] == len(big) and m["content_type"] == "video/mp4"
    page = client.get("/storage").text
    assert "Recently transcoded" in page


def test_communities_saving_only_pictures_skip_videos(bouncer, yt, downloads):
    cid = bouncer.follow_community(f"https://www.youtube.com/channel/{CHANNEL}", None, 30, backfill=True)
    bouncer.poll_follow(cid)
    with bouncer.db.transaction() as conn:
        conn.execute("""UPDATE communities SET media_policy='{"video": {"save": false}}' WHERE id=?""", (cid,))
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


def test_live_stream_links_are_not_saved(server, bouncer, downloads):
    from .conftest import DOMAIN
    live = "https://www.youtube.com/live/dQw4w9WgXcQ?si=x"
    assert youtube.saved_video_id(live) is None and youtube.saved_video_id(VIDEO) == "dQw4w9WgXcQ"
    server.add_post("1", "streaming now", "")
    server.edit_post("1", url=live)
    bouncer.ingest_url(f"https://{DOMAIN}/post/1")  # kept by link
    bouncer.media.fetch_pending()
    assert downloads == [] and one(bouncer, "SELECT 1 FROM media WHERE url=?", live) is None

    # One an older version registered isn't saved either.
    with bouncer.db.transaction() as conn:
        mid = media.media_id(conn, live, utcnow())
        media.register_youtube_links(conn)
    assert one(bouncer, "SELECT status FROM media WHERE id=?", mid)[0] == "skipped"


def test_a_video_that_is_live_now_is_not_recorded(bouncer, monkeypatch, tmp_path):
    fetched = []

    class FakeYDL:
        def __init__(self, opts):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def extract_info(self, url, download=True, process=True):
            return {"id": "dQw4w9WgXcQ", "live_status": "is_live", "is_live": True}

        def process_ie_result(self, info, download=True):
            fetched.append(info)
            return info

    import yt_dlp
    monkeypatch.setattr(yt_dlp, "YoutubeDL", FakeYDL)
    with pytest.raises(youtube.StillLive):
        youtube.download(VIDEO, tmp_path / "media", bouncer.youtube, 10_000_000)
    assert fetched == [] and not list((tmp_path / "media").iterdir())  # tried again once it's over (a VideoRetry)
    assert issubclass(youtube.StillLive, youtube.VideoRetry)


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


# -- previews in the feed ----------------------------------------------------------------------
def test_the_watch_page_parser():
    watch = youtube.parse_watch(WATCH_PAGE)
    assert watch.likes == 1234
    assert watch.description == ("Building a computer.\\\nParts: <https://eater.net/6502_kit>\n\n"
                                 "1\\. clock\\\n\\* not bold \\*")
    assert (watch.title, watch.channel, watch.channel_id, watch.published) == (
        "Hello, world from a 6502", "Ben Eater", CHANNEL, "2026-09-01T07:00:00-07:00")
    labelled = '<script>var ytInitialPlayerResponse = {"videoDetails": {}};</script>' \
               '"accessibilityText":"like this video along with 5,678 other people"'
    assert youtube.parse_watch(labelled) == youtube.Watch(None, 5678)
    assert youtube.parse_watch("<html>no data</html>") is None


def test_videos_scrolled_to_get_their_description_and_likes(settings, bouncer, yt):
    """A video's post shown in the feed has its description and likes read
    from its page once it's on screen (app.js), and not again for a while."""
    cid = bouncer.follow_community(f"https://www.youtube.com/channel/{CHANNEL}", None, 30, backfill=True)
    bouncer.poll_follow(cid)
    tid = one(bouncer, "SELECT t.id FROM archived_threads t JOIN objects o ON o.id=t.root_object_id "
                       "WHERE o.canonical_ap_id='rss:yt:video:dQw4w9WgXcQ'")[0]
    client = logged_in(settings, bouncer)
    assert 'data-preview=""' in client.get(f"/c/{cid}").text

    def watches():
        return sum(1 for r in yt.requests if r.url.path == "/watch")

    client.post("/feed/articles", data={"ids": [tid]})
    while bouncer.run_one_job():
        pass
    assert watches() == 1
    row = one(bouncer, "SELECT o.score, o.upvotes, o.downvotes, o.revision_count, r.body FROM objects o "
                       "JOIN revisions r ON r.object_id=o.id WHERE o.canonical_ap_id='rss:yt:video:dQw4w9WgXcQ'")
    assert row["body"].startswith("Building a computer.") and row["revision_count"] == 1  # not an edit
    assert (row["score"], row["upvotes"], row["downvotes"]) == (1234, 1234, None)
    assert client.get("/feed/articles", params={"ids": [tid]}).json()["previewed"][str(tid)]
    page = client.get(f"/c/{cid}").text
    # The start of the description, and the Post text button that shows it whole, as for any post.
    assert "Building a computer. Parts: https://eater.net/6502_kit" in page and "▲ 1234" in page
    assert f'class="act read-post" href="/t/{tid}#post-text"' in page
    assert f'class="act read-post" href="/t/{tid}#post-text"' in client.get(f"/c/{cid}?view=pictures").text
    tiles = client.get(f"/c/{cid}?view=tiles").text
    assert "▲ 1234" in tiles and "Add an account to vote" not in tiles  # likes are shown, not voted on

    client.post("/feed/articles", data={"ids": [tid]})
    while bouncer.run_one_job():
        pass
    assert watches() == 1  # read moments ago
    thread = client.get(f"/t/{tid}").text  # the post page: the whole description, its line breaks and links
    assert "Building a computer.<br>" in thread and 'href="https://eater.net/6502_kit"' in thread
    assert "* not bold *" in thread


def test_comments_are_read_when_a_video_is_opened(settings, bouncer, yt):
    """Opening a video's post (its page, or its comments in the feed) reads its
    top comments and some replies from YouTube's comments API, as the video's
    page would; not again within a few minutes."""
    cid = bouncer.follow_community(f"https://www.youtube.com/channel/{CHANNEL}", None, 30, backfill=True)
    bouncer.poll_follow(cid)
    tid = one(bouncer, "SELECT t.id FROM archived_threads t JOIN objects o ON o.id=t.root_object_id "
                       "WHERE o.canonical_ap_id='rss:yt:video:dQw4w9WgXcQ'")[0]
    client = logged_in(settings, bouncer)

    def asks():
        return [r.url.path for r in yt.requests if r.url.path in ("/watch", "/youtubei/v1/next")]

    page = client.get(f"/t/{tid}?inline=1").text
    assert 'id="refreshing"' in page and "Checking for new comments" in page and "hidden>" not in page
    while bouncer.run_one_job():
        pass
    assert asks() == ["/watch"] + ["/youtubei/v1/next"] * 3  # two pages of comments, one thread's replies
    with bouncer.db.connect() as conn:
        rows = {r["canonical_ap_id"]: r for r in conn.execute(
            "SELECT o.canonical_ap_id, o.parent_id, o.score, r.body, a.username FROM objects o "
            "JOIN revisions r ON r.object_id=o.id JOIN actors a ON a.id=o.author_id WHERE o.object_type='comment'")}
    assert set(rows) == {f"rss:yt:comment:{c}" for c in ("Ugw1", "Ugw2", "Ugw3", "Ugw1.r1")}
    first, reply = rows["rss:yt:comment:Ugw1"], rows["rss:yt:comment:Ugw1.r1"]
    assert first["score"] == 14000 and first["username"] == "@ann"
    assert first["body"] == "First! see <https://x.test/a_b>"
    assert rows["rss:yt:comment:Ugw2"]["body"] == "\*nice\* video"
    assert reply["parent_id"] == one(bouncer, "SELECT id FROM objects WHERE canonical_ap_id='rss:yt:comment:Ugw1'")[0]
    # The same page read refreshed the video's description and likes.
    assert one(bouncer, "SELECT upvotes FROM objects WHERE canonical_ap_id='rss:yt:video:dQw4w9WgXcQ'")[0] == 1234

    page = client.get(f"/t/{tid}").text
    assert "4 comments" in page and "@ann" in page and "@ann@" not in page and "*nice* video" in page
    assert 'href="https://x.test/a_b"' in page
    assert 'id="refreshing"' not in page  # read moments ago
    assert asks() == ["/watch"] + ["/youtubei/v1/next"] * 3
    feed = client.get(f"/c/{cid}").text
    assert "4 comments" in feed


def test_the_comments_parser():
    got = youtube.parse_comments(COMMENT_PAGES["TOP"], now=datetime(2026, 9, 23, tzinfo=timezone.utc))
    assert [c.id for c in got.comments] == ["Ugw1", "Ugw2"] and got.next == "P2" and got.replies == {"Ugw1": "R1"}
    assert got.comments[0].published == datetime(2026, 9, 21, tzinfo=timezone.utc)
    assert (got.comments[1].likes, got.comments[1].edited) == (0, True)
    [reply] = youtube.parse_comments(COMMENT_PAGES["R1"]).comments
    assert reply.parent_id == "Ugw1"
    assert youtube.comments_token("<html>no data</html>") is None
    assert youtube.api_context(WATCH_PAGE) == {"client": {"clientName": "WEB", "clientVersion": "2.20260922",
                                                          "hl": "en"}}


# -- YouTube links in text: the video's title, and the box they open ---------------------------

def test_youtube_links_show_the_title_and_open_the_box():
    from threadbnc.render import render_markdown
    titles = {"dQw4w9WgXcQ": "Never Gonna Give You Up"}.get
    out = render_markdown(f"watch {VIDEO} and [my pick](https://youtu.be/dQw4w9WgXcQ) or https://youtu.be/gnmrKDpTM7o",
                          titles=titles)
    assert ('<a href="/youtube/v/dQw4w9WgXcQ" class="video-link" title="YouTube video: watch it here, '
            'or download and archive it" rel="noopener noreferrer nofollow">'
            'Never Gonna Give You Up</a>') in out
    assert '>my pick</a>' in out  # written text stays
    assert 'class="video-link untitled"' in out and ">https://youtu.be/gnmrKDpTM7o</a>" in out  # app.js asks
    assert 'target="_blank"' not in out
    assert "https://example.com" in render_markdown("https://example.com")  # other links as before


def test_youtube_links_in_articles_open_the_box_too():
    from threadbnc import articles
    html = f'<p>See <a href="{VIDEO}">{VIDEO}</a> and <a href="https://blog.example/2026/05/post">this</a>.</p>'
    out = articles.render(html, lambda _u: None, "https://blog.example/a", lambda u: "/read?url=x",
                          {"dQw4w9WgXcQ": "Never Gonna Give You Up"}.get)
    assert '<a href="/youtube/v/dQw4w9WgXcQ" class="video-link"' in out and ">Never Gonna Give You Up</a>" in out


def test_titles_are_asked_of_youtube_once(settings, bouncer, yt):
    client = logged_in(settings, bouncer)
    got = client.get("/youtube/titles", params=[("ids", "dQw4w9WgXcQ"), ("ids", "gnmrKDpTM7o"),
                                                ("ids", "not an id")]).json()
    assert got == {"dQw4w9WgXcQ": "Never Gonna Give You Up"}
    asked = [r for r in yt.requests if r.url.path == "/oembed"]
    assert len(asked) == 2
    client.get("/youtube/titles", params=[("ids", "dQw4w9WgXcQ"), ("ids", "gnmrKDpTM7o")])
    assert len([r for r in yt.requests if r.url.path == "/oembed"]) == 2  # known, or asked lately


def test_the_box_says_how_big_the_video_is_and_saves_it(settings, bouncer, yt, downloads, monkeypatch):
    probes = []

    def fake_probe(url, session):
        probes.append(url)
        return youtube.Probe("Never Gonna Give You Up", "Rick Astley", 213, 123_400_000, False, 1080)

    monkeypatch.setattr(youtube, "probe", fake_probe)
    client = logged_in(settings, bouncer)
    box = client.get("/youtube/v/dQw4w9WgXcQ?pane=1").text  # at once: YouTube's player, then how big it is
    assert '<article class="video-box" id="video-dQw4w9WgXcQ"' in box and "<html" not in box
    assert '<div class="video-player" data-embed="https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ"' in box
    assert "Never Gonna Give You Up" in box and "Rick Astley" in box  # (its title asked of YouTube)
    assert "data-video-probe" in box and "Finding out how big the video is…" in box and probes == []
    box = client.get("/youtube/v/dQw4w9WgXcQ?pane=1&probe=1").text  # app.js asks
    assert "The video is <strong>123 MB</strong>, at 1080p." in box and "data-video-probe" not in box
    assert box.index("video-player") < box.index("The video is") < box.index('action="/youtube/v/dQw4w9WgXcQ/save"')
    assert "Download and archive" in box and f'href="{VIDEO}"' in box
    client.get("/youtube/v/dQw4w9WgXcQ")
    client.get("/youtube/v/dQw4w9WgXcQ?pane=1&probe=1")
    assert probes == [VIDEO]  # found once, then remembered
    assert client.get("/youtube/v/nope").status_code == 404

    client.post("/youtube/v/dQw4w9WgXcQ/save")
    m = video_media(bouncer)
    assert m["kept_only"] == 1 and m["wanted_at"] and m["status"] == "pending"
    assert "Downloading…" in client.get("/youtube/v/dQw4w9WgXcQ?pane=1").text
    bouncer.media.fetch_pending()
    assert [u for u, _, _ in downloads] == [VIDEO]
    m = video_media(bouncer)
    box = client.get("/youtube/v/dQw4w9WgXcQ?pane=1").text
    assert f'<video class="media" src="/media/{m["id"]}"' in box and "Download and archive" not in box
    # No post links to it, but it was asked for: it stays.
    with bouncer.db.transaction() as conn:
        media.collect_orphans(conn, bouncer.media_dir)
    assert video_media(bouncer)["status"] == "ok"


def test_a_failed_probe_still_offers_both(settings, bouncer, yt, monkeypatch):
    def fake_probe(url, session):
        raise youtube.NeedsSession("YouTube wants a signed-in session for this video.")

    monkeypatch.setattr(youtube, "probe", fake_probe)
    box = logged_in(settings, bouncer).get("/youtube/v/dQw4w9WgXcQ?pane=1&probe=1").text
    assert "Couldn't find out how big the video is: YouTube wants a signed-in session" in box
    assert "<h2>Never Gonna Give You Up</h2>" in box  # its title asked of YouTube instead
    assert "Download and archive" in box and "Watch on YouTube" in box


def test_a_post_linking_to_a_video_shows_its_player(settings, server, bouncer, yt):
    from .conftest import DOMAIN
    server.add_post("1", "neat video", "also https://youtu.be/gnmrKDpTM7o")
    server.edit_post("1", url="https://youtu.be/dQw4w9WgXcQ")
    tid = bouncer.ingest_url(f"https://{DOMAIN}/post/1")
    with bouncer.db.transaction() as conn:
        youtube.save_title(conn, "dQw4w9WgXcQ", "Never Gonna Give You Up", None, utcnow())
    page = logged_in(settings, bouncer).get(f"/t/{tid}").text
    assert ('<a href="https://youtu.be/dQw4w9WgXcQ" rel="noreferrer noopener nofollow" target="_blank">'
            'Never Gonna Give You Up</a>') in page  # its player is in the post
    assert '<div class="video-player" data-embed="https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ"' in page
    assert "data-video-probe" not in page  # (YouTube isn't asked how big it is)
    assert "Downloading…" in page and 'action="/youtube/v/dQw4w9WgXcQ/save"' not in page  # kept, so it's on its way
    assert '<a href="/youtube/v/gnmrKDpTM7o" class="video-link untitled"' in page  # one in its text opens its box


def test_videos_saved_from_links_are_under_kept_videos(settings, server, bouncer, yt, downloads, monkeypatch):
    monkeypatch.setattr(youtube, "probe", lambda url, session: youtube.Probe(
        "Never Gonna Give You Up", "Rick Astley", 213, 123_400_000, False, 1080))
    client = logged_in(settings, bouncer)
    assert "Saved from links" not in client.get("/kept?tab=videos").text
    client.get("/youtube/v/dQw4w9WgXcQ?pane=1")
    client.post("/youtube/v/dQw4w9WgXcQ/save")
    page = client.get("/kept?tab=videos").text
    assert "Saved from links" in page and "saving…" in page
    assert '<a class="video-link" href="/youtube/v/dQw4w9WgXcQ">Never Gonna Give You Up</a>' in page
    assert '<span class="count muted" aria-label="1 kept">1</span>' in page
    bouncer.media.fetch_pending()
    m = video_media(bouncer)
    page = client.get("/kept?tab=videos").text
    assert f'<video src="/media/{m["id"]}#t=1"' in page and "Rick Astley" in page and "4 min" in page

    # Once a kept post links to it, it's listed as that post instead.
    from .conftest import DOMAIN
    server.add_post("1", "neat video", "")
    server.edit_post("1", url=VIDEO)
    bouncer.ingest_url(f"https://{DOMAIN}/post/1")
    page = client.get("/kept?tab=videos").text
    assert "Saved from links" not in page and "neat video" in page
    assert '<span class="count muted" aria-label="1 kept">1</span>' in page


def video_post(bouncer):
    return one(bouncer, "SELECT t.*, c.canonical_ap_id AS c_ap, c.name AS c_name FROM archived_threads t "
                        "JOIN objects o ON o.id=t.root_object_id JOIN communities c ON c.id=t.community_id "
                        "WHERE o.canonical_ap_id='rss:yt:video:dQw4w9WgXcQ'")


def test_a_video_saved_from_a_link_is_kept_as_its_channels_post(settings, bouncer, yt, downloads, monkeypatch):
    """Saving a video from a link's box keeps it the way keeping a followed
    channel's video does: a post in its channel, with its description, likes
    and comments, whose video is the one downloaded."""
    monkeypatch.setattr(youtube, "probe", lambda url, session: youtube.Probe(
        "Never Gonna Give You Up", "Rick Astley", 213, 123_400_000, False, 1080))
    client = logged_in(settings, bouncer)
    client.post("/youtube/v/dQw4w9WgXcQ/save")
    while bouncer.run_one_job():
        pass
    t = video_post(bouncer)
    assert t["retention"] == "manual" and t["expires_at"] is None
    assert (t["c_ap"], t["c_name"]) == (f"rss:{FEED}", "Ben Eater")
    root = one(bouncer, "SELECT o.upvotes, r.title, r.url, r.body, o.created_at FROM objects o "
                        "JOIN revisions r ON r.object_id=o.id AND r.seq=o.revision_count WHERE o.id=?",
               t["root_object_id"])
    assert (root["title"], root["url"], root["upvotes"]) == ("Hello, world from a 6502", VIDEO, 1234)
    assert root["body"].startswith("Building a computer.") and root["created_at"].startswith("2026-09-01T14:00")
    assert one(bouncer, "SELECT COUNT(*) FROM objects WHERE thread_id=? AND object_type='comment'", t["id"])[0] == 4

    bouncer.media.fetch_pending()
    assert [u for u, _, _ in downloads] == [VIDEO]  # once: the post's video is the one asked for
    page = client.get("/kept?tab=videos").text
    assert "Saved from links" not in page and "Hello, world from a 6502" in page
    assert '<span class="count muted" aria-label="1 kept">1</span>' in page
    assert f'href="/t/{t["id"]}"' in client.get("/youtube/v/dQw4w9WgXcQ?pane=1").text

    # Following its channel later finds the same post, not another.
    cid = bouncer.follow_community(f"https://www.youtube.com/channel/{CHANNEL}", None, 30, backfill=True)
    bouncer.poll_follow(cid)
    assert one(bouncer, "SELECT COUNT(*) FROM objects WHERE canonical_ap_id='rss:yt:video:dQw4w9WgXcQ'")[0] == 1
    assert video_post(bouncer)["community_id"] == cid


def test_a_followed_channels_video_saved_from_a_link_keeps_its_post(settings, bouncer, yt, downloads):
    cid = bouncer.follow_community(f"https://www.youtube.com/channel/{CHANNEL}", None, 30, backfill=True)
    bouncer.poll_follow(cid)
    assert video_post(bouncer)["retention"] == "auto"
    logged_in(settings, bouncer).post("/youtube/v/dQw4w9WgXcQ/save")
    while bouncer.run_one_job():
        pass
    assert video_post(bouncer)["retention"] == "manual"
    assert one(bouncer, "SELECT COUNT(*) FROM archived_threads")[0] == 2  # the channel's two videos, no more


def test_videos_saved_from_links_before_are_kept_as_posts(settings, bouncer, yt, downloads):
    """Saved from a youtu.be link before this kept a post: the post is made
    once, and its video is the file already saved, not downloaded again."""
    with bouncer.db.transaction() as conn:
        mid = media.media_id(conn, "https://youtu.be/dQw4w9WgXcQ", utcnow())
        conn.execute("UPDATE media SET wanted_at=?, held=0 WHERE id=?", (utcnow(), mid))
    bouncer.media.fetch_pending()
    assert len(downloads) == 1
    bouncer._keep_saved_links()
    bouncer._keep_saved_links()  # asked once
    assert one(bouncer, "SELECT COUNT(*) FROM jobs WHERE kind='keep_youtube'")[0] == 1
    while bouncer.run_one_job():
        pass
    assert video_post(bouncer)["retention"] == "manual"
    bouncer.media.fetch_pending()
    assert len(downloads) == 1
    old, new = one(bouncer, "SELECT * FROM media WHERE id=?", mid), video_media(bouncer)
    assert new["status"] == "ok" and new["storage_path"] == old["storage_path"]
    with bouncer.db.connect() as conn:
        assert media.saved_from_links(conn) == []  # listed as the post, not besides it too
    assert "Saved from links" not in logged_in(settings, bouncer).get("/kept?tab=videos").text
