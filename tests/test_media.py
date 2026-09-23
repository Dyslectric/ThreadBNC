from __future__ import annotations

import re

import httpx
import pytest
from fastapi.testclient import TestClient

from threadbnc import media, thumbs, transcode
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
    assert f'<img src="/media/{mid}?w=320"' in feed_html and 'img.test<svg class="i"' in feed_html  # (list size)
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
        srcs = [int(m) for m in re.findall(r'<img src="/media/(\d+)\?w=\d+"', page)]
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
    if name == "fat.mp4":  # over 1 MB
        return httpx.Response(200, content=b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 1_500_000,
                              headers={"content-type": "video/mp4"})
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


PICTURES_ONLY = {"video_save": "off", "audio_save": "off"}
NOTHING = {"image_save": "off", "video_save": "off", "audio_save": "off"}


def test_community_can_leave_out_videos_or_everything(settings, server, tbouncer):
    cid = community_with(server, tbouncer, "![a](https://img.test/cat.gif) ![v](https://img.test/small.mp4)")
    client = logged_in(settings, tbouncer)
    client.post(f"/c/{cid}/media-settings", data=PICTURES_ONLY)
    tbouncer.media.fetch_pending()
    rows = media_rows(tbouncer)
    assert rows["https://img.test/cat.gif"]["status"] == "ok"
    assert rows["https://img.test/small.mp4"]["status"] == "skipped"
    assert "for this community" in rows["https://img.test/small.mp4"]["error"]

    # Allowing videos again retries the one that was left out.
    r = client.post(f"/c/{cid}/media-settings", data={})
    assert "Trying again 1 file" in r.text
    tbouncer.media.fetch_pending()
    assert media_rows(tbouncer)["https://img.test/small.mp4"]["status"] == "ok"

    server.add_post("2", "more", "![b](https://img.test/octet)")
    client.post(f"/c/{cid}/media-settings", data=NOTHING)
    tbouncer.ingest_url(f"https://{DOMAIN}/post/2")
    tbouncer.media.fetch_pending()
    assert media_rows(tbouncer)["https://img.test/octet"]["status"] == "skipped"
    assert media_rows(tbouncer)["https://img.test/cat.gif"]["status"] == "ok"  # already archived: kept


def test_shared_media_gets_the_more_generous_setting(server, tbouncer):
    cid = community_with(server, tbouncer, "![v](https://img.test/small.mp4)")
    default = tbouncer.media.default_policy
    off = media.KindPolicy(False, 1_000_000, None)
    with tbouncer.db.transaction() as conn:
        conn.execute("UPDATE communities SET media_policy=? WHERE id=?", (media.dump_choices(
            {k: {"save": False} for k in media.KINDS}), cid))
        mid = conn.execute("SELECT id FROM media").fetchone()[0]
        assert media.policy_for(conn, mid, default) == media.MediaPolicy(off, off, off)
        # The same video posted in another community that keeps pictures only, up to 50 MB, transcoding
        # (as saved before there were settings for each kind).
        now = utcnow()
        other = conn.execute("INSERT INTO communities(canonical_ap_id, name, first_seen_at, last_seen_at, "
                             "media_archive, media_max_mb, media_transcode) "
                             "VALUES ('https://x.test/c/o', 'o', ?, ?, 'images', 50, 1)", (now, now)).lastrowid
        media.migrate_legacy(conn)
        oid = conn.execute("INSERT INTO objects(canonical_ap_id, object_type, community_id, first_seen_at, "
                           "last_seen_at) VALUES ('https://x.test/post/9', 'post', ?, ?, ?)",
                           (other, now, now)).lastrowid
        media.register(conn, oid, ["https://img.test/small.mp4"], now)
        assert media.policy_for(conn, mid, default) == media.MediaPolicy(
            media.KindPolicy(True, 50_000_000, 50_000_000), media.KindPolicy(False, 50_000_000, 50_000_000),
            media.KindPolicy(False, 50_000_000, None))


