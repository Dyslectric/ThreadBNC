from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from threadbnc import articles, links, media
from threadbnc.adapters import NActor, NCommunity, RemoteAuthError, RemoteUnavailable
from threadbnc.db import utcnow
from threadbnc.web import create_app

from .conftest import DOMAIN, FakeAdapter
from .test_articles import PARA

STORY = "https://news.test/2026/09/harbour-wall"
OTHER_PARA = ("A second paper carried the council's decision word for word, as papers do with wire copy, "
              "under a headline of its own. ")


def page(title: str, head: str = "", para: str = PARA, paragraphs: int = 6) -> bytes:
    body = "".join(f"<p>{para}Paragraph {n}.</p>" for n in range(paragraphs))
    return f"""<!doctype html><html><head><title>{title}</title>{head}
<meta property="article:published_time" content="2026-09-20T08:00:00Z"></head>
<body><article><h1>{title}</h1>{body}</article></body></html>""".encode()


def fake_web(request: httpx.Request) -> httpx.Response:
    html = {"content-type": "text/html; charset=utf-8"}
    url = str(request.url).split("?")[0]
    own = f'<link rel="canonical" href="{STORY}">'
    if url == STORY:
        return httpx.Response(200, headers=html, content=page("Harbour wall to be rebuilt after floods", own + (
            '<link rel="alternate" type="application/activity+json" href="https://news.test/?p=5">'
            '<link rel="webmention" href="https://webmention.io/news.test/webmention">')))
    if url == "https://news.test/s/abc":  # a short link
        return httpx.Response(301, headers={"location": STORY})
    if url == "https://mirror.test/syndicated":  # a copy that says where the original lives
        return httpx.Response(200, headers=html, content=page("Harbour wall to be rebuilt after floods", own))
    if url == "https://other.test/2026/wall":  # the same wire story, on another paper's page
        return httpx.Response(200, headers=html, content=page("Council backs harbour wall rebuild"))
    if url == "https://third.test/2026/wall":  # a different story, the same headline the same week
        return httpx.Response(200, headers=html, content=page("Harbour wall to be rebuilt after floods",
                                                              para=OTHER_PARA))
    return httpx.Response(404)


@pytest.fixture
def dbouncer(bouncer):
    client = httpx.Client(transport=httpx.MockTransport(fake_web))
    bouncer.articles = articles.ArticleFetcher(bouncer.db, "t", client=client, check_host=False)
    bouncer.media = media.MediaFetcher(bouncer.db, bouncer.media_dir, "t", 10_000_000, client=client,
                                       check_host=False)
    return bouncer


def post_linking(server, bouncer, url: str, local_id: str, title: str = "Harbour news") -> int:
    server.add_post(local_id, title, "")
    server.edit_post(local_id, url=url)
    return bouncer.ingest_url(f"https://{DOMAIN}/post/{local_id}")


def run_jobs(b) -> None:
    while b.run_one_job():
        pass


def client_for(settings, b):
    client = TestClient(create_app(settings, b))
    client.post("/login", data={"password": "pw"})
    return client


def article_of(b, tid: int):
    with b.db.connect() as conn:
        root = conn.execute("SELECT root_object_id FROM archived_threads WHERE id=?", (tid,)).fetchone()[0]
        return articles.for_object(conn, root)


# --- telling links apart ---------------------------------------------------------------

@pytest.mark.parametrize("url", [
    "https://www.news.test/2026/09/harbour-wall?utm_source=rss&utm_medium=feed",
    "http://news.test/2026/09/harbour-wall/",
    "https://news.test/2026/09/harbour-wall/amp",
    "https://amp.news.test/2026/09/harbour-wall",
    "https://news.test/2026/09/harbour-wall?outputType=amp",
    "https://www.google.com/amp/s/news.test/2026/09/harbour-wall/amp",
    "https://news-test.cdn.ampproject.org/c/s/news.test/2026/09/harbour-wall",
    "https://web.archive.org/web/20260920080000/https://news.test/2026/09/harbour-wall",
    "https://archive.ph/newest/https://news.test/2026/09/harbour-wall",
    "https://12ft.io/https://news.test/2026/09/harbour-wall",
    "https://www.google.com/url?q=https://news.test/2026/09/harbour-wall&sa=D",
    "https://l.facebook.com/l.php?u=https%3A%2F%2Fnews.test%2F2026%2F09%2Fharbour-wall&h=x",
    "https://news.test/2026/09/harbour-wall/index.html",
])
def test_links_to_the_same_page_have_the_same_key(url):
    assert links.key(url) == "news.test/2026/09/harbour-wall"


