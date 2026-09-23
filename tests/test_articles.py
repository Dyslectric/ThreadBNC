from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from threadbnc import articles, feed, media, storage
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
    if path == "/linking":  # links to another article, a section page, and itself
        return httpx.Response(200, content=page(extra='<p>See <a href="/2026/09/other-story-here">the other story</a>, '
                                                      '<a href="/world">world news</a> and '
                                                      '<a href="/linking#top">the top</a>.</p>'), headers=html)
    if path == "/2026/09/other-story-here":
        return httpx.Response(200, content=page("The other story"), headers=html)
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
    if path in ("/img/wall.png", "/img/own.png"):
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


def test_article_pictures_follow_the_posts_own(server, abouncer):
    """They're in the post's gallery, after its own pictures, but don't make
    the feed pick the pictures view."""
    server.add_post("1", "Harbour news", "![own](https://news.test/img/own.png)")
    server.edit_post("1", url="https://news.test/story")
    tid = abouncer.ingest_url(f"https://{DOMAIN}/post/1")
    abouncer.articles.fetch_pending()
    abouncer.media.fetch_pending()
    with abouncer.db.connect() as conn:
        cid = conn.execute("SELECT community_id FROM archived_threads WHERE id=?", (tid,)).fetchone()[0]
        own, wall = (conn.execute("SELECT id FROM media WHERE url=?", (f"https://news.test/img/{n}.png",)).fetchone()[0]
                     for n in ("own", "wall"))
        [item] = feed.load_feed(conn, community_id=cid).items
        assert item["article"] and item["thumb"]["id"] == own
        assert [p["id"] for p in item["thumb"]["pics"]] == [own, wall]
        assert feed.media_share(conn, cid) == (1, 1)


def test_feed_fetches_articles_scrolled_to(server, abouncer, settings):
    """A post shown in the feed with an unread article asks for it (app.js,
    once it's on screen); the article and its pictures are fetched, and the
    feed learns there's a picture to show."""
    tid = post_linking(server, abouncer, "https://news.test/story")
    with abouncer.db.connect() as conn:
        cid = conn.execute("SELECT community_id FROM archived_threads WHERE id=?", (tid,)).fetchone()[0]
    client = TestClient(create_app(settings, abouncer))
    client.post("/login", data={"password": "pw"})
    assert 'data-article-waiting data-pics="0"' in client.get(f"/c/{cid}").text
    assert client.get("/feed/articles", params={"ids": [tid]}).json() == {"waiting": [tid], "pics": {str(tid): 0}}

    assert client.post("/feed/articles", data={"ids": [tid]}).json()["ok"]
    assert abouncer.run_one_job()
    assert article_row(abouncer, "https://news.test/story")["status"] == "ok"
    assert client.get("/feed/articles", params={"ids": [tid]}).json() == {"waiting": [], "pics": {str(tid): 1}}
    page_now = client.get(f"/c/{cid}").text
    assert "data-article-waiting" not in page_now and "data-gallery" not in page_now
    with abouncer.db.connect() as conn:
        wall = conn.execute("SELECT id FROM media WHERE url='https://news.test/img/wall.png'").fetchone()[0]
    assert f'src="/media/{wall}"' in page_now


def test_feed_waits_for_article_pictures_too(server, abouncer, settings):
    """An article read (by opening the post) whose pictures aren't downloaded yet still waits."""
    tid = post_linking(server, abouncer, "https://news.test/story")
    abouncer.articles.fetch_pending()
    with abouncer.db.connect() as conn:
        oid = conn.execute("SELECT root_object_id FROM archived_threads WHERE id=?", (tid,)).fetchone()[0]
        assert articles.waiting(conn, [(oid, "https://news.test/story")]) == {oid}
    abouncer.fetch_articles([tid])
    with abouncer.db.connect() as conn:
        assert articles.waiting(conn, [(oid, "https://news.test/story")]) == set()


