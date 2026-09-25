"""Video links (Vimeo, Dailymotion, Streamable, PeerTube, video files) open a box with the video's
player and a button to download and archive it; a post that is one shows the box in the post."""

from __future__ import annotations

from urllib.parse import quote

import httpx
import pytest
from fastapi.testclient import TestClient

from threadbnc import articles, media, youtube
from threadbnc.render import MediaInfo, render_markdown
from threadbnc.videos import Video, video_of
from threadbnc.web import create_app

from .conftest import DOMAIN

MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 100
SHORT = "9c9de5e8-0a1b-4e6f-8a3d-2f5b7c1d0e4f"


def test_links_that_are_videos():
    assert video_of("https://vimeo.com/76979871") == Video("vimeo", "76979871")
    assert video_of("https://vimeo.com/76979871/8272103f6e") == Video("vimeo", "76979871", secret="8272103f6e")
    assert video_of("https://player.vimeo.com/video/76979871?h=8272103f6e") == \
        Video("vimeo", "76979871", secret="8272103f6e")
    assert video_of("https://vimeo.com/channels/staffpicks/76979871") == Video("vimeo", "76979871")
    assert video_of("https://www.dailymotion.com/video/x8abc12_a-title") == Video("dailymotion", "x8abc12")
    assert video_of("https://dai.ly/x8abc12") == Video("dailymotion", "x8abc12")
    assert video_of("https://streamable.com/moo") == Video("streamable", "moo")
    assert video_of("https://streamable.com/e/dnd1") == Video("streamable", "dnd1")
    assert video_of("https://tube.example/w/9xDnpJbQmU7ooSa2HXWoXL") == \
        Video("peertube", "9xDnpJbQmU7ooSa2HXWoXL", host="tube.example")
    assert video_of(f"https://tube.example:8443/videos/watch/{SHORT}") == \
        Video("peertube", SHORT, host="tube.example:8443")
    assert video_of("https://x.test/clips/cat.mp4?x=1") == Video("file", "https://x.test/clips/cat.mp4?x=1")
    for url in ("https://vimeo.com/staffpicks", "https://vimeo.com/76979871/not-a-hash", "https://streamable.com/login",
                "https://www.dailymotion.com/us", "https://en.wikipedia.org/w/index.php",
                "https://tube.example/w/p/9xDnpJbQmU7ooSa2HXWoXL",  # a playlist
                "https://www.youtube.com/watch?v=dQw4w9WgXcQ", "https://www.twitch.tv/vinesauce",
                "https://x.test/cat.gif", "ftp://x.test/cat.mp4", "https://user:pw@vimeo.com/76979871", None):
        assert video_of(url) is None, url


def test_players():
    assert Video("vimeo", "76979871").embed == "https://player.vimeo.com/video/76979871?dnt=1"
    assert Video("vimeo", "1", secret="abcdef").embed.endswith("?dnt=1&h=abcdef")
    assert Video("vimeo", "1", secret="abcdef").url == "https://vimeo.com/1/abcdef"
    assert Video("dailymotion", "x8abc12").embed == "https://www.dailymotion.com/embed/video/x8abc12"
    assert Video("streamable", "moo").embed == "https://streamable.com/e/moo"
    pt = Video("peertube", SHORT, host="tube.example")
    assert pt.embed == f"https://tube.example/videos/embed/{SHORT}?p2p=0"
    assert (pt.url, pt.fetch_url) == (f"https://tube.example/w/{SHORT}", f"peertube:tube.example:{SHORT}")
    assert Video("vimeo", "1", secret="abcdef").fetch_url == "https://player.vimeo.com/video/1?h=abcdef"
    f = Video("file", "https://i.imgur.test/abc.gifv")
    assert f.embed is None and f.plays_from == "https://i.imgur.test/abc.mp4" and f.label == "abc.gifv"
    assert Video("file", "http://x.test/a.mp4").plays_from is None  # (only https plays on the page)
    assert Video("vimeo", "76979871").href == "/video?url=" + quote("https://vimeo.com/76979871", safe="")
    assert Video("vimeo", "76979871").box_id == video_of("https://player.vimeo.com/video/76979871").box_id