def test_links_to_other_pages_dont():
    assert links.key("https://news.test/2026/09/harbour-wall?id=5") != links.key(STORY)
    assert links.key("https://news.test/2026/09/other") != links.key(STORY)
    assert links.key("https://archive.ph/AbCdE") == "archive.ph/AbCdE"  # nothing inside to go by
    assert links.key("mailto:x@news.test") is None


def test_what_a_page_says_about_itself():
    found = links.page_links(page("x", '<link rel="canonical" href="/2026/09/harbour-wall">'
                                       '<link rel="alternate" type=\'application/ld+json; '
                                       'profile="https://www.w3.org/ns/activitystreams"\' href="/?p=5">'
                                       '<link rel="webmention" href="https://webmention.io/n/webmention">'),
                             "https://news.test/2026/09/harbour-wall?utm_source=rss")
    assert found == {"canonical": STORY, "activitypub": "https://news.test/?p=5",
                     "webmention": "https://webmention.io/n/webmention"}
    # Some sites point every page at their home page: that says nothing.
    assert links.page_links(page("x", '<meta property="og:url" content="https://news.test/">'),
                            STORY)["canonical"] is None


def test_headlines_and_text_tell_the_same_story():
    assert links.title_key("Harbour wall to be rebuilt | The Gazette", "The Gazette") == \
        links.title_key("Harbour wall to be rebuilt - BBC News") == "harbour wall to be rebuilt"
    assert links.title_key("Live updates") is None  # too short to go by
    a = links.simhash(PARA * 10)
    b = links.simhash(PARA * 10 + "One more line at the end.")
    c = links.simhash(OTHER_PARA * 10)
    assert a is not None and links.near(a, b) and not links.near(a, c)


# --- posts of the same page, by other links ----------------------------------------------

def test_posts_of_the_same_page_find_each_other(server, dbouncer, settings):
    """An RSS-style link with tracking, a short link and a syndicated copy
    that names the original are all posts of the same article."""
    t1 = post_linking(server, dbouncer, STORY + "?utm_source=rss&utm_medium=feed", "1", "From the feed")
    t2 = post_linking(server, dbouncer, "https://news.test/s/abc", "2", "Short link")
    t3 = post_linking(server, dbouncer, "https://mirror.test/syndicated", "3", "Syndicated copy")
    dbouncer.articles.fetch_pending()
    client = client_for(settings, dbouncer)
    page1 = client.get(f"/t/{t1}").text
    assert "Discussions" in page1 and "Also posted here" in page1
    assert f'href="/t/{t2}"' in page1 and f'href="/t/{t3}"' in page1
    page3 = client.get(f"/t/{t3}").text
    assert f'href="/t/{t1}"' in page3 and f'href="/t/{t2}"' in page3
    reader = client.get(f"/t/{t2}/article").text
    assert f'href="/t/{t1}"' in reader and f'href="/t/{t3}"' in reader
    a = article_of(dbouncer, t3)
    assert a["canonical_url"] == STORY and a["fetched_from"] == "https://mirror.test/syndicated"


def test_keys_are_filled_in_for_articles_read_before(server, dbouncer):
    post_linking(server, dbouncer, STORY + "?utm_source=rss", "1")
    dbouncer.articles.fetch_pending()
    with dbouncer.db.transaction() as conn:
        conn.execute("DELETE FROM article_keys")
        conn.execute("UPDATE article_links SET link_key=NULL")
        conn.execute("UPDATE articles SET simhash=NULL, title_key=NULL")
        articles.register_all_existing(conn)
        a = conn.execute("SELECT * FROM articles").fetchone()
        assert set(articles.keys_of(conn, a["id"])) == {links.key(STORY)}
        assert a["simhash"] and a["title_key"] == "harbour wall to be rebuilt after floods"


