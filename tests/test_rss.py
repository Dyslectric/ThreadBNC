"""RSS and Atom feeds: following them like communities, and posting articles to your own."""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from threadbnc.accounts import AccountError, Poster
from threadbnc.adapters import parse_community_ref
from threadbnc.adapters import rss
from threadbnc.adapters.http import HostThrottle
from threadbnc.adapters.rss import FeedFetcher, RssAdapter, html_to_markdown, parse_feed
from threadbnc.vault import TokenVault
from threadbnc.web import create_app

from .conftest import DOMAIN

HOME = "home.test"
FEED = "https://blog.example/feed.xml"


def rss_item(n, title, body, date="Mon, 21 Sep 2026 10:00:00 GMT", link=None):
    return f"""<item><title>{title}</title><link>{link or f'https://blog.example/posts/{n}'}</link>
      <guid isPermaLink="false">post-{n}</guid><pubDate>{date}</pubDate>
      <dc:creator>Jane Doe</dc:creator>
      <content:encoded><![CDATA[{body}]]></content:encoded>
      <media:thumbnail url="https://blog.example/img/{n}.jpg"/></item>"""


def rss_doc(items):
    return f"""<?xml version="1.0"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/"
     xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:media="http://search.yahoo.com/mrss/">
  <channel><title>Jane's Blog</title><link>https://blog.example/</link><description>Notes</description>
  {''.join(items)}</channel></rss>"""


class FakeWeb:
    def __init__(self) -> None:
        self.items = {1: rss_item(1, "First post", "<p>Hello <b>world</b>.</p><p>More text here.</p>"),
                      2: rss_item(2, "Second post", "<p>A photo:</p><img src='/img/cat.jpg' alt='cat'>",
                                  date="Tue, 22 Sep 2026 08:00:00 GMT")}
        self.pages = {
            "/": ("text/html", '<html><head><link rel="alternate" type="application/rss+xml" '
                               'href="/feed.xml"></head><body>Jane</body></html>'),
            "/plain": ("text/html", "<html><body>no feed here</body></html>"),
            "/atom.xml": ("application/atom+xml", """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title type="html">Atom &amp;amp; Co</title>
  <link rel="alternate" href="https://atom.example/"/>
  <entry><title>Atomic</title><id>tag:atom.example,2026:1</id><link href="https://atom.example/1"/>
    <published>2026-09-20T12:00:00Z</published><updated>2026-09-21T12:00:00Z</updated>
    <author><name>Ann</name></author>
    <content type="xhtml"><div xmlns="http://www.w3.org/1999/xhtml"><p>Split <em>atoms</em>.</p></div></content>
  </entry></feed>"""),
        }
        self.requests: list[tuple[str, dict]] = []

    def feed_body(self) -> str:
        return rss_doc(self.items[k] for k in sorted(self.items, reverse=True))

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append((request.url.path, dict(request.headers)))
        path = request.url.path
        if path == "/feed.xml":
            body = self.feed_body()
            etag = f'"{hash(body)}"'
            if request.headers.get("if-none-match") == etag:
                return httpx.Response(304)
            return httpx.Response(200, text=body, headers={"content-type": "application/rss+xml", "etag": etag})
        if path in self.pages:
            ctype, body = self.pages[path]
            return httpx.Response(200, text=body, headers={"content-type": ctype})
        return httpx.Response(404)


@pytest.fixture
def web(bouncer, monkeypatch):
    fake = FakeWeb()
    bouncer.rss_adapter = RssAdapter(FeedFetcher("test", throttle=HostThrottle(0), check_host=False,
                                                 transport=httpx.MockTransport(fake.handle)))
    monkeypatch.setattr(rss, "FEED_CACHE", 0)  # every check fetches (conditionally)
    return fake


def one(b, sql, *args):
    with b.db.connect() as conn:
        return conn.execute(sql, args).fetchone()


def col(b, sql, *args):
    with b.db.connect() as conn:
        return [r[0] for r in conn.execute(sql, args).fetchall()]


