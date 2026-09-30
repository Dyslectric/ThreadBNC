"""Forums: PieFed feeds of feeds (or topics), added by their link (forums.py)."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from threadbnc import forums
from threadbnc.adapters import RemoteNotFound
from threadbnc.db import utcnow
from threadbnc.web import create_app

from .conftest import DOMAIN

HOST = "forums.test"
MATH = f"https://{DOMAIN}/c/math"


def community(cid, ap_id, title, icon=None, nsfw=False):
    return {"id": cid, "actor_id": ap_id, "name": ap_id.rsplit("/", 1)[-1], "title": title, "icon": icon,
            "nsfw": nsfw, "deleted": False, "removed": False}


def feed(name, title, children=(), communities=(), actor=None, **extra):
    return {"name": name, "title": title, "actor_id": actor or f"https://{HOST}/f/{name}", "children": list(children),
            "communities": list(communities), "description": "", **extra}


FEEDS = {"feeds": [
    feed("other", "Other", communities=[community(9, "https://elsewhere.test/c/x", "X")]),
    feed("bigtree", "Big Tree", actor=f"https://{HOST}/f/oldname", description="All of it, **sorted**.",
         icon="https://img.test/tree.png", children=[
             feed("animals", "Animals", description="Every [animal](https://a.test) community",
                  communities=[community(1, "https://zoo.test/c/zebras", "Zebras", icon="https://img.test/z.png")],
                  children=[feed("cats", "Cats", communities=[community(2, MATH, "Math cats"),
                                                              community(3, "https://zoo.test/c/lions", "Lions")])]),
             feed("aesthetics", "Aesthetics", communities=[community(4, "https://pretty.test/c/art", "Art", nsfw=True),
                                                           community(1, "https://zoo.test/c/zebras", "Zebras")]),
         ]),
]}


class FakeHttp:
    def __init__(self):
        self.asked = []

    def get_json(self, domain, path, params=None, token=None):
        self.asked.append((domain, path, dict(params or {})))
        if path == "/api/alpha/feed/list":
            return json.loads(json.dumps(FEEDS))
        if path == "/api/alpha/community":
            ap_id = {1: "https://zoo.test/c/zebras", 2: MATH, 3: "https://zoo.test/c/lions"}.get(params["id"])
            if ap_id is None:
                raise RemoteNotFound("gone")
            return {"community_view": {"community": {"actor_id": ap_id, "description": f"About {ap_id}.\n\nRules..."},
                                       "counts": {"total_subscriptions_count": 1200 + params["id"], "post_count": 50}}}
        raise RemoteNotFound(path)


@pytest.mark.parametrize("text, want", [
    ("https://piefed.example/f/Forumverse", forums.Link("piefed.example", "feed", "forumverse")),
    ("piefed.example/f/forumverse/", forums.Link("piefed.example", "feed", "forumverse")),
    ("~forumverse@piefed.example", forums.Link("piefed.example", "feed", "forumverse")),
    ("https://piefed.example/topic/technology/programming", forums.Link("piefed.example", "topic", "programming")),
    ("https://piefed.example/topics", forums.Link("piefed.example", "topic", None)),
])
def test_links(text, want):
    assert forums.parse_link(text) == want


@pytest.mark.parametrize("text", ["https://piefed.example/c/news", "hello", "https://piefed.example/"])
def test_links_that_arent_forums(text):
    with pytest.raises(forums.ForumError):
        forums.parse_link(text)


def test_tree_is_cut_from_the_list():
    tree = forums.tree_from(FEEDS, forums.Link(HOST, "feed", "bigtree"))
    assert tree["title"] == "Big Tree" and tree["url"] == f"https://{HOST}/f/oldname"
    assert [c["name"] for c in tree["children"]] == ["animals", "aesthetics"]
    assert tree["total"] == 4  # zebras counted once
    assert tree["children"][0]["total"] == 3
    # found by its old name in its address too, and inside another
    assert forums.tree_from(FEEDS, forums.Link(HOST, "feed", "oldname"))["title"] == "Big Tree"
    assert forums.tree_from(FEEDS, forums.Link(HOST, "feed", "cats"))["total"] == 2
    with pytest.raises(forums.ForumError):
        forums.tree_from(FEEDS, forums.Link(HOST, "feed", "nope"))


def test_all_topics():
    topics = {"topics": [{"id": 1, "name": "tech", "title": "Technology", "communities": [],
                          "children": [{"id": 2, "name": "linux", "title": "Linux", "children": [],
                                        "communities": [community(5, "https://l.test/c/linux", "Linux")]}]}]}
    tree = forums.tree_from(topics, forums.Link(HOST, "topic", None))
    assert tree["title"] == f"Topics on {HOST}" and tree["url"] == f"https://{HOST}/topics"
    assert tree["children"][0]["children"][0]["url"] == f"https://{HOST}/topic/tech/linux"
    linux = forums.tree_from(topics, forums.Link(HOST, "topic", "linux"))
    assert linux["url"] == f"https://{HOST}/topic/tech/linux" and linux["total"] == 1


def test_excerpt():
    assert forums.excerpt("Every [animal](https://a.test) **community**\n\nRules") == "Every animal community"
    assert forums.excerpt("word " * 100, 20).endswith("…")
    assert forums.excerpt(None) == ""


def client_for(settings, bouncer):
    bouncer.http = FakeHttp()
    client = TestClient(create_app(settings, bouncer))
    client.post("/login", data={"password": "pw"})
    return client


def test_add_browse_and_follow(settings, bouncer):
    client = client_for(settings, bouncer)
    assert "Add a forum" in client.get("/forums").text
    r = client.post("/forums", data={"link": f"https://{HOST}/f/bigtree"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/forums/")
    fid = int(r.headers["location"].rsplit("/", 1)[-1])
    page = client.get(f"/forums/{fid}").text
    assert "Big Tree" in page and "Animals" in page and "Cats" in page and "<strong>sorted</strong>" in page
    assert f'href="/forums/{fid}/animals"' in page and f'href="/forums/{fid}/animals/cats"' in page
    assert 'aria-current="page"' in page and '>Forums</span>' in page  # the Forums tab is the one lit
    # a part of it: its communities, asked about on its server
    page = client.get(f"/forums/{fid}/animals/cats").text
    assert "Math cats" in page and "Lions" in page and 'name="community" value="' + MATH + '"' in page
    assert "data-forum-wait" in page
    bouncer.run_one_job()
    asked = [p for p in bouncer.http.asked if p[1] == "/api/alpha/community"]
    assert sorted(p[2]["id"] for p in asked) == [2, 3]
    page = client.get(f"/forums/{fid}/animals/cats").text
    assert "1,202" in page and f"About {MATH}." in page and "Rules" not in page
    # following from the forum comes back to it
    r = client.post("/follow", data={"community": MATH, "backfill": "1", "stay": "1", "anchor": "fc-1"},
                    headers={"referer": f"http://testserver/forums/{fid}/animals/cats"}, follow_redirects=False)
    assert r.headers["location"] == f"/forums/{fid}/animals/cats#fc-1"
    page = client.get(f"/forums/{fid}/animals/cats").text
    assert "Following" in page
    # a community's page is a feed narrowed to it: the Feeds tab is lit, not Forums
    with bouncer.db.connect() as conn:
        cid = conn.execute("SELECT id FROM communities WHERE canonical_ap_id=?", (MATH,)).fetchone()["id"]
    nav = client.get(f"/c/{cid}").text.split('id="site-nav"', 1)[1].split("</nav>", 1)[0]
    assert 'href="/" class="on" aria-current="page"' in nav and 'href="/forums" class=""' in nav
    index = client.get("/forums").text
    assert "Big Tree" in index and "Math cats" not in index and "Big Tree › Animals › Cats" in index
    assert client.get(f"/forums/{fid}/nowhere").status_code == 404


def test_nsfw_pictures_arent_downloaded(settings, bouncer):
    client = client_for(settings, bouncer)
    fid = int(client.post("/forums", data={"link": f"https://{HOST}/f/bigtree"},
                          follow_redirects=False).headers["location"].rsplit("/", 1)[-1])
    assert "NSFW" in client.get(f"/forums/{fid}/aesthetics").text
    client.get(f"/forums/{fid}/animals")
    client.get(f"/forums/{fid}")
    with bouncer.db.connect() as conn:
        urls = {r["url"] for r in conn.execute("SELECT m.url FROM forum_media f JOIN media m ON m.id=f.media_id")}
    assert "https://img.test/z.png" in urls and "https://img.test/tree.png" in urls
    assert not any("pretty.test" in u for u in urls)


def test_refresh_and_remove(settings, bouncer):
    client = client_for(settings, bouncer)
    fid = int(client.post("/forums", data={"link": f"~bigtree@{HOST}"},
                          follow_redirects=False).headers["location"].rsplit("/", 1)[-1])
    client.get(f"/forums/{fid}")
    FEEDS["feeds"][1]["title"] = "Bigger Tree"
    try:
        client.post(f"/forums/{fid}/refresh")
        assert "Bigger Tree" in client.get(f"/forums/{fid}").text
    finally:
        FEEDS["feeds"][1]["title"] = "Big Tree"
    # adding it again is the same forum
    again = client.post("/forums", data={"link": f"https://{HOST}/f/bigtree"}, follow_redirects=False)
    assert again.headers["location"] == f"/forums/{fid}"
    client.post(f"/forums/{fid}/remove")
    assert client.get(f"/forums/{fid}").status_code == 404
    with bouncer.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM forum_media").fetchone()[0] == 0


def test_bad_link(settings, bouncer):
    client = client_for(settings, bouncer)
    r = client.post("/forums", data={"link": f"https://{HOST}/f/nope"})
    assert "no public feed called nope" in r.text


def test_stale_details_are_asked_again(settings, bouncer):
    now = utcnow()
    assert forums.stale(None, now)
    assert not forums.stale({"checked_at": now}, now)
    assert forums.stale({"checked_at": "2020-01-01T00:00:00.000000Z"}, now)


def test_default_forums_are_added_once(settings, bouncer):
    import dataclasses
    settings = dataclasses.replace(settings, default_forums=(f"https://{HOST}/f/bigtree",))
    client = client_for(settings, bouncer)
    page = client.get("/forums").text
    assert "being read from its server" in page and "data-forum-wait" in page
    client.get("/forums")  # not asked for twice
    with bouncer.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM jobs WHERE kind=?", (forums.ADD_JOB,)).fetchone()[0] == 1
    bouncer.run_one_job()
    page = client.get("/forums").text
    assert "Big Tree" in page and "data-forum-wait" not in page
    fid = int(page.split('href="/forums/', 1)[1].split('"', 1)[0])
    client.post(f"/forums/{fid}/remove")
    page = client.get("/forums").text  # removed stays removed
    assert "Big Tree" not in page and "being read" not in page


def test_default_forums_setting():
    from threadbnc.config import DEFAULT_FORUMS, _forums
    assert _forums(None) == DEFAULT_FORUMS == ("https://piefed.social/f/forumverse",)
    assert _forums("off") == _forums("") == ()
    assert _forums("https://a.test/f/x, ~y@b.test") == ("https://a.test/f/x", "~y@b.test")


def test_default_forum_already_added_isnt_added_again(settings, bouncer):
    import dataclasses
    settings = dataclasses.replace(settings, default_forums=(f"https://{HOST}/f/bigtree",))
    client = client_for(settings, bouncer)
    client.post("/forums", data={"link": f"~bigtree@{HOST}"})
    page = client.get("/forums").text
    assert "Big Tree" in page and "being read from its server" not in page
    with bouncer.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM jobs WHERE kind=?", (forums.ADD_JOB,)).fetchone()[0] == 0


def test_forum_menus(settings, bouncer):
    client = client_for(settings, bouncer)
    fid = int(client.post("/forums", data={"link": f"https://{HOST}/f/bigtree"},
                          follow_redirects=False).headers["location"].rsplit("/", 1)[-1])
    index = client.get("/forums").text
    assert f'action="/forums/{fid}/remove"' in index and f'action="/forums/{fid}/refresh"' in index
    page = client.get(f"/forums/{fid}").text
    assert f'action="/forums/{fid}/remove"' in page
    assert 'action="/forums/%d/remove"' % fid not in client.get(f"/forums/{fid}/animals").text  # only at its top
    r = client.post(f"/forums/{fid}/remove", headers={"referer": "http://testserver/forums"}, follow_redirects=False)
    assert r.headers["location"] == "/forums"


def test_forum_opened_in_place(settings, bouncer):
    client = client_for(settings, bouncer)
    fid = int(client.post("/forums", data={"link": f"https://{HOST}/f/bigtree"},
                          follow_redirects=False).headers["location"].rsplit("/", 1)[-1])
    page = client.get(f"/forums/{fid}").text
    # each section opens and closes, and each forum in one opens right there
    assert page.count('<details class="forum-topic') == 2  # Animals, and More forums
    assert f'data-part="/forums/{fid}/animals/cats?part=1&amp;depth=1"' in page
    assert 'class="forum-rail' in page and 'data-forum-fold="close"' in page
    part = client.get(f"/forums/{fid}/animals/cats?part=1&depth=1").text
    assert "<html" not in part and 'class="forum-body"' in part and "Math cats" in part
    assert f'data-forum-src="/forums/{fid}/animals/cats?part=1&amp;depth=1"' in part
    assert "data-forum-follow" in part and 'id="fc-' in part
    # a part with forums in it has its sections, coloured by how far down it is
    assert 'class="forum-topic d1"' in client.get(f"/forums/{fid}/animals?part=1&depth=1").text
    # each forum on the index opens in place too; a forum's own communities open by themselves (part=2)
    assert f'data-part="/forums/{fid}?part=1&amp;depth=0"' in client.get("/forums").text
    own = client.get(f"/forums/{fid}/animals?part=2&depth=1").text
    assert 'class="forum-topic' not in own and 'data-forum-src="/forums/%d/animals?part=2&amp;depth=1"' % fid in own