def test_feed_doesnt_ask_when_articles_are_off(server, bouncer, settings):
    tid = post_linking(server, bouncer, "https://news.test/story")
    bouncer.articles.enabled = False
    with bouncer.db.connect() as conn:
        cid = conn.execute("SELECT community_id FROM archived_threads WHERE id=?", (tid,)).fetchone()[0]
    client = TestClient(create_app(settings, bouncer))
    client.post("/login", data={"password": "pw"})
    assert "data-article-waiting" not in client.get(f"/c/{cid}").text
    client.post("/feed/articles", data={"ids": [tid]})
    assert not bouncer.run_one_job()


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


def test_storage_page_counts_articles(server, abouncer, settings):
    tid = post_linking(server, abouncer, "https://news.test/story")
    abouncer.articles.fetch_pending()
    abouncer.media.fetch_pending()
    with abouncer.db.connect() as conn:
        u = storage.overview(abouncer.db, conn, abouncer.media_dir)
        text = len(conn.execute("SELECT content_html FROM articles").fetchone()[0].encode())
    e = u["everything"]
    assert e.articles == 1 and e.article_text == text
    assert e.media["articles"] == [1, len(PNG)] and e.media["pictures"] == [0, 0]  # only the article uses it
    assert e.total == e.text + text + len(PNG)
    [row] = [k for k in u["kinds"] if k["label"] == "Linked articles"]
    assert row["count"] == 1 and row["bytes"] == text + len(PNG)
    assert not any(k["label"] == "Article pictures" for k in u["kinds"])  # part of that row, not its own
    [(_c, community)] = u["communities"]
    assert community.article_text == text and community.media["articles"] == [1, len(PNG)]

    client = TestClient(create_app(settings, abouncer))
    client.post("/login", data={"password": "pw"})
    page = client.get("/storage").text
    assert "Linked articles" in page and "1 picture" in page
    assert "of it from 1 linked article" in page  # in the community and thread Text columns

    # Once the post itself shows the picture, it's one of the post's pictures.
    server.edit_post("1", body="![wall](https://news.test/img/wall.png)")
    abouncer.sync_thread(tid, force=True)
    with abouncer.db.connect() as conn:
        e = storage.overview(abouncer.db, conn, abouncer.media_dir)["everything"]
    assert e.media["pictures"] == [1, len(PNG)] and e.media["articles"] == [0, 0]


def test_which_links_look_like_articles():
    for url in ("https://news.test/2026/09/some-story", "https://news.test/world/rebuild-the-harbour-wall",
                "https://news.test/story/123456", "https://news.test/a/b.html", "https://blog.test/my_long_post_title"):
        assert articles.looks_like_article(url), url
    for url in ("https://news.test/world", "https://news.test/", "https://news.test/about", "https://news.test/pic.jpg",
                "https://www.youtube.com/watch?v=abcdefghijk", "https://lemmy.test/post/123456", "mailto:x@y.test"):
        assert not articles.looks_like_article(url), url


def test_links_to_articles_are_read_here():
    html = ('<p><a href="https://news.test/2026/09/other-story">other</a> <a href="https://news.test/world">world</a> '
            '<a href="https://news.test/2026/09/this-story#notes">notes</a></p>')
    out = str(articles.render(html, {}.get, "https://news.test/2026/09/this-story", lambda u: "/read?url=" + u))
    assert '<a href="/read?url=https://news.test/2026/09/other-story"' in out and 'class="article-link"' in out
    assert 'href="https://news.test/world"' in out and 'target="_blank"' in out  # a section page: the site
    assert 'href="https://news.test/2026/09/this-story#notes"' in out  # the same page: left alone
    assert out.count("article-link") == 1


def reader(settings, bouncer):
    client = TestClient(create_app(settings, bouncer))
    client.post("/login", data={"password": "pw"})
    return client


