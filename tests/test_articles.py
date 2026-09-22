from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from threadbnc import articles, feed, media
from threadbnc.render import MediaInfo
from threadbnc.web import create_app

from .conftest import DOMAIN

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 50
PARA = ("The council voted on Tuesday to rebuild the old harbour wall, which has been crumbling for a decade "
        "and flooded the market square twice last winter. ")


def page(title: str = "Harbour wall to be rebuilt", paragraphs: int = 4, extra: str = "") -> bytes:
    body = "".join(f"<p>{PARA}Paragraph {n}.</p>" for n in range(paragraphs))
    return f"""<!doctype html><html><head><title>{title} | Gazette</title>
<meta property="og:title" content="{title}"><meta property="og:site_name" content="The Gazette">
<meta name="author" content="Pat Writer"><meta property="article:published_time" content="2026-09-20T08:00:00Z">
<script>alert("tracking")</script></head><body>
<nav><a href="/">Home</a> <a href="/sport">Sport</a> <a href="/weather">Weather</a></nav>
<article><h1>{title}</h1>
<figure><img src="/img/wall.png" alt="The wall"><figcaption>The wall in March</figcaption></figure>
{body}{extra}
<p>Read <a href="/related">the earlier story</a>.</p></article>
<footer>Copyright The Gazette. <a href="/privacy">Privacy</a></footer></body></html>""".encode()


def fake_web(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    html = {"content-type": "text/html; charset=utf-8"}
    if path == "/story":
        return httpx.Response(200, content=page(), headers=html)
    if path == "/moved":
        return httpx.Response(301, headers={"location": "/story"})
    if path == "/teaser":
        return httpx.Response(200, content=page(paragraphs=0), headers=html)
    if path == "/paywalled":
        return httpx.Response(403, content=b"no", headers=html)
    if path == "/report.pdf":
        return httpx.Response(200, content=b"%PDF-1.7", headers={"content-type": "application/pdf"})
    if path == "/down":
        return httpx.Response(503)
    if path == "/img/wall.png":
        return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})
    return httpx.Response(404)


@pytest.fixture
def abouncer(bouncer):
    client = httpx.Client(transport=httpx.MockTransport(fake_web))
    bouncer.articles = articles.ArticleFetcher(bouncer.db, "t", client=client, check_host=False)
    bouncer.media = media.MediaFetcher(bouncer.db, bouncer.media_dir, "t", 10_000_000, client=client,
                                       check_host=False)
    return bouncer


def post_linking(server, bouncer, url: str, local_id: str = "1") -> int:
    server.add_post(local_id, "Harbour news", "")
    server.edit_post(local_id, url=url)
    return bouncer.ingest_url(f"https://{DOMAIN}/post/{local_id}")


def article_row(b, url: str):
    with b.db.connect() as conn:
        return conn.execute("SELECT * FROM articles WHERE url=?", (url,)).fetchone()


def test_candidates():
    assert articles.candidate("https://news.test/2026/story") == "https://news.test/2026/story"
    for url in ("https://news.test/", "https://news.test", "https://news.test/pic.jpg", "https://youtu.be/abc",
                "https://www.youtube.com/watch?v=abc", "https://old.reddit.com/r/x/comments/abc/t/",
                "https://lemmy.test/post/12", "https://x.com/someone/status/1", "ftp://news.test/story", None):
        assert articles.candidate(url) is None, url


def test_extract_keeps_the_article_only():
    art = articles.extract(page(extra='<p onclick="x()">Hi<script>bad()</script></p>'), "https://news.test/story")
    assert art.title == "Harbour wall to be rebuilt" and art.site_name == "The Gazette"
    assert art.byline == "Pat Writer" and art.published == "2026-09-20"
    assert art.words >= articles.MIN_WORDS
    assert "Paragraph 3." in art.content_html
    assert "Sport" not in art.content_html and "Privacy" not in art.content_html  # site navigation dropped
    assert "<h1" not in art.content_html  # the headline is shown as the title instead
    assert "script" not in art.content_html and "onclick" not in art.content_html
    assert art.images == ["https://news.test/img/wall.png"]  # made absolute
    assert 'href="https://news.test/related"' in art.content_html


def test_extract_gives_up_on_teasers():
    with pytest.raises(articles.ArticleSkipped):
        articles.extract(page(paragraphs=0), "https://news.test/teaser")


def test_render_uses_archived_pictures():
    html = '<p>Hi</p><img src="https://news.test/a.png" alt="A"><img src="https://news.test/b.png" alt="B">'
    out = str(articles.render(html, {"https://news.test/a.png": MediaInfo(4, "ok", "image/png")}.get))
    assert 'src="/media/4"' in out
    assert "news.test/a.png" not in out
    assert "<img" in out and out.count("<img") == 1  # b.png isn't archived: a note, not a hotlink
    assert "archiving pending" in out