def test_probably_the_same_story(server, dbouncer, settings):
    t1 = post_linking(server, dbouncer, STORY, "1")
    t2 = post_linking(server, dbouncer, "https://other.test/2026/wall", "2", "Other paper")
    t3 = post_linking(server, dbouncer, "https://third.test/2026/wall", "3", "Third paper")
    dbouncer.articles.fetch_pending()
    client = client_for(settings, dbouncer)
    page1 = client.get(f"/t/{t1}").text
    assert "Probably the same story" in page1
    others = {a["id"]: a for a in (article_of(dbouncer, t2), article_of(dbouncer, t3))}
    for aid in others:  # the same text on another paper; the same headline the same week
        assert f'href="/a/{aid}"' in page1
    assert f'href="/t/{t2}"' in page1  # with where it was posted
    assert "Also posted here" not in page1  # another story isn't the same page


# --- looking elsewhere ---------------------------------------------------------------------

def lemmy_post(local_id: str, url: str, community: str = "tech", domain: str = DOMAIN):
    from threadbnc.adapters import NPost
    return NPost(ap_id=f"https://{domain}/post/{local_id}", local_id=local_id, title=f"Posted in {community}",
                 body=None, url=url, created_at="2026-09-21T00:00:00.000000Z", updated_at=None, deleted=False,
                 removed=False, locked=False,
                 community=NCommunity(f"https://{domain}/c/{community}", community, domain),
                 author=NActor(f"https://{domain}/u/zed", "zed", domain), score=12, comment_count=4)


@pytest.fixture
def looking(server, dbouncer, settings, monkeypatch):
    """Lemmy, Bluesky, the blog's replies and webmention.io, answering."""
    asked: dict[str, list] = {"lemmy": [], "bluesky": [], "ap": [], "webmention": []}

    def posts_linking(self, url, limit=20):
        asked["lemmy"].append((self.domain, url))
        return [lemmy_post("77", STORY, "tech"), lemmy_post("78", "https://elsewhere.test/story", "misc")]

    monkeypatch.setattr(FakeAdapter, "posts_linking", posts_linking, raising=False)

    def bluesky(url, limit=25):
        asked["bluesky"].append(url)
        return [{"uri": "at://did:plc:abc/app.bsky.feed.post/3k", "author": {"handle": "pat.test"},
                 "record": {"text": "Good news for the harbour", "createdAt": "2026-09-21T10:00:00Z"},
                 "likeCount": 9, "replyCount": 1}]

    monkeypatch.setattr(dbouncer.bluesky_adapter, "posts_linking", bluesky)
    replies = {"https://news.test/?p=5": {"type": "Article", "replies": "https://news.test/?p=5&replies"},
               "https://news.test/?p=5&replies": {"type": "Collection", "first": {
                   "type": "CollectionPage", "items": ["https://mastodon.test/users/carol/statuses/1"]}},
               "https://mastodon.test/users/carol/statuses/1": {
                   "type": "Note", "id": "https://mastodon.test/users/carol/statuses/1",
                   "url": "https://mastodon.test/@carol/1", "attributedTo": "https://mastodon.test/users/carol",
                   "content": "<p>About time they fixed that wall.</p>", "published": "2026-09-21T11:00:00Z"}}

    real_get_json = dbouncer.http.get_json

    def get_json(domain, path, params=None, token=None):
        if domain == "webmention.io":
            asked["webmention"].append(params["target"])
            return {"children": [
                {"type": "entry", "wm-property": "in-reply-to", "url": "https://blog.test/replies/9",
                 "author": {"name": "Sam"}, "content": {"text": "I wrote about this too."},
                 "published": "2026-09-22T09:00:00Z"},
                {"type": "entry", "wm-property": "like-of", "url": "https://blog.test/likes/1"}]}
        return real_get_json(domain, path, params, token)

    monkeypatch.setattr(dbouncer.http, "get_json", get_json)
    client = client_for(settings, dbouncer)
    finder = dbouncer.job_handlers["discussions"].__self__
    finder.lemmy_servers = lambda: [DOMAIN]
    finder.check_host = False

    def ap_get(url):
        asked["ap"].append(url)
        return replies[url]

    monkeypatch.setattr(finder, "_ap_get", ap_get)
    return client, asked