def test_article_links_open_in_the_reader(server, abouncer, settings):
    tid = post_linking(server, abouncer, "https://news.test/linking")
    abouncer.articles.fetch_pending()
    client = reader(settings, abouncer)
    page = client.get(f"/t/{tid}/article").text
    link = "/read?url=https%3A%2F%2Fnews.test%2F2026%2F09%2Fother-story-here"
    assert f'href="{link}"' in page and "Read here · news.test" in page
    assert 'href="https://news.test/world"' in page
    # Keep and Repost act on the post's thread here (kept already: it was saved by link).
    assert f'action="/t/{tid}/unkeep"' in page
    # Following the link reads that article now, and shows it here.
    r = client.get(link, follow_redirects=False)
    aid = one_article(abouncer, "https://news.test/2026/09/other-story-here")["id"]
    assert r.status_code == 303 and r.headers["location"] == f"/a/{aid}"
    other = client.get(f"/a/{aid}").text
    assert "The other story" in other and "Paragraph 3." in other and f'action="/a/{aid}/keep"' in other
    # Its pictures are archived as the article's own (no post links to it).
    abouncer.media.fetch_pending()
    with abouncer.db.connect() as conn:
        pic = conn.execute("SELECT m.* FROM media m JOIN article_media am ON am.media_id=m.id "
                           "WHERE am.article_id=?", (aid,)).fetchone()
    assert pic["status"] == "ok" and f'src="/media/{pic["id"]}"' in client.get(f"/a/{aid}").text
    # Read once: opening it again doesn't fetch it again.
    assert client.get(link, follow_redirects=False).headers["location"] == f"/a/{aid}"



def test_kept_articles_list_the_articles_that_mention_them(server, abouncer, settings):
    client = reader(settings, abouncer)
    client.get("/read?url=https%3A%2F%2Fnews.test%2Flinking")
    linking = one_article(abouncer, "https://news.test/linking")
    client.get("/read?url=https%3A%2F%2Fnews.test%2F2026%2F09%2Fother-story-here")
    other = one_article(abouncer, "https://news.test/2026/09/other-story-here")
    with abouncer.db.connect() as conn:  # the section page and the #fragment link to itself are recorded too
        assert {r[0] for r in conn.execute("SELECT url FROM article_links WHERE article_id=?", (linking["id"],))} == {
            "https://news.test/2026/09/other-story-here", "https://news.test/world", "https://news.test/linking",
            "https://news.test/related"}
        assert [m["id"] for m in articles.mentioned_by(conn, other)] == [linking["id"]]
        assert articles.mentioned_by(conn, linking) == []  # linking to itself isn't a mention
    # Only kept articles show them.
    assert "Mentioned in" not in client.get(f"/a/{other['id']}").text
    client.post(f"/a/{other['id']}/keep")
    page = client.get(f"/a/{other['id']}").text
    assert "Mentioned in" in page and f'<a href="/a/{linking["id"]}" data-dive>' in page
    assert "No other article saved here links" not in page
    client.post(f"/a/{linking['id']}/keep")
    assert "No other article saved here links" in client.get(f"/a/{linking['id']}").text
    # A post's article shows them once the post is kept (this one is: it was saved by link).
    tid = post_linking(server, abouncer, "https://news.test/2026/09/other-story-here")
    assert f'<a href="/a/{linking["id"]}" data-dive>' in client.get(f"/t/{tid}/article").text
    # Recorded for articles read before this, and gone with the article.
    with abouncer.db.transaction() as conn:
        conn.execute("DELETE FROM article_links")
        articles.register_all_existing(conn)
        assert [m["id"] for m in articles.mentioned_by(conn, other)] == [linking["id"]]
        conn.execute("UPDATE articles SET kept_at=NULL, opened_at='2000-01-01T00:00:00Z' WHERE id=?", (linking["id"],))
        articles.collect_orphans(conn)
        assert conn.execute("SELECT COUNT(*) FROM article_links WHERE article_id=?", (linking["id"],)).fetchone()[0] == 0