def test_older_settings_become_the_same_for_each_kind(bouncer):
    now = utcnow()
    with bouncer.db.transaction() as conn:
        for key, value in (("media_archive", "images"), ("media_transcode", "1")):
            conn.execute("INSERT INTO app_settings(key, value) VALUES (?, ?)", (key, value))
        cid = conn.execute("INSERT INTO communities(canonical_ap_id, name, first_seen_at, last_seen_at, "
                           "media_max_mb, media_transcode) VALUES ('https://x.test/c/o', 'o', ?, ?, 30, 0)",
                           (now, now)).lastrowid
        media.migrate_legacy(conn)
        saved = media.load_defaults(conn)
        assert saved == {"image": {"save": True, "fit": True}, "video": {"save": False, "fit": True},
                         "audio": {"save": False}}
        row = conn.execute("SELECT * FROM communities WHERE id=?", (cid,)).fetchone()
        assert row["media_max_mb"] is None and media.parse_choices(row["media_policy"]) == {
            "image": {"keep_mb": 30, "target_mb": 0}, "video": {"keep_mb": 30, "target_mb": 0},
            "audio": {"keep_mb": 30}}
    # "Transcode to fit" whatever size limit applies.
    policy = media.MediaPolicy.uniform(20_000_000, False).override(saved)
    assert policy.image == media.KindPolicy(True, 20_000_000, 20_000_000) and not policy.video.save


def test_oversized_without_transcoding_fails_then_retries_when_enabled(settings, server, tbouncer, monkeypatch):
    monkeypatch.setattr(media.transcode, "shrink",
                        lambda src, ctype, limit, workdir: (_write(workdir, b"\x00\x00\x00\x18ftypmp42small"),
                                                            "video/mp4"))
    monkeypatch.setattr(media.MediaFetcher, "can_transcode", lambda self: True)
    cid = community_with(server, tbouncer, "![v](https://img.test/fat.png)")
    tbouncer.media.fetch_pending()
    assert media_rows(tbouncer)["https://img.test/fat.png"]["error"].startswith("too large")

    client = logged_in(settings, tbouncer)
    client.post(f"/c/{cid}/media-settings", data={"image_bigger": "transcode"})  # to fit the size limit
    tbouncer.media.fetch_pending()
    row = media_rows(tbouncer)["https://img.test/fat.png"]
    assert row["status"] == "ok" and row["content_type"] == "video/mp4"
    assert row["original_type"] == "image/png" and row["original_bytes"] == len(PNG) + 1_500_000


def test_each_kind_has_its_own_size_limits(settings, server, tbouncer, monkeypatch):
    shrunk = []
    monkeypatch.setattr(media.transcode, "shrink", lambda src, ctype, limit, workdir: shrunk.append((ctype, limit)))
    monkeypatch.setattr(media.MediaFetcher, "can_transcode", lambda self: True)
    cid = community_with(server, tbouncer, "![v](https://img.test/fat.png)")
    client = logged_in(settings, tbouncer)
    # A bigger limit for videos, and transcoding them, doesn't reach pictures.
    client.post(f"/c/{cid}/media-settings", data={"video_keep": "5", "video_bigger": "transcode"})
    tbouncer.media.fetch_pending()
    assert media_rows(tbouncer)["https://img.test/fat.png"]["error"].startswith("too large") and not shrunk
    client.post(f"/c/{cid}/media-settings", data={"image_keep": "2"})
    tbouncer.media.fetch_pending()
    row = media_rows(tbouncer)["https://img.test/fat.png"]
    assert row["status"] == "ok" and row["original_bytes"] is None and not shrunk  # kept as it is
    # Audio is never transcoded, whatever the form says.
    client.post(f"/c/{cid}/media-settings", data={"audio_bigger": "transcode", "audio_target": "1"})
    with tbouncer.db.connect() as conn:
        assert tbouncer.media.default_policy.override(media.parse_choices(conn.execute(
            "SELECT media_policy FROM communities WHERE id=?", (cid,)).fetchone()[0])).audio.target_bytes is None


