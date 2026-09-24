"""Opening a post fetches what a browser would: its comments (unless read in
the last few minutes), the article it links to, and its videos."""

from __future__ import annotations

from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from threadbnc import articles, media
from threadbnc.db import fmt_ts, parse_ts, utcnow
from threadbnc.web import create_app

from .conftest import DOMAIN, FakeAdapter
from .test_articles import fake_web

MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 50
GIF = b"GIF89a" + b"\x00" * 50


def fake_files(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path.endswith(".gif"):
        return httpx.Response(200, content=GIF, headers={"content-type": "image/gif"})
    if path.endswith(".mp4") or path == "/pictrs/image/abc":  # a video without a telling name
        return httpx.Response(200, content=MP4, headers={"content-type": "video/mp4"})
    return fake_web(request)


@pytest.fixture
def web(settings, bouncer):
    bouncer.requests = []

    def counting(request):
        bouncer.requests.append(str(request.url))
        return fake_files(request)

    client = httpx.Client(transport=httpx.MockTransport(counting))
    bouncer.articles = articles.ArticleFetcher(bouncer.db, "t", client=client, check_host=False)
    bouncer.media = media.MediaFetcher(bouncer.db, bouncer.media_dir, "t", 10_000_000, client=client,
                                       check_host=False)
    c = TestClient(create_app(settings, bouncer))
    assert c.post("/login", data={"password": "pw"}, follow_redirects=False).status_code == 303
    return c


def one(b, sql, *args):
    with b.db.connect() as conn:
        return conn.execute(sql, args).fetchone()


def jobs(b, kind="open"):
    with b.db.connect() as conn:
        return conn.execute("SELECT * FROM jobs WHERE kind=? ORDER BY id", (kind,)).fetchall()


def run_jobs(b):
    while b.run_one_job():
        pass


def age_comments(b, tid, minutes):
    when = fmt_ts(parse_ts(utcnow()) - timedelta(minutes=minutes))
    with b.db.transaction() as conn:
        conn.execute("UPDATE archived_threads SET last_full_fetch_at=? WHERE id=?", (when, tid))


@pytest.fixture
def counted(monkeypatch):
    calls = []
    real = FakeAdapter.fetch_comments

    def fetch_comments(self, post_local_id):
        calls.append(post_local_id)
        return real(self, post_local_id)

    monkeypatch.setattr(FakeAdapter, "fetch_comments", fetch_comments)
    return calls


def test_opening_a_post_reads_new_comments(server, bouncer, web, counted):
    server.add_post("1", "hello", "b")
    tid = bouncer.ingest_url(f"https://{DOMAIN}/post/1")
    server.add_comment("1", "10", "arrived after capture")
    age_comments(bouncer, tid, 6)
    page = web.get(f"/t/{tid}").text
    assert 'id="refreshing"' in page and "arrived after capture" not in page
    [job] = jobs(bouncer)
    run_jobs(bouncer)
    assert one(bouncer, "SELECT status FROM jobs WHERE id=?", job["id"])["status"] == "done"
    fresh = web.get(f"/t/{tid}?refreshed=1").text
    assert "arrived after capture" in fresh and 'id="refreshing"' not in fresh


def test_comments_read_in_the_last_five_minutes_are_not_read_again(server, bouncer, web, counted):
    server.add_post("1", "hello", "b")
    tid = bouncer.ingest_url(f"https://{DOMAIN}/post/1")
    counted.clear()
    age_comments(bouncer, tid, 4)
    assert 'id="refreshing"' not in web.get(f"/t/{tid}").text
    assert jobs(bouncer) == []
    # A second open racing the first: the job itself checks again.
    bouncer.open_threads([tid])
    assert counted == []


def test_expanding_comments_forces_a_check_unless_the_community_is_pushed(server, bouncer, web):
    server.add_post("1", "hello", "b")
    tid = bouncer.ingest_url(f"https://{DOMAIN}/post/1")
    result = web.post(f"/t/{tid}/comments/check").json()
    assert result["thread_ids"] == [tid] and len(result["jobs"]) == 1
    assert len(jobs(bouncer, "sync")) == 1

    cid = one(bouncer, "SELECT community_id FROM archived_threads WHERE id=?", tid)[0]
    now = utcnow()
    with bouncer.db.transaction() as conn:
        conn.execute(
            "INSERT INTO community_follows(community_id, followed_at, capture_since, poll_interval_minutes, "
            "retention_days, source_domain, source_ref, next_poll_at, polling, push_state) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (cid, now, now, 10, 7, DOMAIN, f"math@{DOMAIN}", now, 0, "subscribed"))
    pushed = web.post(f"/t/{tid}/comments/check").json()
    assert pushed == {"jobs": [], "thread_ids": []}
    assert len(jobs(bouncer, "sync")) == 1


def test_the_fresh_copy_is_not_another_visit(server, bouncer, web):
    server.add_post("1", "hello", "b")
    tid = bouncer.ingest_url(f"https://{DOMAIN}/post/1")
    age_comments(bouncer, tid, 10)
    web.get(f"/t/{tid}")
    before = one(bouncer, "SELECT last_viewed_at, prev_viewed_at FROM archived_threads WHERE id=?", tid)
    web.get(f"/t/{tid}?refreshed=1")
    after = one(bouncer, "SELECT last_viewed_at, prev_viewed_at FROM archived_threads WHERE id=?", tid)
    assert tuple(before) == tuple(after)
    assert len(jobs(bouncer)) == 1


def test_comments_new_since_the_last_visit_stay_highlighted_in_the_fresh_copy(server, bouncer, web):
    server.add_post("1", "hello", "b")
    tid = bouncer.ingest_url(f"https://{DOMAIN}/post/1")
    web.get(f"/t/{tid}")  # a first visit
    server.add_comment("1", "10", "new since then")
    age_comments(bouncer, tid, 10)
    web.get(f"/t/{tid}")  # the next visit, which asks for the comments
    run_jobs(bouncer)
    assert 'class="comment d0 is-new' in web.get(f"/t/{tid}?refreshed=1").text


def test_feed_articles_have_no_comments_to_read(server, bouncer, web, counted):
    server.add_post("1", "hello", "b")
    tid = bouncer.ingest_url(f"https://{DOMAIN}/post/1")
    with bouncer.db.transaction() as conn:
        conn.execute("UPDATE archived_threads SET source_domain='rss' WHERE id=?", (tid,))
    t = one(bouncer, "SELECT * FROM archived_threads WHERE id=?", tid)
    assert bouncer.comments_stale(t) is False


def test_opening_a_post_saves_its_article(server, bouncer, web):
    server.add_post("1", "Harbour news", "")
    server.edit_post("1", url="https://news.test/story")
    tid = bouncer.ingest_url(f"https://{DOMAIN}/post/1")
    age_comments(bouncer, tid, 1)  # comments fresh: the article alone asks for the job
    assert "Saving the linked article" in web.get(f"/t/{tid}").text
    run_jobs(bouncer)
    a = one(bouncer, "SELECT * FROM articles WHERE url=?", "https://news.test/story")
    assert a["status"] == "ok"
    assert "Read article" in web.get(f"/t/{tid}?refreshed=1").text


def test_videos_wait_until_the_post_is_opened(server, bouncer, web):
    server.add_post("1", "clip", "![still](https://img.test/still.gif) [clip](https://img.test/clip.mp4) "
                                 "![also](https://img.test/pictrs/image/abc)")
    tid = bouncer.ingest_url(f"https://{DOMAIN}/post/1", "auto")
    bouncer.media.fetch_pending()
    rows = {r["url"]: r for r in _media(bouncer)}
    assert rows["https://img.test/still.gif"]["status"] == "ok"  # pictures at once
    for url in ("https://img.test/clip.mp4", "https://img.test/pictrs/image/abc"):
        assert rows[url]["status"] == "pending" and rows[url]["held"] == 1 and rows[url]["attempts"] == 0
    # Only the one without a telling name was asked about, and only once.
    assert [u for u in bouncer.requests if "clip" in u] == []
    assert bouncer.requests.count("https://img.test/pictrs/image/abc") == 1
    bouncer.media.fetch_pending()
    assert bouncer.requests.count("https://img.test/pictrs/image/abc") == 1

    web.get(f"/t/{tid}")
    bouncer.media.fetch_pending()
    rows = {r["url"]: r for r in _media(bouncer)}
    assert rows["https://img.test/clip.mp4"]["status"] == "ok"
    assert rows["https://img.test/pictrs/image/abc"]["status"] == "ok"


def test_marking_read_does_not_fetch_videos(server, bouncer, web):
    server.add_post("1", "clip", "[clip](https://img.test/clip.mp4)")
    tid = bouncer.ingest_url(f"https://{DOMAIN}/post/1", "auto")
    web.post(f"/t/{tid}/read", data={"read": "1"}, follow_redirects=False)
    bouncer.media.fetch_pending()
    assert _media(bouncer)[0]["status"] == "pending"


def test_keeping_a_post_fetches_its_article_and_videos(server, bouncer, web):
    server.add_post("1", "clip", "[clip](https://img.test/clip.mp4)")
    server.edit_post("1", url="https://news.test/story")
    cid = bouncer.follow_community(f"!math@{DOMAIN}", 15, None, backfill=True)
    bouncer.poll_follow(cid)
    tid = one(bouncer, "SELECT id FROM archived_threads")["id"]
    web.post(f"/t/{tid}/keep", follow_redirects=False)
    assert len(jobs(bouncer)) == 1
    run_jobs(bouncer)
    bouncer.media.fetch_pending()
    assert one(bouncer, "SELECT status FROM articles WHERE url=?", "https://news.test/story")["status"] == "ok"
    assert _media(bouncer)[0]["status"] == "ok"


def test_keeping_by_link_saves_the_article(server, bouncer, web):
    server.add_post("1", "Harbour news", "")
    server.edit_post("1", url="https://news.test/story")
    bouncer.enqueue("ingest", {"url": f"https://{DOMAIN}/post/1", "retention": "manual"})
    run_jobs(bouncer)
    assert one(bouncer, "SELECT status FROM articles WHERE url=?", "https://news.test/story")["status"] == "ok"


def _media(b):
    with b.db.connect() as conn:
        return conn.execute("SELECT * FROM media ORDER BY id").fetchall()
