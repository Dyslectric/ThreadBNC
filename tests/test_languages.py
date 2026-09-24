from __future__ import annotations

from fastapi.testclient import TestClient

from threadbnc import feed, languages
from threadbnc.adapters import lemmy
from threadbnc.adapters.activitypub import language_of
from threadbnc.adapters.rss import parse_feed
from threadbnc.web import create_app

from .conftest import DOMAIN
from .test_lemmy_v4 import adapter, fixture


def test_language_codes_are_normalized():
    assert languages.normalize("en-GB") == "en"
    assert languages.normalize("EN") == "en"
    assert languages.normalize("nb") == "no"
    assert languages.normalize("und") is None
    assert languages.normalize("") is None
    assert languages.normalize(None) is None
    assert languages.parse("English, de fr-CA") == (["en", "de", "fr"], [])
    assert languages.parse("en klingon") == (["en"], ["klingon"])
    assert languages.parse("  ") == ([], [])


def test_servers_say_what_language_a_post_is_in():
    assert language_of({"contentMap": {"en": "<p>hi</p>"}}) == "en"
    assert language_of({"language": {"identifier": "de", "name": "Deutsch"}}) == "de"
    assert language_of({"contentMap": {"en": "hi", "de": "hallo"}}) is None  # several: can't tell which
    assert language_of({"content": "hi"}) is None
    rss = parse_feed(b"<rss><channel><title>t</title><language>en-us</language>"
                     b"<item><title>a</title></item></channel></rss>", "https://x.example/feed")
    assert rss.language == "en"
    atom = parse_feed(b'<feed xmlns="http://www.w3.org/2005/Atom" xml:lang="fr"><title>t</title></feed>',
                      "https://x.example/atom")
    assert atom.language == "fr"


def test_lemmy_language_ids_are_read_from_the_server_once(monkeypatch):
    monkeypatch.setattr(lemmy, "_LANGUAGES", {})
    post = fixture("post")
    post["post_view"]["post"]["language_id"] = 37
    a, http = adapter({("GET", "/post"): post, ("GET", "/site"): fixture("site")})
    assert a.fetch_post("1").language == "en"
    assert a.fetch_post("1").language == "en"
    assert len(http.called("GET", "/site")) == 1
    post["post_view"]["post"]["language_id"] = 0  # undetermined
    assert a.fetch_post("1").language is None


def test_lemmy_server_that_cant_say_leaves_the_language_unknown(monkeypatch):
    monkeypatch.setattr(lemmy, "_LANGUAGES", {})
    post = fixture("post")
    post["post_view"]["post"]["language_id"] = 37
    a, http = adapter({("GET", "/post"): post})
    assert a.fetch_post("1").language is None
    a.fetch_post("1")
    assert len(http.called("GET", "/site")) == 1  # not asked again for every post


def _posts_in_languages(server, bouncer):
    for local_id, lang in (("1", "en"), ("2", "de"), ("3", None)):
        server.add_post(local_id, f"post {local_id}", "text")
        server.edit_post(local_id, language=lang)
    cid = bouncer.follow_community(f"!math@{DOMAIN}", 10, 7, backfill=True)
    bouncer.poll_follow(cid)
    return cid


def thread_of(bouncer, local_id):
    with bouncer.db.connect() as conn:
        return conn.execute("SELECT t.id FROM archived_threads t JOIN objects o ON o.id=t.root_object_id "
                            "WHERE o.canonical_ap_id=?", (f"https://{DOMAIN}/post/{local_id}",)).fetchone()[0]


def titles(page):
    return sorted(i["title"] for i in page.items)


def test_feeds_show_only_the_languages_chosen(server, bouncer):
    cid = _posts_in_languages(server, bouncer)
    with bouncer.db.connect() as conn:
        assert titles(feed.load_feed(conn)) == ["post 1", "post 2", "post 3"]  # none chosen: all of them
    with bouncer.db.transaction() as conn:
        languages.save(conn, ["en"])
    with bouncer.db.connect() as conn:
        assert titles(feed.load_feed(conn)) == ["post 1", "post 3"]  # a post that doesn't say stays
        assert titles(feed.load_feed(conn, community_id=cid)) == ["post 1", "post 3"]
        assert titles(feed.load_feed(conn, community_ids=[cid])) == ["post 1", "post 3"]
        follow = feed.followed_communities(conn)[0]
        assert (follow["unread"], follow["total"]) == (2, 2)
    tid = thread_of(bouncer, "2")
    bouncer.promote(tid)
    with bouncer.db.connect() as conn:
        assert "post 2" in titles(feed.load_feed(conn, kept_only=True))  # kept: always shown
        assert titles(feed.load_feed(conn, thread_ids=[tid])) == ["post 2"]


def test_a_later_read_that_doesnt_say_keeps_the_language(server, bouncer):
    _posts_in_languages(server, bouncer)
    server.edit_post("2", language=None, body="edited")
    tid = thread_of(bouncer, "2")
    bouncer.sync_thread(tid)
    with bouncer.db.connect() as conn:
        assert conn.execute("SELECT language FROM objects WHERE id=(SELECT root_object_id FROM archived_threads "
                            "WHERE id=?)", (tid,)).fetchone()[0] == "de"


def test_choosing_languages_in_the_feed_menu(settings, server, bouncer):
    _posts_in_languages(server, bouncer)
    client = TestClient(create_app(settings, bouncer))
    client.post("/login", data={"password": "pw"})
    html = client.get("/").text
    assert 'action="/feed/languages"' in html and "Posts in every language." in html and "post 2" in html
    client.post("/feed/languages", data={"languages": "English"})
    html = client.get("/").text
    assert "Only posts in English, and ones that don&#39;t say." in html or \
        "Only posts in English, and ones that don't say." in html
    assert "post 1" in html and "post 2" not in html
    client.post("/feed/languages", data={"languages": "english, klingon"})
    with bouncer.db.connect() as conn:
        assert languages.load(conn) == ["en"]  # not understood: nothing changed
    client.post("/feed/languages", data={"languages": ""})
    assert "post 2" in client.get("/").text