def test_lowering_a_limit_transcodes_what_is_already_archived(settings, server, tbouncer, monkeypatch):
    shrunk = []

    def shrink(src, ctype, limit, workdir):
        shrunk.append((src.read_bytes()[:8], ctype, limit))
        return _write(workdir, b"RIFF\x00\x00\x00\x00WEBPsmall"), "image/webp"

    monkeypatch.setattr(media.transcode, "shrink", shrink)
    monkeypatch.setattr(media.MediaFetcher, "can_transcode", lambda self: True)
    cid = community_with(server, tbouncer, "![p](https://img.test/fat.png) ![a](https://img.test/cat.gif)")
    client = logged_in(settings, tbouncer)
    client.post(f"/c/{cid}/media-settings", data={"image_keep": "2"})
    tbouncer.media.fetch_pending()
    before = media_rows(tbouncer)["https://img.test/fat.png"]
    assert before["status"] == "ok" and before["original_bytes"] is None
    while tbouncer.media.convert_some():  # nothing over the limits: nothing to do
        pass
    assert not shrunk

    # Turning pictures off leaves them be; a lower limit with a size to transcode down to converts them.
    client.post(f"/c/{cid}/media-settings", data={"image_save": "off", "image_keep": "1", "image_bigger": "transcode"})
    while tbouncer.media.convert_some():
        pass
    assert not shrunk
    client.post(f"/c/{cid}/media-settings", data={"image_keep": "1", "image_bigger": "transcode", "image_target": "1"})
    while tbouncer.media.convert_some():
        pass
    assert shrunk == [(PNG[:8], "image/png", 1_000_000)]  # the big one only, from its stored copy
    row = media_rows(tbouncer)["https://img.test/fat.png"]
    assert (row["status"], row["content_type"], row["original_type"]) == ("ok", "image/webp", "image/png")
    assert row["original_bytes"] == before["size_bytes"] and row["size_bytes"] < 100
    assert (tbouncer.media_dir / row["storage_path"]).exists()
    assert not (tbouncer.media_dir / before["storage_path"]).exists()  # the bigger copy is gone
    assert tbouncer.db.get_setting(media.CONVERT_KEY) is None  # done

    # Without ffmpeg it waits.
    monkeypatch.setattr(media.MediaFetcher, "can_transcode", lambda self: False)
    client.post(f"/c/{cid}/media-settings", data={})
    assert not tbouncer.media.convert_some() and tbouncer.db.get_setting(media.CONVERT_KEY) == "0"


def test_videos_can_be_reencoded_at_a_bitrate(settings, server, tbouncer, monkeypatch):
    calls = []
    already_low = set()

    def at_bitrate(src, bps, workdir):
        calls.append(bps)
        return None if src.stat().st_size in already_low else _write(workdir, b"\x00\x00\x00\x18ftypmp42lean")

    monkeypatch.setattr(media.transcode, "at_bitrate", at_bitrate)
    monkeypatch.setattr(media.transcode, "shrink", lambda *a: pytest.fail("not by size"))
    monkeypatch.setattr(media.MediaFetcher, "can_transcode", lambda self: True)
    cid = community_with(server, tbouncer, "![v](https://img.test/fat.mp4)")
    client = logged_in(settings, tbouncer)
    r = client.post(f"/c/{cid}/media-settings", data={"video_bigger": "rate", "video_rate": "fast"})
    assert "the bitrate is a number of megabits a second" in r.text
    client.post(f"/c/{cid}/media-settings", data={"video_bigger": "rate", "video_rate": "2.5"})
    with tbouncer.db.connect() as conn:
        mid = conn.execute("SELECT id FROM media WHERE url='https://img.test/fat.mp4'").fetchone()[0]
        assert media.policy_for(conn, mid, tbouncer.media.default_policy).video == media.KindPolicy(
            True, 1_000_000, None, 2_500_000)
    page = client.get(f"/c/{cid}?tab=media").text
    assert '<option value="rate" selected>' in page and 'name="video_rate" class="num" inputmode="decimal" value="2.5"' in page

    tbouncer.media.fetch_one(media_rows(tbouncer)["https://img.test/fat.mp4"], wanted=True)
    row = media_rows(tbouncer)["https://img.test/fat.mp4"]
    assert calls == [2_500_000] and row["status"] == "ok" and row["size_bytes"] < 100
    assert row["original_bytes"] > 1_500_000

    # Already archived as it was: re-encoded when the setting changes, unless it's no higher already.
    with tbouncer.db.transaction() as conn:
        conn.execute("UPDATE media SET status='pending', attempts=0 WHERE id=?", (mid,))
    client.post(f"/c/{cid}/media-settings", data={"video_keep": "5"})
    tbouncer.media.fetch_one(media_rows(tbouncer)["https://img.test/fat.mp4"], wanted=True)
    kept = media_rows(tbouncer)["https://img.test/fat.mp4"]
    assert kept["size_bytes"] > 1_500_000 and kept["original_bytes"] is None
    already_low.add(kept["size_bytes"])
    client.post(f"/c/{cid}/media-settings", data={"video_bigger": "rate", "video_rate": "1"})
    while tbouncer.media.convert_some():
        pass
    assert calls[-1] == 1_000_000 and media_rows(tbouncer)["https://img.test/fat.mp4"]["storage_path"] == kept["storage_path"]
    already_low.clear()
    client.post(f"/c/{cid}/media-settings", data={"video_bigger": "rate", "video_rate": "1"})
    while tbouncer.media.convert_some():
        pass
    assert media_rows(tbouncer)["https://img.test/fat.mp4"]["size_bytes"] < 100