def test_opening_a_post_looks_elsewhere(server, dbouncer, looking):
    client, asked = looking
    tid = post_linking(server, dbouncer, STORY + "?utm_source=rss", "1")
    page1 = client.get(f"/t/{tid}").text
    assert "Looking for more elsewhere" in page1
    run_jobs(dbouncer)
    a = article_of(dbouncer, tid)
    # Asked by the page's own address first, then the link as posted.
    assert asked["lemmy"] == [(DOMAIN, STORY), (DOMAIN, STORY + "?utm_source=rss")]
    assert asked["bluesky"] == [STORY] and asked["webmention"] == [STORY]
    part = client.get(f"/a/{a['id']}/discussions?t={tid}").text
    assert "data-job" not in part
    assert "Posted in tech" in part and f"!tech@{DOMAIN}" in part and "4 comments" in part
    assert "Posted in misc" not in part  # a search turned it up, but it links somewhere else
    assert "Good news for the harbour" in part and "@pat.test" in part and "1 reply" in part
    assert "https://bsky.app/profile/did:plc:abc/post/3k" in part
    assert "About time they fixed that wall." in part and "@carol@mastodon.test" in part
    assert "https://mastodon.test/@carol/1" in part
    assert "I wrote about this too." in part and "likes/1" not in part  # likes aren't conversations
    assert part.count("Open here") == 2  # the Lemmy and Bluesky posts; replies open where they are
    assert "Looked at your Lemmy server, Bluesky, the blog&#39;s replies, its webmentions" in part
    # Opened again soon after, nobody's asked again.
    again = client.get(f"/t/{tid}").text
    assert "Looking for more elsewhere" not in again and "Posted in tech" in again
    run_jobs(dbouncer)
    assert len(asked["lemmy"]) == 2


def test_the_reader_looks_too_and_says_what_went_wrong(server, dbouncer, looking, monkeypatch):
    client, asked = looking

    def signed_out(url, limit=25):
        raise RemoteAuthError("Bluesky only searches for someone signed in", "AuthMissing")

    def down(self, url, limit=20):
        raise RemoteUnavailable(f"{self.domain}: HTTP 502")

    monkeypatch.setattr(dbouncer.bluesky_adapter, "posts_linking", signed_out)
    monkeypatch.setattr(FakeAdapter, "posts_linking", down, raising=False)
    tid = post_linking(server, dbouncer, STORY, "1")
    dbouncer.articles.fetch_pending()
    reader = client.get(f"/t/{tid}/article").text
    assert "Looking for more elsewhere" in reader
    run_jobs(dbouncer)
    a = article_of(dbouncer, tid)
    part = client.get(f"/a/{a['id']}/discussions").text
    assert "About time they fixed that wall." in part
    assert f"Your Lemmy server: {DOMAIN}: HTTP 502" in part
    # Signed out of Bluesky, it isn't asked, and that's no error.
    assert "Looked at your Lemmy server, the blog&#39;s replies, its webmentions" in part
    assert "signed in" not in part
    # Signed in and still refused: that is.
    monkeypatch.setattr(dbouncer.bluesky_adapter, "reading_session", lambda: "session")
    with dbouncer.db.transaction() as conn:
        conn.execute("DELETE FROM discussion_checks")
    client.get(f"/a/{a['id']}")
    run_jobs(dbouncer)
    assert "Bluesky: Bluesky only searches for someone signed in" in client.get(f"/a/{a['id']}/discussions").text


