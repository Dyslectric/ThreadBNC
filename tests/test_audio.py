"""Audio file links: downloaded when the post is scrolled to in a feed, and
played from a bar in the post's card."""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from threadbnc import feed, media
from threadbnc.render import MediaInfo, render_markdown
from threadbnc.web import create_app

from .conftest import DOMAIN

MP3 = b"ID3\x04\x00\x00" + b"\x00" * 50
M4A = b"\x00\x00\x00\x20ftypisom" + b"\x00" * 50  # an MP4 container: audio or video by its name alone


def fake_audio(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/ep1.mp3":
        return httpx.Response(200, content=MP3, headers={"content-type": "audio/mpeg"})
    if path == "/ep2.m4a":
        return httpx.Response(200, content=M4A, headers={"content-type": "application/octet-stream"})
    return httpx.Response(404)


@pytest.fixture
def aubouncer(bouncer):
    bouncer.media = media.MediaFetcher(bouncer.db, bouncer.media_dir, "t", 10_000_000,
                                       client=httpx.Client(transport=httpx.MockTransport(fake_audio)),
                                       check_host=False)
    return bouncer


def post_linking(server, b, url: str, local_id: str = "1") -> int:
    """A post that arrived by itself (not kept), as from a followed community."""
    server.add_post(local_id, "Episode", "")
    server.edit_post(local_id, url=url)
    return b.ingest_url(f"https://{DOMAIN}/post/{local_id}", "auto")


def audio_row(b, url: str):
    with b.db.connect() as conn:
        return conn.execute("SELECT * FROM media WHERE url=?", (url,)).fetchone()


def community_of(b, tid: int) -> int:
    with b.db.connect() as conn:
        return conn.execute("SELECT community_id FROM archived_threads WHERE id=?", (tid,)).fetchone()[0]


def logged_in(settings, b) -> TestClient:
    client = TestClient(create_app(settings, b))
    client.post("/login", data={"password": "pw"})
    return client


def test_sniffing_audio():
    assert media.sniff(MP3) == "audio/mpeg"
    assert media.sniff(b"\xff\xfb\x90\x00") == "audio/mpeg"
    assert media.sniff(b"\xff\xf1\x50\x80") == "audio/aac"
    assert media.sniff(b"OggS\x00\x02") == "audio/ogg"
    assert media.sniff(b"fLaC\x00\x00") == "audio/flac"
    assert media.sniff(b"RIFF\x00\x00\x00\x00WAVEfmt ") == "audio/wav"
    assert media.sniff(b"\x00\x00\x00\x20ftypM4A \x00") == "audio/mp4"
    assert media.sniff(b"\xff\xd8\xff\xe0") == "image/jpeg"  # not mistaken for an MPEG frame


def test_audio_waits_in_the_background(server, aubouncer):
    """Like videos, audio isn't downloaded just because a post arrived."""
    post_linking(server, aubouncer, "https://pod.test/ep1.mp3")
    aubouncer.media.fetch_pending()
    row = audio_row(aubouncer, "https://pod.test/ep1.mp3")
    assert row["status"] == "pending" and row["held"] == 1 and row["attempts"] == 0


def test_feed_fetches_audio_scrolled_to_and_shows_a_player(settings, server, aubouncer):
    tid = post_linking(server, aubouncer, "https://pod.test/ep1.mp3")
    cid = community_of(aubouncer, tid)
    client = logged_in(settings, aubouncer)
    before = client.get(f"/c/{cid}").text
    assert 'data-audio="pending"' in before and "Fetching the audio" in before and "<audio" not in before
    assert client.get("/feed/articles", params={"ids": [tid]}).json()["audio"] == {str(tid): "pending"}

    assert client.post("/feed/articles", data={"ids": [tid]}).json()["ok"]
    while aubouncer.run_one_job():  # the article job (nothing to read) and the audio one
        pass
    row = audio_row(aubouncer, "https://pod.test/ep1.mp3")
    assert row["status"] == "ok" and row["content_type"] == "audio/mpeg"
    status = client.get("/feed/articles", params={"ids": [tid]}).json()
    assert status["waiting"] == [] and status["audio"] == {str(tid): "ok"}

    for view in ("list", "pictures", "tiles"):
        page = client.get(f"/c/{cid}", params={"view": view}).text
        assert f'<audio src="/media/{row["id"]}" controls' in page, view
        assert "data-audio" not in page
    served = client.get(f"/media/{row['id']}")
    assert served.headers["content-type"] == "audio/mpeg" and served.content == MP3


def test_posts_without_audio_dont_ask(settings, server, aubouncer):
    tid = post_linking(server, aubouncer, "https://pod.test/show-notes")
    client = logged_in(settings, aubouncer)
    aubouncer.articles.enabled = False
    assert "data-audio" not in client.get(f"/c/{community_of(aubouncer, tid)}").text
    client.post("/feed/articles", data={"ids": [tid]})
    assert not aubouncer.run_one_job()


def test_mp4_container_named_as_audio_is_audio(server, aubouncer):
    tid = post_linking(server, aubouncer, "https://pod.test/ep2.m4a")
    aubouncer.fetch_audio([tid])
    row = audio_row(aubouncer, "https://pod.test/ep2.m4a")
    assert row["status"] == "ok" and row["content_type"] == "audio/mp4"
    with aubouncer.db.connect() as conn:
        [item] = feed.load_feed(conn, community_id=community_of(aubouncer, tid)).items
    assert item["thumb"] is None  # a player, not a picture
    assert {k: item["audio"][k] for k in ("id", "status", "error")} == {"id": row["id"], "status": "ok", "error": None}


def test_pictures_only_communities_leave_audio_out(settings, server, aubouncer):
    tid = post_linking(server, aubouncer, "https://pod.test/ep1.mp3")
    cid = community_of(aubouncer, tid)
    client = logged_in(settings, aubouncer)
    client.post(f"/c/{cid}/media-settings", data={"video_save": "off", "audio_save": "off"})
    aubouncer.fetch_audio([tid])
    row = audio_row(aubouncer, "https://pod.test/ep1.mp3")
    assert row["status"] == "skipped" and row["error"] == "audio files aren't archived for this community"
    page = client.get(f"/c/{cid}").text
    assert "<audio" not in page and "data-audio" not in page and "audio-bar" not in page


def test_failed_audio_says_so(settings, server, aubouncer):
    tid = post_linking(server, aubouncer, "https://pod.test/gone.mp3")
    aubouncer.fetch_audio([tid])
    assert audio_row(aubouncer, "https://pod.test/gone.mp3")["status"] == "failed"
    page = logged_in(settings, aubouncer).get(f"/c/{community_of(aubouncer, tid)}").text
    assert "Couldn&#39;t save the audio: HTTP 404" in page or "Couldn't save the audio: HTTP 404" in page


def test_markdown_audio():
    out = str(render_markdown("![ep](https://pod.test/ep1.mp3)", {"https://pod.test/ep1.mp3":
                                                                  MediaInfo(3, "ok", "audio/mpeg")}.get))
    assert '<audio class="media" src="/media/3" controls' in out
    assert "[audio: ep" in str(render_markdown("![ep](https://pod.test/ep1.mp3)"))


def test_kept_page_has_video_and_audio_tabs(settings, server, aubouncer):
    for local_id, title, url in [("1", "Episode", "https://pod.test/ep1.mp3"),
                                 ("2", "A talk", "https://www.youtube.com/watch?v=dQw4w9WgXcQ"),
                                 ("3", "Just words", None)]:
        server.add_post(local_id, title, "")
        if url:
            server.edit_post(local_id, url=url)
        aubouncer.ingest_url(f"https://{DOMAIN}/post/{local_id}")  # kept by link
    post_linking(server, aubouncer, "https://pod.test/ep2.m4a", "4")  # not kept
    with aubouncer.db.connect() as conn:
        assert feed.count_kept_media(conn, "audio") == 1 and feed.count_kept_media(conn, "video") == 1
    client = logged_in(settings, aubouncer)
    kept = client.get("/kept").text
    assert 'href="/kept?tab=videos"' in kept and 'href="/kept?tab=audio"' in kept
    audio = client.get("/kept?tab=audio").text
    assert "Episode" in audio and "A talk" not in audio and "Just words" not in audio
    assert 'aria-current="page"' in audio and "/kept?tab=audio&amp;sort=" in audio
    videos = client.get("/kept?tab=videos").text
    assert "A talk" in videos and "Episode" not in videos and "Just words" not in videos


def test_media_filters_have_no_literal_question_marks():
    """On Postgres every "?" becomes a placeholder (db.py), even inside a
    quoted LIKE pattern, so the patterns must be parameters."""
    for sql, params in feed.MEDIA_KINDS.values():
        assert sql.count("?") == len(params)