def post_row(b, key):
    return one(b, "SELECT o.*, r.title, r.body, r.url, t.id AS tid, t.active FROM objects o "
                  "JOIN revisions r ON r.object_id=o.id AND r.seq=o.revision_count "
                  "JOIN archived_threads t ON t.root_object_id=o.id WHERE o.canonical_ap_id=?", f"rss:{key}")


def logged_in(settings, bouncer):
    client = TestClient(create_app(settings, bouncer))
    client.post("/login", data={"password": "pw"})
    return client


# -- parsing ---------------------------------------------------------------------------------

def test_feed_refs():
    for text in (FEED, f"rss:{FEED}", "https://medium.com/@someone/feed"):
        ref = parse_community_ref(text)
        assert ref.domain == "rss" and ref.name.startswith("https://"), text
    assert parse_community_ref("https://lemmy.world/c/tech").domain == "lemmy.world"


def test_html_becomes_markdown():
    md = html_to_markdown('<p>Hi <strong>there</strong>, see <a href="/x">this</a>.</p>'
                          '<script>alert(1)</script><ul><li>one</li><li>two_2</li></ul>'
                          '<img src="pic.png" alt="a pic"><pre>code  here</pre>', "https://site.example/a/")
    assert "Hi **there**, see [this](https://site.example/x)." in md
    assert "alert" not in md and "- one" in md and "- two\\_2" in md
    assert "![a pic](https://site.example/a/pic.png)" in md and "```\ncode  here\n```" in md


def test_atom_and_rdf_feeds_parse(web):
    atom = parse_feed(web.pages["/atom.xml"][1].encode(), "https://atom.example/atom.xml")
    assert atom.title == "Atom & Co"
    e = atom.entries[0]
    assert (e.key, e.link, e.author, e.published) == ("tag:atom.example,2026:1", "https://atom.example/1", "Ann",
                                                      "2026-09-20T12:00:00.000000Z")
    assert "Split" in html_to_markdown(e.html) and "*atoms*" in html_to_markdown(e.html)
    rdf = parse_feed(b"""<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
        xmlns="http://purl.org/rss/1.0/"><channel><title>Old</title><link>https://old.example/</link></channel>
        <item><title>Classic</title><link>https://old.example/1</link><description>Hi</description></item>
        </rdf:RDF>""", "https://old.example/rss")
    assert rdf.title == "Old" and rdf.entries[0].link == "https://old.example/1"
    assert parse_feed(b"<html><body>nope</body></html>", "x") is None


# -- following -------------------------------------------------------------------------------

def test_follow_a_feed(settings, bouncer, web):
    cid = bouncer.follow_community(FEED, None, 30, backfill=True)
    f = one(bouncer, "SELECT * FROM community_follows WHERE community_id=?", cid)
    assert (f["source_domain"], f["source_ref"], f["poll_interval_minutes"]) == ("rss", FEED, 60)
    assert one(bouncer, "SELECT name, canonical_ap_id FROM communities WHERE id=?", cid)[:] == \
        ("Jane's Blog", f"rss:{FEED}")
    assert bouncer.poll_follow(cid) == 2
    first = post_row(bouncer, "post-1")
    assert first["title"] == "First post" and first["url"] == "https://blog.example/posts/1"
    assert first["body"] == "Hello **world**.\n\nMore text here."
    assert first["created_at"] == "2026-09-21T10:00:00.000000Z"
    assert one(bouncer, "SELECT username FROM actors WHERE id=?", first["author_id"])[0] == "Jane Doe"
    assert "![cat](https://blog.example/img/cat.jpg)" in post_row(bouncer, "post-2")["body"]
    page = logged_in(settings, bouncer).get("/").text
    assert "Jane&#39;s Blog" in page and "· feed" in page and "↗ Post" in page and "Jane Doe@" not in page
    assert "Live on server" not in logged_in(settings, bouncer).get(f"/c/{cid}").text


def test_a_website_address_finds_its_feed(bouncer, web):
    cid = bouncer.follow_community("https://blog.example/", None, 30)
    assert one(bouncer, "SELECT source_ref FROM community_follows WHERE community_id=?", cid)[0] == FEED
    with pytest.raises(Exception, match="doesn't link one"):
        bouncer.follow_community("https://blog.example/plain")