def test_a_post_found_elsewhere_opens_here(server, dbouncer, looking):
    client, asked = looking
    tid = post_linking(server, dbouncer, STORY, "1")
    client.get(f"/t/{tid}")
    run_jobs(dbouncer)
    server.add_post("77", "Posted in tech", "")
    server.edit_post("77", url=STORY)
    with dbouncer.db.connect() as conn:
        did = conn.execute("SELECT id FROM discussions WHERE url=?", (f"https://{DOMAIN}/post/77",)).fetchone()[0]
    r = client.post(f"/discussions/{did}/open", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/jobs/")
    waiting = client.get(r.headers["location"], follow_redirects=False)
    assert waiting.status_code == 200 and 'http-equiv="refresh"' in waiting.text
    run_jobs(dbouncer)
    done = client.get(r.headers["location"], follow_redirects=False)
    assert done.status_code == 303
    new_tid = int(done.headers["location"].rsplit("/", 1)[1])
    with dbouncer.db.connect() as conn:
        assert conn.execute("SELECT retention FROM archived_threads WHERE id=?", (new_tid,)).fetchone()[0] == "auto"
    # Now it's here, it's listed with the posts here, once, and opening it again goes straight there.
    part = client.get(f"/t/{tid}").text
    assert part.count(f'href="/t/{new_tid}"') == 1 and "Open here" in part  # (the Bluesky one still)
    again = client.post(f"/discussions/{did}/open", follow_redirects=False)
    assert again.headers["location"] == f"/t/{new_tid}"
    # app.js, saving it as it's expanded, is told the thread (or the job saving it) instead.
    assert client.post(f"/discussions/{did}/open", headers={"X-ThreadBNC-Fetch": "1"}).json() == {"thread_id": new_tid}


def discussion_id(b, url: str) -> int:
    with b.db.connect() as conn:
        return conn.execute("SELECT id FROM discussions WHERE url=?", (url,)).fetchone()[0]


def test_each_one_expands_into_its_text_then_its_replies(server, dbouncer, looking, monkeypatch):
    client, asked = looking
    tid = post_linking(server, dbouncer, STORY, "1")
    client.get(f"/t/{tid}")
    run_jobs(dbouncer)
    part = client.get(f"/t/{tid}").text
    did = discussion_id(dbouncer, f"https://{DOMAIN}/post/77")
    assert f'data-peek="/discussions/{did}"' in part and '<details class="discussion"' in part
    # Expanded with JavaScript, one that can be saved here is, so its comments can be replied to (app.js).
    assert f'data-open="/discussions/{did}/open"' in part
    # A Lemmy post is read from your own server when it's expanded, with its comments, best first.
    server.add_post("77", "Posted in tech", "The council **finally** agreed.")
    server.add_comment("77", "c1", "First!")
    server.add_comment("77", "c2", "Worth the wait.")
    server.add_comment("77", "c3", "Agreed, it was.", parent="c2")
    server.edit_comment("77", "c2", score=5)
    read = []
    real = FakeAdapter.fetch_comments
    monkeypatch.setattr(FakeAdapter, "fetch_comments", lambda self, pid: read.append((self.domain, pid)) or
                        real(self, pid))
    panel = client.get(f"/discussions/{did}").text
    assert read == [(DOMAIN, "77")]
    assert "<strong>finally</strong>" in panel and "3 replies" in panel
    assert panel.index("finally") < panel.index("Worth the wait.") < panel.index("Agreed, it was.")         < panel.index("First!")
    client.get(f"/discussions/{did}")
    assert len(read) == 1  # expanded again soon after, it isn't read again
    # A reply to the blog's post has only its text, kept from when it was found.
    reply = client.get(f"/discussions/{discussion_id(dbouncer, 'https://mastodon.test/@carol/1')}").text
    assert "About time they fixed that wall." in reply and "where it was written" in reply
    # One that can't be read says so, and still shows what it said.
    monkeypatch.setattr(dbouncer.bluesky_adapter, "resolve_url",
                        lambda ref: (_ for _ in ()).throw(RemoteUnavailable("bsky.app: HTTP 502")))
    sky = client.get(f"/discussions/{discussion_id(dbouncer, 'https://bsky.app/profile/did:plc:abc/post/3k')}").text
    assert "Good news for the harbour" in sky and "bsky.app: HTTP 502" in sky


def test_a_post_here_expands_from_what_was_saved(server, dbouncer, looking):
    client, asked = looking
    t1 = post_linking(server, dbouncer, STORY, "1")
    server.add_post("2", "Same story", "Posted again.")
    server.edit_post("2", url="https://news.test/s/abc")  # a short link to it
    server.add_comment("2", "c9", "Saw this one already.")
    t2 = dbouncer.ingest_url(f"https://{DOMAIN}/post/2")
    dbouncer.articles.fetch_pending()
    part = client.get(f"/t/{t1}").text
    assert f'data-thread="{t2}"' in part
    # app.js opens it into the panels the feed does: its own page's text, then its comments.
    server.down = True  # nobody's asked
    panel = client.get(f"/t/{t2}?inline=1").text
    comments = panel[panel.index('<section class="comments"'):]
    assert panel.index("Posted again.") < panel.index('<section class="comments"')
    assert "Saw this one already." in comments and "1 comment" in comments


def test_feed_articles_are_discussed_too(server, dbouncer, looking, monkeypatch):
    """A feed item's article has no comments of its own: its Discussions are the conversation."""
    client, asked = looking
    tid = post_linking(server, dbouncer, STORY + "?utm_source=rss", "1")
    with dbouncer.db.transaction() as conn:
        conn.execute("UPDATE articles SET status='skipped', error='not an article'")
    client.get(f"/a/{article_of(dbouncer, tid)['id']}")
    run_jobs(dbouncer)
    part = client.get(f"/a/{article_of(dbouncer, tid)['id']}").text
    assert "Posted in tech" in part and "Couldn" in part


def test_turned_off(server, dbouncer, settings, looking):
    client, asked = looking
    dbouncer.job_handlers["discussions"].__self__.enabled = False
    tid = post_linking(server, dbouncer, STORY, "1")
    assert "Looking for more elsewhere" not in client.get(f"/t/{tid}").text
    run_jobs(dbouncer)
    assert asked["lemmy"] == [] and asked["bluesky"] == []


def test_found_discussions_go_with_their_article(server, dbouncer, looking):
    client, asked = looking
    with dbouncer.db.transaction() as conn:
        a = articles.ensure(conn, STORY, utcnow())
    client.get(f"/a/{a['id']}")
    run_jobs(dbouncer)
    with dbouncer.db.transaction() as conn:
        assert conn.execute("SELECT COUNT(*) FROM discussions").fetchone()[0] > 0
        assert articles.collect_orphans(conn) == 0  # opened just now
        conn.execute("UPDATE articles SET opened_at='2020-01-01T00:00:00.000000Z'")
        assert articles.collect_orphans(conn) == 1
        for table in ("discussions", "discussion_checks", "article_keys", "article_prints"):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, table


def test_addresses_asked_by(server, dbouncer):
    from threadbnc.discussions import Discussions
    a = {"canonical_url": STORY, "url": STORY + "?utm_source=rss", "fetched_from": STORY}
    same = [{"canonical_url": None, "url": "https://news.test/s/abc", "fetched_from": STORY},
            {"canonical_url": None, "url": "https://x.test/4", "fetched_from": None}]
    assert Discussions.addresses(a, same) == [STORY, STORY + "?utm_source=rss", "https://news.test/s/abc"]



def test_nowhere_to_ask_is_remembered_too(server, dbouncer, looking, monkeypatch):
    client, asked = looking
    finder = dbouncer.job_handlers["discussions"].__self__
    finder.lemmy_servers = lambda: []
    tid = post_linking(server, dbouncer, "https://other.test/2026/wall", "1")  # no replies or webmentions
    monkeypatch.setattr(dbouncer.bluesky_adapter, "posts_linking",
                        lambda url, limit=25: (_ for _ in ()).throw(RemoteAuthError("signed out")))
    assert "Looking for more elsewhere" in client.get(f"/t/{tid}").text
    run_jobs(dbouncer)
    page1 = client.get(f"/t/{tid}").text
    assert "Looking for more elsewhere" not in page1 and "Looked at" not in page1