def test_article_read_and_shown(server, abouncer, settings):
    tid = post_linking(server, abouncer, "https://news.test/moved")
    assert article_row(abouncer, "https://news.test/moved")["status"] == "pending"
    abouncer.articles.fetch_pending()
    a = article_row(abouncer, "https://news.test/moved")
    assert a["status"] == "ok" and a["fetched_from"] == "https://news.test/story"
    assert a["title"] == "Harbour wall to be rebuilt"
    # The article's picture is archived as the post's media.
    abouncer.media.fetch_pending()
    with abouncer.db.connect() as conn:
        pic = conn.execute("SELECT * FROM media WHERE url='https://news.test/img/wall.png'").fetchone()
        cid = conn.execute("SELECT community_id FROM archived_threads WHERE id=?", (tid,)).fetchone()[0]
    assert pic["status"] == "ok"

    client = TestClient(create_app(settings, abouncer))
    client.post("/login", data={"password": "pw"})
    assert f'href="/t/{tid}/article"' in client.get(f"/t/{tid}").text
    assert f'href="/t/{tid}/article"' in client.get(f"/c/{cid}").text
    r = client.get(f"/t/{tid}/article")
    assert r.status_code == 200
    assert "Harbour wall to be rebuilt" in r.text and "Paragraph 3." in r.text and "Pat Writer" in r.text
    assert f'src="/media/{pic["id"]}"' in r.text and "news.test/img" not in r.text


def test_article_pictures_arent_the_posts_own(server, abouncer):
    """They're shown in the article, not as the post's thumbnail or gallery."""
    tid = post_linking(server, abouncer, "https://news.test/story")
    abouncer.articles.fetch_pending()
    abouncer.media.fetch_pending()
    with abouncer.db.connect() as conn:
        cid = conn.execute("SELECT community_id FROM archived_threads WHERE id=?", (tid,)).fetchone()[0]
        [item] = feed.load_feed(conn, community_id=cid).items
        assert item["thumb"] is None and item["article"]
        assert feed.media_share(conn, cid) == (0, 1)
    # Once the post itself embeds the same picture, it is its own.
    server.edit_post("1", body="![wall](https://news.test/img/wall.png)")
    abouncer.sync_thread(tid, force=True)
    with abouncer.db.connect() as conn:
        assert feed.load_feed(conn, community_id=cid).items[0]["thumb"] is not None


def test_article_pictures_survive_restart_cleanup(server, abouncer):
    """Pictures without a file extension aren't mistaken for unprobed post links."""
    tid = post_linking(server, abouncer, "https://news.test/story")
    abouncer.articles.fetch_pending()
    with abouncer.db.transaction() as conn:
        oid = conn.execute("SELECT root_object_id FROM archived_threads WHERE id=?", (tid,)).fetchone()[0]
        conn.execute("UPDATE articles SET images_json='[\"https://cdn.test/pic/123\"]'")
        media.register(conn, oid, ["https://cdn.test/pic/123"], "2026-09-01T00:00:00Z", from_article=True)
        media.skip_unprobed_links(conn)
        assert conn.execute("SELECT status FROM media WHERE url='https://cdn.test/pic/123'").fetchone()[0] == "pending"


def test_same_link_twice_is_read_once(server, abouncer):
    post_linking(server, abouncer, "https://news.test/story", "1")
    abouncer.articles.fetch_pending()
    post_linking(server, abouncer, "https://news.test/story", "2")
    assert abouncer.articles.fetch_pending() == 0
    with abouncer.db.connect() as conn:
        # ...and the second post gets the article's pictures too.
        assert conn.execute("SELECT COUNT(*) FROM media_refs r JOIN media m ON m.id=r.media_id "
                            "WHERE m.url='https://news.test/img/wall.png'").fetchone()[0] == 2


@pytest.mark.parametrize("path, status", [("/paywalled", "failed"), ("/teaser", "skipped"),
                                          ("/report.pdf", "skipped"), ("/gone", "failed")])
def test_unreadable_links(server, abouncer, settings, path, status):
    tid = post_linking(server, abouncer, f"https://news.test{path}")
    abouncer.articles.fetch_pending()
    assert article_row(abouncer, f"https://news.test{path}")["status"] == status
    client = TestClient(create_app(settings, abouncer))
    client.post("/login", data={"password": "pw"})
    assert client.get(f"/t/{tid}/article").status_code == 404
    assert f'href="/t/{tid}/article"' not in client.get(f"/t/{tid}").text


def test_server_errors_are_retried_then_can_be_retried_by_hand(server, abouncer, settings):
    tid = post_linking(server, abouncer, "https://news.test/down")
    abouncer.articles.fetch_pending()
    a = article_row(abouncer, "https://news.test/down")
    assert a["status"] == "pending" and a["attempts"] == 1 and a["next_attempt_at"] > a["first_seen_at"]
    with abouncer.db.transaction() as conn:
        conn.execute("UPDATE articles SET status='failed'")
    client = TestClient(create_app(settings, abouncer))
    client.post("/login", data={"password": "pw"})
    assert "Try again" in client.get(f"/t/{tid}").text
    client.post(f"/t/{tid}/article/retry")
    assert article_row(abouncer, "https://news.test/down")["status"] == "pending"


def test_turned_off(server, abouncer):
    abouncer.articles.enabled = False
    post_linking(server, abouncer, "https://news.test/story")
    assert abouncer.articles.fetch_pending() == 0
    assert article_row(abouncer, "https://news.test/story")["status"] == "pending"


def test_purged_with_the_thread(server, abouncer):
    tid = post_linking(server, abouncer, "https://news.test/story")
    abouncer.articles.fetch_pending()
    abouncer.move_to_trash(tid)
    abouncer.delete_from_trash([tid])
    assert article_row(abouncer, "https://news.test/story") is None