def test_checks_are_conditional(bouncer, web):
    cid = bouncer.follow_community(FEED, None, 30, backfill=True)
    bouncer.poll_follow(cid)
    web.requests.clear()
    assert bouncer.poll_follow(cid) == 0
    (path, headers), = web.requests
    assert path == "/feed.xml" and "if-none-match" in headers  # answered 304: nothing re-downloaded


def test_edited_articles_keep_their_history(bouncer, web):
    cid = bouncer.follow_community(FEED, None, 30, backfill=True)
    bouncer.poll_follow(cid)
    web.items[1] = rss_item(1, "First post (updated)", "<p>Hello <b>everyone</b>.</p>")
    bouncer.sync_thread(post_row(bouncer, "post-1")["tid"])
    row = post_row(bouncer, "post-1")
    assert row["title"] == "First post (updated)" and row["revision_count"] == 2


def test_articles_that_leave_the_feed_are_kept_not_missing(bouncer, web):
    cid = bouncer.follow_community(FEED, None, 30, backfill=True)
    bouncer.poll_follow(cid)
    tid = post_row(bouncer, "post-1")["tid"]
    del web.items[1]  # pushed out by newer entries
    bouncer.sync_thread(tid)
    row = post_row(bouncer, "post-1")
    assert row["active"] == 0 and row["cur_missing"] == 0
    assert col(bouncer, "SELECT event_type FROM state_events WHERE thread_id=?", tid)[-1] == "aged_out"
    assert col(bouncer, "SELECT event_type FROM state_events WHERE event_type='missing'") == []


def test_feeds_are_checked_no_faster_than_every_5_minutes(bouncer, web):
    cid = bouncer.follow_community(FEED, 1, 30)
    assert one(bouncer, "SELECT poll_interval_minutes FROM community_follows WHERE community_id=?", cid)[0] == 5


# -- posting articles ------------------------------------------------------------------------

@pytest.fixture
def poster(bouncer, settings):
    return Poster(bouncer, TokenVault(settings.credentials_key, settings.data_dir))


def test_post_an_article_to_your_community(settings, bouncer, web, server, poster):
    feed = bouncer.follow_community(FEED, None, 30, backfill=True)
    bouncer.poll_follow(feed)
    mine = bouncer.follow_community(f"!math@{DOMAIN}")
    poster.add(HOME, "dave", "hunter2")
    src = post_row(bouncer, "post-1")["tid"]
    client = logged_in(settings, bouncer)
    form = client.get(f"/t/{src}/repost").text
    assert "Post this article" in form and "Jane&#39;s Blog (feed)" not in form.split("<select")[1]
    draft = poster.repost_draft(src)
    assert draft["url"] == "https://blog.example/posts/1"
    assert draft["body"] == "> Hello **world**.\n>\n> More text here." and "cross-posted" not in draft["body"]
    fields = {k: draft[k] for k in ("title", "url", "body")}
    r = client.post(f"/t/{src}/repost", data={"community_id": str(mine), **fields}, follow_redirects=False)
    new = int(r.headers["location"].split("/")[2])
    assert new != src
    page = client.get(f"/t/{src}").text  # the same link: shown together
    assert "Posted 2 times" in page and "You reposted this to !math@lemmy.test" in page


def test_feed_articles_take_no_comments_or_votes(settings, bouncer, web, poster):
    cid = bouncer.follow_community(FEED, None, 30, backfill=True)
    bouncer.poll_follow(cid)
    account = poster.add(HOME, "dave", "hunter2")
    row = post_row(bouncer, "post-1")
    with pytest.raises(AccountError, match="Feed articles"):
        poster.vote(account, row["id"], 1)
    with pytest.raises(AccountError, match="Feed articles"):
        poster.reply(account, row["tid"], "hi")
    with pytest.raises(AccountError, match="Feed articles"):
        poster.submit(account, cid, "a post")
    page = logged_in(settings, bouncer).get(f"/t/{row['tid']}").text
    assert "article from a feed" in page and "Add a comment" not in page and "Upvote" not in page
    assert 'href="https://blog.example/posts/1" rel="noreferrer noopener nofollow" target="_blank">Original' in page
