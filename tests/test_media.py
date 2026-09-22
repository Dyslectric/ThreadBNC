from __future__ import annotations

import re

import httpx
import pytest
from fastapi.testclient import TestClient

from threadbnc import media, transcode
from threadbnc.db import utcnow
from threadbnc.render import MediaInfo, extract_media_urls, render_markdown
from threadbnc.web import create_app

from .conftest import DOMAIN

GIF = b"GIF89a" + b"\x00" * 50
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 50


def fake_remote(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/cat.gif":
        return httpx.Response(200, content=GIF, headers={"content-type": "image/gif"})
    if path == "/octet":
        return httpx.Response(200, content=PNG, headers={"content-type": "application/octet-stream"})
    if path == "/moved.png":
        return httpx.Response(302, headers={"location": "/cat.gif"})
    if path == "/article":
        return httpx.Response(200, content=b"<html>", headers={"content-type": "text/html"})
    if path == "/huge.png":
        return httpx.Response(200, content=PNG, headers={"content-type": "image/png", "content-length": "999999999"})
    return httpx.Response(404)


@pytest.fixture
def mbouncer(bouncer, tmp_path):
    bouncer.media = media.MediaFetcher(bouncer.db, bouncer.media_dir, "t", 10_000_000,
                                       client=httpx.Client(transport=httpx.MockTransport(fake_remote)),
                                       check_host=False)
    return bouncer


def media_rows(b):
    with b.db.connect() as conn:
        return {r["url"]: r for r in conn.execute("SELECT * FROM media")}


def test_extract_media_urls():
    text = "![a](https://x.test/a.png) [clip](https://x.test/v.mp4) [page](https://x.test/page) https://x.test/b.gif"
    assert extract_media_urls(text) == ["https://x.test/a.png", "https://x.test/v.mp4", "https://x.test/b.gif"]


def test_render_is_sanitised():
    out = str(render_markdown("<img src=x onerror=alert(1)> [x](javascript:alert(1)) **ok**"))
    assert "<img" not in out and "javascript:" not in out.replace("[x](javascript:alert(1))", "")
    assert "<strong>ok</strong>" in out


def test_render_uses_archived_copy():
    info = MediaInfo(7, "ok", "image/gif")
    out = str(render_markdown("![cat](https://x.test/cat.gif)", {"https://x.test/cat.gif": info}.get))
    assert 'src="/media/7"' in out and "x.test/cat.gif" not in out


def test_images_archived_and_survive_edits(server, mbouncer):
    server.add_post("1", "pics", "![cat](https://img.test/cat.gif)")
    server.add_comment("1", "10", "look https://img.test/moved.png and [bin](https://img.test/octet.png)")
    tid = mbouncer.ingest_url(f"https://{DOMAIN}/post/1")
    mbouncer.media.fetch_pending()
    rows = media_rows(mbouncer)
    assert rows["https://img.test/cat.gif"]["status"] == "ok"
    assert rows["https://img.test/moved.png"]["status"] == "ok"
    # same bytes -> same stored file
    assert rows["https://img.test/moved.png"]["storage_path"] == rows["https://img.test/cat.gif"]["storage_path"]
    assert (mbouncer.media_dir / rows["https://img.test/cat.gif"]["storage_path"]).read_bytes() == GIF

    # Author edits the image out: the archived copy stays referenced by history.
    server.edit_post("1", body="no more pics")
    mbouncer.sync_thread(tid)
    assert media_rows(mbouncer)["https://img.test/cat.gif"]["status"] == "ok"


def test_non_media_and_oversized(server, mbouncer):
    p = server.add_post("1", "article", "![big](https://img.test/huge.png)")
    server.edit_post("1", url="https://img.test/article")
    mbouncer.ingest_url(f"https://{DOMAIN}/post/1")
    mbouncer.media.fetch_pending()
    rows = media_rows(mbouncer)
    assert "https://img.test/article" not in rows  # article links are never fetched
    assert rows["https://img.test/huge.png"]["status"] == "failed"


def test_private_addresses_refused():
    with pytest.raises(media.MediaRejected):
        media._assert_public_host("http://127.0.0.1/x.png")
    with pytest.raises(media.MediaRejected):
        media._assert_public_host("http://192.168.1.10/x.png")


def test_purge_removes_only_unshared_media(server, mbouncer):
    server.add_post("1", "kept", "![a](https://img.test/cat.gif)", created=utcnow())
    mbouncer.ingest_url(f"https://{DOMAIN}/post/1")
    cid = mbouncer.follow_community(f"!math@{DOMAIN}", 10, 1)
    server.add_post("2", "auto", "![a](https://img.test/cat.gif) ![b](https://img.test/octet)", created=utcnow())
    mbouncer.poll_follow(cid)
    mbouncer.media.fetch_pending()
    rows = media_rows(mbouncer)
    shared, own = rows["https://img.test/cat.gif"], rows["https://img.test/octet"]
    with mbouncer.db.transaction() as conn:
        conn.execute("UPDATE archived_threads SET expires_at='2000-01-01T00:00:00.000000Z' WHERE retention='auto'")
    assert mbouncer.purge_expired() == 1
    rows = media_rows(mbouncer)
    assert "https://img.test/cat.gif" in rows and "https://img.test/octet" not in rows
    assert (mbouncer.media_dir / shared["storage_path"]).exists()
    assert not (mbouncer.media_dir / own["storage_path"]).exists()


def test_media_route_requires_auth(settings, server, mbouncer):
    server.add_post("1", "pics", "![cat](https://img.test/cat.gif)")
    tid = mbouncer.ingest_url(f"https://{DOMAIN}/post/1")
    mbouncer.media.fetch_pending()
    mid = media_rows(mbouncer)["https://img.test/cat.gif"]["id"]
    client = TestClient(create_app(settings, mbouncer))
    assert client.get(f"/media/{mid}", follow_redirects=False).status_code == 303
    client.post("/login", data={"password": "pw"})
    r = client.get(f"/media/{mid}")
    assert r.status_code == 200 and r.content == GIF and r.headers["content-type"] == "image/gif"
    assert "sandbox" in r.headers["content-security-policy"]
    page = client.get(f"/t/{tid}").text
    assert f'src="/media/{mid}"' in page


def test_article_preview_thumbnail(settings, server, mbouncer):
    server.add_post("1", "An article", "")
    server.edit_post("1", url="https://img.test/article", thumbnail_url="https://img.test/cat.gif")
    cid = mbouncer.follow_community(f"!math@{DOMAIN}", 10, 7, backfill=True)
    mbouncer.poll_follow(cid)
    mbouncer.media.fetch_pending()
    rows = media_rows(mbouncer)
    assert "https://img.test/article" not in rows
    assert rows["https://img.test/cat.gif"]["status"] == "ok"
    mid = rows["https://img.test/cat.gif"]["id"]
    client = TestClient(create_app(settings, mbouncer))
    client.post("/login", data={"password": "pw"})
    feed_html = client.get("/").text
    assert f'<img src="/media/{mid}"' in feed_html and 'img.test<svg class="i"' in feed_html
    tid = client.get("/").text.split('href="/t/')[1].split('"')[0]
    thread_html = client.get(f"/t/{tid}").text
    assert 'class="link-preview"' in thread_html and f'/media/{mid}' in thread_html


def test_media_downloads_are_spaced_per_host(server, mbouncer):
    import time
    from threadbnc.adapters.http import HostThrottle
    mbouncer.media.throttle = HostThrottle(0.3)
    server.add_post("1", "pics", "![a](https://img.test/cat.gif) ![b](https://img.test/octet) "
                                 "![c](https://other.test/cat.gif)")
    mbouncer.ingest_url(f"https://{DOMAIN}/post/1")
    start = time.monotonic()
    mbouncer.media.fetch_pending()
    # two requests to img.test must be >= 0.3s apart; other.test isn't delayed by them
    assert 0.3 <= time.monotonic() - start < 0.9


def test_pending_article_links_from_before_are_skipped(server, mbouncer):
    server.add_post("1", "article", "![e](https://img.test/embedded)")
    tid = mbouncer.ingest_url(f"https://{DOMAIN}/post/1")
    with mbouncer.db.transaction() as conn:  # simulate a row left by the old behaviour
        conn.execute("INSERT INTO media(url, first_seen_at) VALUES ('https://news.test/story', '2026-01-01')")
        assert media.skip_unprobed_links(conn) == 1
    rows = media_rows(mbouncer)
    assert rows["https://news.test/story"]["status"] == "skipped"
    assert rows["https://img.test/embedded"]["status"] == "pending"  # embedded images still fetched


def picture_community(server, bouncer, pictures=4, texts=0):
    for k in range(pictures):
        server.add_post(str(k + 1), f"picture {k + 1}", "")
        server.edit_post(str(k + 1), url=f"https://img.test/cat.gif?n={k}", created_at=utcnow())  # distinct links
    for k in range(texts):
        server.add_post(str(100 + k), f"text {k + 1}", "just words", created=utcnow())
    cid = bouncer.follow_community(f"!math@{DOMAIN}", 10, 7, backfill=True)
    bouncer.poll_follow(cid)
    bouncer.media.fetch_pending()
    return cid


def test_image_communities_show_as_tiles_automatically(settings, server, mbouncer):
    cid = picture_community(server, mbouncer)
    client = TestClient(create_app(settings, mbouncer))
    client.post("/login", data={"password": "pw"})
    page = client.get(f"/c/{cid}").text
    assert 'class="tiles"' in page and page.count('class="tile ') == 4 and 'class="post-card' not in page
    assert 'aria-label="Pictures in a grid" aria-current="true"' in page
    assert 'view=auto"><span class="tick"><svg' in page  # auto, not chosen
    home = client.get("/").text
    assert 'class="tiles"' in home


def test_mostly_text_communities_stay_a_list(settings, server, mbouncer):
    cid = picture_community(server, mbouncer, pictures=2, texts=3)
    client = TestClient(create_app(settings, mbouncer))
    client.post("/login", data={"password": "pw"})
    assert 'class="tiles"' not in client.get(f"/c/{cid}").text


def test_view_choice_is_remembered_per_community(settings, server, mbouncer):
    cid = picture_community(server, mbouncer)
    client = TestClient(create_app(settings, mbouncer))
    client.post("/login", data={"password": "pw"})
    assert 'class="tiles"' not in client.get(f"/c/{cid}?view=list").text
    assert 'class="tiles"' not in client.get(f"/c/{cid}?tab=kept").text  # remembered, kept tab too
    assert 'class="tiles"' in client.get("/").text  # the home feed has its own setting
    client.get("/?view=list")
    assert 'class="tiles"' not in client.get("/").text
    client.get(f"/c/{cid}?view=auto")
    assert 'class="tiles"' in client.get(f"/c/{cid}").text


def test_pictures_view_is_a_third_choice(settings, server, mbouncer):
    cid = picture_community(server, mbouncer)
    client = TestClient(create_app(settings, mbouncer))
    client.post("/login", data={"password": "pw"})
    page = client.get(f"/c/{cid}?view=pictures").text
    assert 'class="pictures"' in page and page.count('class="picture-card ') == 4
    assert 'class="pictures"' in client.get(f"/c/{cid}").text  # remembered


def test_posts_with_several_pictures_can_be_paged_through(settings, server, mbouncer):
    server.add_post("1", "album", "![a](https://img.test/cat.gif?a) text ![b](https://img.test/cat.gif?b) "
                                  "![c](https://img.test/cat.gif?c)", created=utcnow())
    server.add_post("2", "single", "![a](https://img.test/cat.gif?single)", created=utcnow())
    cid = mbouncer.follow_community(f"!math@{DOMAIN}", 10, 7, backfill=True)
    mbouncer.poll_follow(cid)
    mbouncer.media.fetch_pending()
    client = TestClient(create_app(settings, mbouncer))
    client.post("/login", data={"password": "pw"})
    rows = media_rows(mbouncer)
    album = [rows[u]["id"] for u in ("https://img.test/cat.gif?a", "https://img.test/cat.gif?b", "https://img.test/cat.gif?c")]
    for view in ("tiles", "pictures"):
        page = client.get(f"/c/{cid}?view={view}").text
        assert page.count("data-gallery") == 1 and '<span data-at>1</span>/3' in page
        srcs = [int(m) for m in re.findall(r'<img src="/media/(\d+)"', page)]
        assert [s for s in srcs if s in album] == album  # in the order they appear in the post
    assert 'title="3 pictures"' in client.get(f"/c/{cid}?view=list").text


def test_nsfw_tiles_are_veiled(settings, server, mbouncer):
    cid = picture_community(server, mbouncer)
    server.edit_post("1", metadata={"nsfw": True})
    tid = mbouncer.ingest_url(f"https://{DOMAIN}/post/1")
    mbouncer.sync_thread(tid, force=True)
    client = TestClient(create_app(settings, mbouncer))
    client.post("/login", data={"password": "pw"})
    page = client.get(f"/c/{cid}").text
    assert page.count(" veiled") == 1 and '<span class="tile-veil">NSFW<span class="small">Tap to show</span></span>' in page



# --- per-community settings and transcoding ---------------------------------

needs_ffmpeg = pytest.mark.skipif(not transcode.available(), reason="ffmpeg not installed")
BIG: dict[str, tuple[bytes, str]] = {}  # path -> (content, type), made with ffmpeg on first use


def big_file(name: str) -> tuple[bytes, str]:
    """A clip / picture well over 1 MB (the smallest limit a community can set)."""
    if name not in BIG:
        import subprocess
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            out = f"{d}/{name}"
            if name.endswith(".mp4"):  # 4 s of lossless test pattern with a tone
                args = ["-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30", "-f", "lavfi",
                        "-i", "sine=frequency=440", "-t", "4", "-c:v", "libx264", "-qp", "0", "-preset",
                        "ultrafast", "-c:a", "aac", "-shortest"]
            else:  # a noisy picture, which compresses badly as PNG
                args = ["-f", "lavfi", "-i", "nullsrc=size=1600x1200,geq=random(1)*255:128:128", "-frames:v", "1"]
            subprocess.run(["ffmpeg", "-v", "error", "-y", *args, out], check=True)
            BIG[name] = (open(out, "rb").read(), "video/mp4" if name.endswith(".mp4") else "image/png")
    return BIG[name]


def remote_with_big(request: httpx.Request) -> httpx.Response:
    name = request.url.path.lstrip("/")
    if name in ("clip.mp4", "noise.png"):
        body, ctype = big_file(name)
        return httpx.Response(200, content=body, headers={"content-type": ctype})
    if name == "fat.png":  # really over 1 MB, unlike huge.png, which only says so
        return httpx.Response(200, content=PNG + b"\x00" * 1_500_000, headers={"content-type": "image/png"})
    if name == "small.mp4":
        return httpx.Response(200, content=b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 50,
                              headers={"content-type": "video/mp4"})
    return fake_remote(request)


@pytest.fixture
def tbouncer(bouncer):
    bouncer.media = media.MediaFetcher(bouncer.db, bouncer.media_dir, "t", 1_000_000,
                                       client=httpx.Client(transport=httpx.MockTransport(remote_with_big)),
                                       check_host=False)
    return bouncer


def community_with(server, b, body: str) -> int:
    server.add_post("1", "media", body)
    b.ingest_url(f"https://{DOMAIN}/post/1")
    with b.db.connect() as conn:
        return conn.execute("SELECT id FROM communities").fetchone()[0]


def logged_in(settings, b) -> TestClient:
    client = TestClient(create_app(settings, b))
    client.post("/login", data={"password": "pw"})
    return client


def test_community_can_leave_out_videos_or_everything(settings, server, tbouncer):
    cid = community_with(server, tbouncer, "![a](https://img.test/cat.gif) ![v](https://img.test/small.mp4)")
    client = logged_in(settings, tbouncer)
    client.post(f"/c/{cid}/media-settings", data={"archive": "images", "max_mb": "", "transcode": "default"})
    tbouncer.media.fetch_pending()
    rows = media_rows(tbouncer)
    assert rows["https://img.test/cat.gif"]["status"] == "ok"
    assert rows["https://img.test/small.mp4"]["status"] == "skipped"
    assert "for this community" in rows["https://img.test/small.mp4"]["error"]

    # Allowing videos again retries the one that was left out.
    r = client.post(f"/c/{cid}/media-settings", data={"archive": "default", "max_mb": "", "transcode": "default"})
    assert "Trying again 1 file" in r.text
    tbouncer.media.fetch_pending()
    assert media_rows(tbouncer)["https://img.test/small.mp4"]["status"] == "ok"

    server.add_post("2", "more", "![b](https://img.test/octet)")
    client.post(f"/c/{cid}/media-settings", data={"archive": "off", "max_mb": "", "transcode": "default"})
    tbouncer.ingest_url(f"https://{DOMAIN}/post/2")
    tbouncer.media.fetch_pending()
    assert media_rows(tbouncer)["https://img.test/octet"]["status"] == "skipped"
    assert media_rows(tbouncer)["https://img.test/cat.gif"]["status"] == "ok"  # already archived: kept


def test_shared_media_gets_the_more_generous_setting(server, tbouncer):
    cid = community_with(server, tbouncer, "![v](https://img.test/small.mp4)")
    default = tbouncer.media.default_policy
    with tbouncer.db.transaction() as conn:
        conn.execute("UPDATE communities SET media_archive='off' WHERE id=?", (cid,))
        mid = conn.execute("SELECT id FROM media").fetchone()[0]
        assert media.policy_for(conn, mid, default) == media.MediaPolicy("off", 1_000_000, False)
        # The same video posted in another community that keeps pictures only, up to 50 MB, transcoding.
        now = utcnow()
        other = conn.execute("INSERT INTO communities(canonical_ap_id, name, first_seen_at, last_seen_at, "
                             "media_archive, media_max_mb, media_transcode) "
                             "VALUES ('https://x.test/c/o', 'o', ?, ?, 'images', 50, 1)", (now, now)).lastrowid
        oid = conn.execute("INSERT INTO objects(canonical_ap_id, object_type, community_id, first_seen_at, "
                           "last_seen_at) VALUES ('https://x.test/post/9', 'post', ?, ?, ?)",
                           (other, now, now)).lastrowid
        media.register(conn, oid, ["https://img.test/small.mp4"], now)
        assert media.policy_for(conn, mid, default) == media.MediaPolicy("images", 50_000_000, True)


def test_oversized_without_transcoding_fails_then_retries_when_enabled(settings, server, tbouncer, monkeypatch):
    monkeypatch.setattr(media.transcode, "shrink",
                        lambda src, ctype, limit, workdir: (_write(workdir, b"\x00\x00\x00\x18ftypmp42small"),
                                                            "video/mp4"))
    monkeypatch.setattr(media.MediaFetcher, "can_transcode", lambda self: True)
    cid = community_with(server, tbouncer, "![v](https://img.test/fat.png)")
    tbouncer.media.fetch_pending()
    assert media_rows(tbouncer)["https://img.test/fat.png"]["error"].startswith("too large")

    client = logged_in(settings, tbouncer)
    client.post(f"/c/{cid}/media-settings", data={"archive": "default", "max_mb": "", "transcode": "on"})
    tbouncer.media.fetch_pending()
    row = media_rows(tbouncer)["https://img.test/fat.png"]
    assert row["status"] == "ok" and row["content_type"] == "video/mp4"
    assert row["original_type"] == "image/png" and row["original_bytes"] == len(PNG) + 1_500_000


def _write(workdir, data: bytes):
    import tempfile
    from pathlib import Path
    with tempfile.NamedTemporaryFile(dir=workdir, delete=False) as f:
        f.write(data)
    return Path(f.name)


def test_bad_size_limit_is_refused(settings, server, tbouncer):
    cid = community_with(server, tbouncer, "")
    client = logged_in(settings, tbouncer)
    r = client.post(f"/c/{cid}/media-settings", data={"archive": "all", "max_mb": "lots", "transcode": "on"})
    assert "number of megabytes" in r.text
    with tbouncer.db.connect() as conn:
        assert conn.execute("SELECT media_archive FROM communities WHERE id=?", (cid,)).fetchone()[0] is None


@needs_ffmpeg
def test_oversized_video_and_picture_are_transcoded_to_fit(settings, server, tbouncer):
    cid = community_with(server, tbouncer, "![v](https://img.test/clip.mp4) ![p](https://img.test/noise.png)")
    assert len(big_file("clip.mp4")[0]) > 1_000_000 and len(big_file("noise.png")[0]) > 1_000_000
    client = logged_in(settings, tbouncer)
    client.post(f"/c/{cid}/media-settings", data={"archive": "all", "max_mb": "1", "transcode": "on"})
    tbouncer.media.fetch_pending()
    rows = media_rows(tbouncer)
    video, picture = rows["https://img.test/clip.mp4"], rows["https://img.test/noise.png"]
    assert video["status"] == "ok", video["error"]
    assert video["content_type"] == "video/mp4" and video["size_bytes"] <= 1_000_000
    assert video["original_bytes"] == len(big_file("clip.mp4")[0])
    assert picture["status"] == "ok", picture["error"]
    assert picture["content_type"] in ("image/webp", "image/jpeg") and picture["size_bytes"] <= 1_000_000
    stored = transcode.probe(tbouncer.media_dir / video["storage_path"])
    assert {s["codec_type"] for s in stored["streams"]} == {"video", "audio"}

    page = client.get(f"/c/{cid}?tab=media").text
    assert "2</strong> files" in page and "2 transcoded" in page and "Recently transcoded" in page
    assert 'value="1"' in page and '<option value="on" selected>' in page


def test_media_tab_lists_what_was_not_archived(settings, server, tbouncer):
    cid = community_with(server, tbouncer, "![big](https://img.test/huge.png) ![a](https://img.test/cat.gif)")
    tbouncer.media.fetch_pending()
    client = logged_in(settings, tbouncer)
    page = client.get(f"/c/{cid}?tab=media").text
    assert "Not archived" in page and "too large" in page and "1 couldn&#39;t be archived" in page
    r = client.post(f"/c/{cid}/media-retry")
    assert "Trying again 1 file" in r.text


def test_media_defaults_from_the_storage_page(settings, server, tbouncer):
    cid = community_with(server, tbouncer, "![a](https://img.test/cat.gif) ![v](https://img.test/small.mp4)")
    client = logged_in(settings, tbouncer)
    env = tbouncer.media.env_policy
    assert 'action="/storage/media-defaults"' in client.get("/storage").text

    r = client.post("/storage/media-defaults", data={"archive": "images", "max_mb": "40", "transcode": "on"})
    assert "Media defaults saved." in r.text
    assert tbouncer.media.default_policy == media.MediaPolicy("images", 40_000_000, True)
    assert "Default: transcode to fit" in client.get(f"/c/{cid}?tab=media").text
    tbouncer.media.fetch_pending()
    assert media_rows(tbouncer)["https://img.test/small.mp4"]["status"] == "skipped"

    # A community's own choice still wins over the defaults.
    client.post(f"/c/{cid}/media-settings", data={"archive": "all", "max_mb": "", "transcode": "off"})
    with tbouncer.db.connect() as conn:
        mid = conn.execute("SELECT id FROM media WHERE url='https://img.test/small.mp4'").fetchone()[0]
        assert media.policy_for(conn, mid, tbouncer.media.default_policy) == media.MediaPolicy("all", 40_000_000, False)
    client.post(f"/c/{cid}/media-settings", data={"archive": "default", "max_mb": "", "transcode": "default"})

    # Back to the server settings: the left-out video is tried again.
    r = client.post("/storage/media-defaults", data={"archive": "default", "max_mb": "", "transcode": "default"})
    assert tbouncer.media.default_policy == env
    tbouncer.media.fetch_pending()
    assert media_rows(tbouncer)["https://img.test/small.mp4"]["status"] == "ok"

    r = client.post("/storage/media-defaults", data={"archive": "all", "max_mb": "lots", "transcode": "default"})
    assert "number of megabytes" in r.text and tbouncer.media.default_policy == env