def test_video_links_open_their_box():
    out = render_markdown("see https://vimeo.com/76979871 and [this clip](https://x.test/cat.mp4)",
                          titles=lambda _v: None)
    box = "/video?url=" + quote("https://vimeo.com/76979871", safe="")
    assert (f'<a href="{box}" class="video-link" title="Vimeo video: watch it here, or download and archive it" '
            f'rel="noopener noreferrer nofollow">https://vimeo.com/76979871</a>') in out
    assert f'href="/video?url={quote("https://x.test/cat.mp4", safe="")}" class="video-link"' in out
    assert ">this clip</a>" in out and 'target="_blank"' not in out
    # A video "embedded" that isn't saved: its box, to play it from where it is.
    out = render_markdown("![a cat](https://x.test/cat.mp4)", lambda _u: MediaInfo(1, "pending", None),
                          titles=lambda _v: None)
    assert 'class="video-link"' in out and "[video: a cat · x.test]" in out
    saved = render_markdown("![a cat](https://x.test/cat.mp4)", lambda _u: MediaInfo(1, "ok", "video/mp4"),
                            titles=lambda _v: None)
    assert '<video class="media" src="/media/1"' in saved
    html = '<p>Watch <a href="https://streamable.com/moo" target="_blank">this</a>.</p>'
    out = articles.render(html, lambda _u: None, "https://blog.example/a", None, None)
    assert f'href="/video?url={quote("https://streamable.com/moo", safe="")}" class="video-link"' in out
    assert "_blank" not in out
    assert articles.candidate("https://tube.example/w/9xDnpJbQmU7ooSa2HXWoXL") is None  # not read as an article