def test_a_bitrate_wins_over_a_size_for_shared_videos():
    by_size, by_rate = media.KindPolicy(True, 10_000_000, 50_000_000), media.KindPolicy(True, 5_000_000, None, 1_000_000)
    assert by_size.merge(by_rate) == by_rate.merge(by_size) == media.KindPolicy(True, 10_000_000, None, 1_000_000)
    assert by_rate.override({"target_mb": 20}) == media.KindPolicy(True, 5_000_000, 20_000_000, None)
    assert by_size.override({"target_mbps": 2.5}) == media.KindPolicy(True, 10_000_000, None, 2_500_000)


@needs_ffmpeg
def test_reencoding_at_a_bitrate(tmp_path):
    src = tmp_path / "clip.mp4"
    src.write_bytes(big_file("clip.mp4")[0])
    out = transcode.at_bitrate(src, 1_000_000, tmp_path)
    info = transcode.probe(out)
    assert float(info["format"]["bit_rate"]) < 1_300_000 and out.stat().st_size < src.stat().st_size
    assert transcode.at_bitrate(out, 5_000_000, tmp_path) is None  # already lower


@needs_ffmpeg
def test_scaling_a_video_down_to_a_resolution(tmp_path):
    src = tmp_path / "clip.mp4"
    src.write_bytes(big_file("clip.mp4")[0])  # 1280x720
    out = transcode.to_height(src, 480, tmp_path)
    video = next(s for s in transcode.probe(out)["streams"] if s["codec_type"] == "video")
    assert (video["width"], video["height"]) == (854, 480)
    assert transcode.to_height(out, 720, tmp_path) is None  # no taller than that already


def test_you_can_see_what_is_transcoded(settings, server, tbouncer, monkeypatch):
    fail = []

    def shrink(src, ctype, limit, workdir):
        if fail:
            raise media.transcode.TranscodeError("ffmpeg: broken file")
        return _write(workdir, b"RIFF\x00\x00\x00\x00WEBPsmall"), "image/webp"

    monkeypatch.setattr(media.transcode, "shrink", shrink)
    monkeypatch.setattr(media.MediaFetcher, "can_transcode", lambda self: True)
    server.add_post("1", "a picture", "")
    server.edit_post("1", url="https://img.test/fat.png")  # the post's link: shown with a note under it
    tbouncer.ingest_url(f"https://{DOMAIN}/post/1")
    cid = one(tbouncer, "SELECT id FROM communities")[0]
    client = logged_in(settings, tbouncer)
    page = client.get("/storage").text
    assert 'id="transcoding"' in page and "ffmpeg</span> installed" in page and "Idle" in page
    assert "Recently transcoded" not in page

    # Checking what's already archived, when asked to, with nothing to do yet.
    client.post(f"/c/{cid}/media-settings", data={"image_keep": "2"})
    tbouncer.media.fetch_pending()
    r = client.post("/storage/convert")
    assert "Checking archived files against the media settings." in r.text
    assert "0 of 1 looked at" in r.text

    # A conversion that fails says why, on the Storage page and the post.
    fail.append(1)
    client.post(f"/c/{cid}/media-settings", data={"image_keep": "1", "image_bigger": "transcode"})
    while tbouncer.media.convert_some():
        pass
    tid = one(tbouncer, "SELECT thread_id FROM objects WHERE object_type='post'")[0]
    page = client.get("/storage").text
    assert "<h3>Couldn't transcode</h3>" in page and "kept as it was" in page and "ffmpeg: broken file" in page
    assert f'href="/t/{tid}"' in page
    assert "couldn&#39;t transcode it down to 1 MB: ffmpeg: broken file" in client.get(f"/t/{tid}").text

    # Then one that works: listed, and noted under the picture, the failure gone.
    fail.clear()
    client.post("/storage/convert")
    while tbouncer.media.convert_some():
        pass
    page = client.get("/storage").text
    assert "Recently transcoded" in page and "png →" in page and "<h3>Couldn't transcode" not in page
    post = client.get(f"/t/{tid}").text
    assert "transcoded from 1.5 MB" in post and "png to" in post and "broken file" not in post

    # While it's working on one.
    tbouncer.media.working = {"id": 1, "url": "https://img.test/big.mp4", "size": 80_000_000,
                              "how": "at 2.5 Mbps", "since": utcnow()}
    page = client.get("/storage").text
    assert "transcoding a 80 MB file at 2.5 Mbps" in page and "img.test" in page


