from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from threadbnc import media
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
    assert 'aria-label="Show as a list"' in page and 'view=auto"><span class="tick"><svg' in page  # auto, not chosen
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


def test_nsfw_tiles_are_veiled(settings, server, mbouncer):
    cid = picture_community(server, mbouncer)
    server.edit_post("1", metadata={"nsfw": True})
    tid = mbouncer.ingest_url(f"https://{DOMAIN}/post/1")
    mbouncer.sync_thread(tid, force=True)
    client = TestClient(create_app(settings, mbouncer))
    client.post("/login", data={"password": "pw"})
    page = client.get(f"/c/{cid}").text
    assert page.count(" veiled") == 1 and '<span class="tile-veil">NSFW<span class="small">Tap to show</span></span>' in page