def site(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/clip.mp4":
        return httpx.Response(200, content=MP4, headers={"content-type": "video/mp4"})
    return httpx.Response(404)


@pytest.fixture
def vbouncer(bouncer, monkeypatch):
    bouncer.media = media.MediaFetcher(bouncer.db, bouncer.media_dir, "t", 1_000_000,
                                       client=httpx.Client(transport=httpx.MockTransport(site)),
                                       check_host=False, youtube_session=bouncer.youtube)
    bouncer.downloads = []

    def fake_download(url, workdir, session, max_bytes):
        bouncer.downloads.append((url, max_bytes))
        path = workdir / ".yt-test.mp4"
        workdir.mkdir(parents=True, exist_ok=True)
        path.write_bytes(MP4)
        return path, 720

    monkeypatch.setattr(youtube, "download", fake_download)
    return bouncer


def logged_in(settings, b) -> TestClient:
    client = TestClient(create_app(settings, b))
    client.post("/login", data={"password": "pw"})
    return client


def one(b, sql, *args):
    with b.db.connect() as conn:
        return conn.execute(sql, args).fetchone()


def test_the_box_plays_a_sites_video_and_saves_it(settings, vbouncer):
    client = logged_in(settings, vbouncer)
    url = "https://player.vimeo.com/video/76979871"
    box = client.get("/video", params={"url": url, "pane": "1"}).text
    assert '<article class="video-box" id="video-vimeo-' in box and "<html" not in box
    assert '<div class="video-player" data-embed="https://player.vimeo.com/video/76979871?dnt=1"' in box
    assert box.index("video-player") < box.index('action="/video/save"')
    assert '<input type="hidden" name="url" value="https://vimeo.com/76979871">' in box
    assert "Download and archive" in box and 'href="https://vimeo.com/76979871"' in box and "Watch on Vimeo" in box
    assert "<html" in client.get("/video", params={"url": url}).text
    assert client.get("/video", params={"url": "https://example.com/page"}).status_code == 404
    assert "media-src 'self' https:" in client.get("/video", params={"url": url}).headers["content-security-policy"]

    client.post("/video/save", data={"url": url})
    m = one(vbouncer, "SELECT * FROM media WHERE url='https://vimeo.com/76979871'")
    assert (m["kept_only"], m["held"], m["status"]) == (1, 0, "pending") and m["wanted_at"]
    assert "Downloading…" in client.get("/video", params={"url": url, "pane": "1"}).text
    vbouncer.media.fetch_pending()
    # (from Vimeo's player: its own pages want you signed in)
    assert vbouncer.downloads == [("https://player.vimeo.com/video/76979871", youtube.DEFAULT_MAX_MB * 1_000_000)]
    m = one(vbouncer, "SELECT * FROM media WHERE id=?", m["id"])
    assert (m["status"], m["content_type"]) == ("ok", "video/mp4")
    box = client.get("/video", params={"url": url, "pane": "1"}).text
    assert f'<video class="media" src="/media/{m["id"]}"' in box and "data-embed" not in box
    assert "Saved here" in box and "Download and archive" not in box and "Archive" not in box
    # No post links to it, but it was asked for: it stays, and it's on the Kept page.
    with vbouncer.db.transaction() as conn:
        media.collect_orphans(conn, vbouncer.media_dir)
    assert one(vbouncer, "SELECT status FROM media WHERE id=?", m["id"])[0] == "ok"
    kept = client.get("/kept?tab=videos").text
    assert "Saved from links" in kept and '<span class="pc-community">Vimeo</span>' in kept
    assert f'<a class="video-link" href="/video?url={quote("https://vimeo.com/76979871", safe="")}">' \
           "vimeo.com/76979871</a>" in kept


def test_a_peertube_video_is_asked_of_its_server(settings, vbouncer):
    client = logged_in(settings, vbouncer)
    client.post("/video/save", data={"url": f"https://tube.example/videos/watch/{SHORT}"})
    vbouncer.media.fetch_pending()
    assert [u for u, _ in vbouncer.downloads] == [f"peertube:tube.example:{SHORT}"]


def test_youtubes_session_is_only_sent_to_youtube(vbouncer):
    vbouncer.youtube.save_session("SID=abc; HSID=def", "")
    assert "cookiefile" in youtube._options(vbouncer.youtube, "https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    assert "cookiefile" not in youtube._options(vbouncer.youtube, "https://vimeo.com/76979871")
    assert "cookiefile" not in youtube._options(vbouncer.youtube, f"peertube:tube.example:{SHORT}")


def test_without_ffmpeg_only_whole_files_are_downloaded(vbouncer, monkeypatch):
    """A stream of pieces (HLS) comes out as MPEG-TS without ffmpeg, which browsers can't play."""
    monkeypatch.setattr(youtube.shutil, "which", lambda _name: None)
    fmt = youtube._options(vbouncer.youtube, "https://www.dailymotion.com/video/x8abc12")["format"]
    assert fmt == "b[height<=1080][protocol^=http][protocol!*=dash]/b[protocol^=http][protocol!*=dash]"
    gone = youtube._failure(Exception("ERROR: [dailymotion] x8abc12: Requested format is not available. Use "
                                      "--list-formats"), False, "https://www.dailymotion.com/video/x8abc12")
    assert isinstance(gone, youtube.VideoGone) and "needs ffmpeg" in str(gone)
    monkeypatch.setattr(youtube.shutil, "which", lambda _name: "/usr/bin/ffmpeg")
    assert youtube._options(vbouncer.youtube, "https://vimeo.com/1")["format"].startswith("bv*[height<=1080]+ba/")


def test_a_video_file_plays_from_where_it_is_until_it_is_saved(settings, vbouncer):
    client = logged_in(settings, vbouncer)
    url = "https://files.test/clip.mp4"
    box = client.get("/video", params={"url": url, "pane": "1"}).text
    assert f'<video class="media" src="{url}" controls playsinline preload="none">' in box
    assert "Download and archive" in box and "Open the file" in box
    # Saved whatever the community would save (nothing links to it here), and kept.
    with vbouncer.db.transaction() as conn:
        media.save_defaults(conn, {"video": {"save": False}})
    client.post("/video/save", data={"url": url})
    vbouncer.media.fetch_pending()
    m = one(vbouncer, "SELECT * FROM media WHERE url=?", url)
    assert (m["kept_only"], m["status"], m["content_type"]) == (0, "ok", "video/mp4") and m["wanted_at"]
    assert vbouncer.downloads == []  # (not with yt-dlp)
    box = client.get("/video", params={"url": url, "pane": "1"}).text
    assert f'<video class="media" src="/media/{m["id"]}"' in box and "Saved here" in box
    with vbouncer.db.transaction() as conn:
        media.collect_orphans(conn, vbouncer.media_dir)
    assert one(vbouncer, "SELECT status FROM media WHERE id=?", m["id"])[0] == "ok"


def test_a_post_that_is_a_videos_link_shows_its_player(settings, server, vbouncer):
    server.add_post("1", "neat video", "have a look")
    server.edit_post("1", url="https://vimeo.com/76979871")
    server.add_post("2", "a clip", "")
    server.edit_post("2", url="https://files.test/clip.mp4")
    vimeo = vbouncer.ingest_url(f"https://{DOMAIN}/post/1")
    clip = vbouncer.ingest_url(f"https://{DOMAIN}/post/2")
    client = logged_in(settings, vbouncer)
    page = client.get(f"/t/{vimeo}").text
    assert '<article class="video-box bare"' in page and 'data-embed="https://player.vimeo.com/video/76979871?dnt=1"' in page
    assert page.index("data-embed") < page.index('action="/video/save"') and "Read article" not in page
    assert '<a href="https://vimeo.com/76979871" rel="noreferrer noopener nofollow" target="_blank">' in page
    cid = one(vbouncer, "SELECT community_id FROM archived_threads WHERE id=?", vimeo)[0]
    feed = client.get(f"/c/{cid}").text
    assert f'class="act read-post" href="/t/{vimeo}#post-text" title="Show the video' in feed
    page = client.get(f"/t/{clip}").text  # (opening it downloads it, as it did)
    assert '<video class="media" src="https://files.test/clip.mp4" controls playsinline preload="none">' in page
    vbouncer.media.fetch_pending()
    m = one(vbouncer, "SELECT * FROM media WHERE url='https://files.test/clip.mp4'")
    assert m["status"] == "ok" and not m["wanted_at"]
    page = client.get(f"/t/{clip}").text
    assert f'<video class="media" src="/media/{m["id"]}" controls playsinline preload="metadata" loop muted>' in page
    assert "unless you archive it" in page and "Archive</button>" in page  # it goes with the post otherwise
    client.post("/video/save", data={"url": "https://files.test/clip.mp4"})
    page = client.get(f"/t/{clip}").text
    assert "unless you archive it" not in page and "Archive</button>" not in page