def one(b, sql, *args):
    with b.db.connect() as conn:
        return conn.execute(sql, args).fetchone()


def test_the_feed_asks_for_pictures_at_each_view_s_width(settings, server, mbouncer):
    cid = picture_community(server, mbouncer, pictures=1)
    client = TestClient(create_app(settings, mbouncer))
    client.post("/login", data={"password": "pw"})
    mid = media_rows(mbouncer)["https://img.test/cat.gif?n=0"]["id"]
    for view, width in (("list", 320), ("tiles", 640), ("pictures", 1280)):  # the defaults
        assert f'<img src="/media/{mid}?w={width}"' in client.get(f"/c/{cid}?view={view}").text, view

    r = client.post("/storage/thumbnails", data={"list": "200", "tile": "0", "picture": ""})
    assert "Copies at the new widths are made in the background" in r.text
    assert 'name="list" class="num" inputmode="numeric" value="200"' in r.text
    assert f'<img src="/media/{mid}?w=200"' in client.get(f"/c/{cid}?view=list").text
    assert f'<img src="/media/{mid}"' in client.get(f"/c/{cid}?view=tiles").text  # 0: as it is
    assert f'<img src="/media/{mid}?w=1280"' in client.get(f"/c/{cid}?view=pictures").text
    r = client.post("/storage/thumbnails", data={"list": "huge"})
    assert "Thumbnails in the list view: a width in pixels" in r.text
    with mbouncer.db.connect() as conn:
        assert thumbs.widths(conn) == {"list": 200, "tile": 0, "picture": 1280}


def test_smaller_copies_are_made_and_served_in_place(settings, server, tbouncer, monkeypatch):
    made = []

    def thumbnail(src, width, workdir):  # the picture is 1000px wide
        made.append((src.read_bytes()[:8], width))
        if width >= 1000:
            return None
        out = workdir / f".thumb-{len(made)}.webp"
        out.write_bytes(b"RIFF\x00\x00\x00\x00WEBP%d" % width)
        return out, "image/webp"

    monkeypatch.setattr(media.transcode, "thumbnail", thumbnail)
    monkeypatch.setattr(media.transcode, "available", lambda: True)
    cid = community_with(server, tbouncer, "![p](https://img.test/fat.png) ![a](https://img.test/cat.gif)")
    client = logged_in(settings, tbouncer)
    client.post(f"/c/{cid}/media-settings", data={"image_keep": "2"})
    tbouncer.media.fetch_pending()
    rows = media_rows(tbouncer)
    png, gif = rows["https://img.test/fat.png"], rows["https://img.test/cat.gif"]
    # Made as the picture is downloaded: one for each width it's wider than. None of a GIF, so it still moves.
    assert made == [(PNG[:8], 320), (PNG[:8], 640), (PNG[:8], 1280)]
    r = client.get(f"/media/{png['id']}?w=1280")  # narrower than that: as it is, for good
    assert r.content.startswith(PNG[:8]) and "immutable" in r.headers["cache-control"]

    r = client.get(f"/media/{png['id']}?w=640")
    assert r.content == b"RIFF\x00\x00\x00\x00WEBP640" and r.headers["content-type"] == "image/webp"
    assert "immutable" in r.headers["cache-control"]
    r = client.get(f"/media/{png['id']}?w=500")  # not a width used: the picture itself
    assert r.content.startswith(PNG[:8]) and "immutable" in r.headers["cache-control"]
    r = client.get(f"/media/{gif['id']}?w=320")  # none of it: as it is
    assert r.content == GIF and "immutable" in r.headers["cache-control"]
    r = client.get(f"/media/{png['id']}")
    assert r.content.startswith(PNG[:8])

    # New widths: copies of what's archived are made in the background, and ones no longer used go.
    client.post("/storage/thumbnails", data={"list": "160", "tile": "0"})
    assert "Making</span> copies of archived pictures" in client.get("/storage").text
    r = client.get(f"/media/{png['id']}?w=160")  # not made yet: the picture, for now
    assert r.content.startswith(PNG[:8]) and r.headers["cache-control"] == "private, max-age=600"
    while media.thumbs.run_some(tbouncer.db, tbouncer.media_dir):
        pass
    assert made[3:] == [(PNG[:8], 160)]  # (1280 was settled already)
    assert client.get(f"/media/{png['id']}?w=160").content == b"RIFF\x00\x00\x00\x00WEBP160"
    names = sorted(p.name.split("-")[1] for p in (tbouncer.media_dir / "thumbs").glob("*/*"))
    assert names == ["1280.same", "160.webp"]  # the 320 and 640 ones gone
    page = client.get("/storage").text
    assert "1 copy kept" in page and "Making</span>" not in page