def test_articles_open_beside_the_one_being_read(abouncer, settings):
    """app.js asks for just the article (pane=1), to show beside or inside the one you're reading."""
    client = reader(settings, abouncer)
    r = client.get("/read?url=https%3A%2F%2Fnews.test%2F2026%2F09%2Fother-story-here&pane=1", follow_redirects=False)
    aid = one_article(abouncer, "https://news.test/2026/09/other-story-here")["id"]
    assert r.headers["location"] == f"/a/{aid}?pane=1"
    pane = client.get(r.headers["location"]).text
    assert "<html" not in pane and "The other story" in pane and "data-dive-close" in pane
    assert f'data-src="/a/{aid}?pane=1"' in pane and "data-back" not in pane
    assert 'data-inplace="reader-actions-' in pane  # keeping it doesn't leave the page it's open on
    full = client.get(f"/a/{aid}").text
    assert "data-deck" in full and "data-dive-close" not in full and "data-back" in full


def one_article(b, url):
    with b.db.connect() as conn:
        return conn.execute("SELECT * FROM articles WHERE url=?", (url,)).fetchone()


def test_links_that_arent_readable(server, abouncer, settings):
    client = reader(settings, abouncer)
    # Not something we read (a picture): straight to it.
    r = client.get("/read?url=https%3A%2F%2Fnews.test%2Fwall.png", follow_redirects=False)
    assert r.headers["location"] == "https://news.test/wall.png"
    assert client.get("/read?url=javascript%3Aalert(1)").status_code == 400
    # Too little text to be an article: says so, with the original to open instead.
    r = client.get("/read?url=https%3A%2F%2Fnews.test%2F2026%2Fteaser-of-a-story", follow_redirects=True)
    assert "Couldn&#39;t read this page here" in r.text or "Couldn't read this page here" in r.text
    assert "Open the original" in r.text


def test_articles_read_from_links_can_be_kept_or_go_after_a_while(abouncer, settings):
    from datetime import datetime, timedelta, timezone
    from threadbnc.db import fmt_ts, utcnow
    client = reader(settings, abouncer)
    client.get("/read?url=https%3A%2F%2Fnews.test%2Fstory")
    kept = one_article(abouncer, "https://news.test/story")
    client.get("/read?url=https%3A%2F%2Fnews.test%2F2026%2F09%2Fother-story-here")
    other = one_article(abouncer, "https://news.test/2026/09/other-story-here")
    client.post(f"/a/{kept['id']}/keep")
    assert "Harbour wall to be rebuilt" in client.get("/kept?tab=articles").text
    long_ago = fmt_ts(datetime.now(timezone.utc) - timedelta(days=articles.STANDALONE_DAYS + 1))
    with abouncer.db.transaction() as conn:
        conn.execute("UPDATE articles SET opened_at=?", (long_ago,))
        articles.collect_orphans(conn, utcnow())
    assert one_article(abouncer, kept["url"]) is not None  # kept
    assert one_article(abouncer, other["url"]) is None  # a month unopened, nothing links to it
    with abouncer.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM article_media WHERE article_id=?", (other["id"],)).fetchone()[0] == 0
    client.post(f"/a/{kept['id']}/unkeep")
    assert one_article(abouncer, kept["url"])["kept_at"] is None


def test_an_article_read_here_can_be_posted(server, abouncer, settings):
    settings.credentials_key = "test-key"
    server.add_post("1", "Harbour news", "")
    abouncer.ingest_url(f"https://{DOMAIN}/post/1")  # so there's a community to post in
    client = reader(settings, abouncer)
    client.post("/accounts", data={"server": "home.test", "username": "dave", "password": "hunter2"})
    client.get("/read?url=https%3A%2F%2Fnews.test%2F2026%2F09%2Fother-story-here")
    a = one_article(abouncer, "https://news.test/2026/09/other-story-here")
    form = client.get(f"/a/{a['id']}/post").text
    assert 'value="The other story"' in form and "&gt; The council voted" in form
    with abouncer.db.connect() as conn:
        cid = conn.execute("SELECT id FROM communities").fetchone()[0]
    r = client.post(f"/a/{a['id']}/post", data={"title": "The other story", "url": a["url"], "body": "> excerpt",
                                                "community_id": str(cid)}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/t/")
    new = [p for p in server.posts.values() if p.title == "The other story"]
    assert new and new[0].url == a["url"]


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