def test_a_pass_is_started_when_the_widths_are_new(bouncer):
    with bouncer.db.transaction() as conn:
        thumbs.request_pass_if_needed(conn)
        assert thumbs.progress(conn) == {"checked": 0, "total": 0}
    with bouncer.db.transaction() as conn:
        conn.execute("DELETE FROM app_settings WHERE key=?", (thumbs.PASS_KEY,))
        conn.execute("INSERT INTO app_settings(key, value) VALUES (?, ?)", (thumbs.DONE_KEY, "320,640,1280"))
        thumbs.request_pass_if_needed(conn)  # done for these widths already
        assert thumbs.progress(conn) is None


def _write(workdir, data: bytes):
    import tempfile
    from pathlib import Path
    with tempfile.NamedTemporaryFile(dir=workdir, delete=False) as f:
        f.write(data)
    return Path(f.name)


def test_bad_size_limit_is_refused(settings, server, tbouncer):
    cid = community_with(server, tbouncer, "")
    client = logged_in(settings, tbouncer)
    r = client.post(f"/c/{cid}/media-settings", data={"image_keep": "lots", "video_save": "off"})
    assert "Pictures: sizes are a number of megabytes" in r.text
    r = client.post(f"/c/{cid}/media-settings", data={"video_keep": "10", "video_bigger": "transcode",
                                                       "video_target": "20"})
    assert "Videos: transcode down to no more than" in r.text
    with tbouncer.db.connect() as conn:
        assert conn.execute("SELECT media_policy FROM communities WHERE id=?", (cid,)).fetchone()[0] is None


@needs_ffmpeg
def test_oversized_video_and_picture_are_transcoded_to_fit(settings, server, tbouncer):
    cid = community_with(server, tbouncer, "![v](https://img.test/clip.mp4) ![p](https://img.test/noise.png)")
    assert len(big_file("clip.mp4")[0]) > 1_000_000 and len(big_file("noise.png")[0]) > 1_000_000
    client = logged_in(settings, tbouncer)
    client.post(f"/c/{cid}/media-settings", data={"image_keep": "1", "image_bigger": "transcode",
                                                   "video_keep": "1", "video_bigger": "transcode", "video_target": "1"})
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
    assert 'value="1"' in page and '<option value="transcode" selected>' in page


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

    r = client.post("/storage/media-defaults", data={
        **PICTURES_ONLY, "image_keep": "40", "image_bigger": "transcode", "image_target": "8",
        "video_keep": "40", "video_bigger": "transcode", "audio_keep": "40"})
    assert "Media defaults saved." in r.text
    assert tbouncer.media.default_policy == media.MediaPolicy(
        media.KindPolicy(True, 40_000_000, 8_000_000), media.KindPolicy(False, 40_000_000, 40_000_000),
        media.KindPolicy(False, 40_000_000, None))
    assert "Default: transcode to 8" in client.get(f"/c/{cid}?tab=media").text
    tbouncer.media.fetch_pending()
    assert media_rows(tbouncer)["https://img.test/small.mp4"]["status"] == "skipped"

    # A community's own choice still wins over the defaults.
    client.post(f"/c/{cid}/media-settings", data={"video_save": "on", "video_bigger": "leave"})
    with tbouncer.db.connect() as conn:
        mid = conn.execute("SELECT id FROM media WHERE url='https://img.test/small.mp4'").fetchone()[0]
        assert media.policy_for(conn, mid, tbouncer.media.default_policy).video == media.KindPolicy(
            True, 40_000_000, None)
    client.post(f"/c/{cid}/media-settings", data={})

    # Back to the server settings: the left-out video is tried again.
    r = client.post("/storage/media-defaults", data={})
    assert tbouncer.media.default_policy == env
    tbouncer.media.fetch_pending()
    assert media_rows(tbouncer)["https://img.test/small.mp4"]["status"] == "ok"

    r = client.post("/storage/media-defaults", data={"audio_keep": "lots"})
    assert "number of megabytes" in r.text and tbouncer.media.default_policy == env
