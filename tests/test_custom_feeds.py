from __future__ import annotations

import re
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from threadbnc import feed
from threadbnc.db import utcnow
from threadbnc.web import create_app

from .conftest import COMMUNITY, DOMAIN

PHYSICS = replace(COMMUNITY, ap_id=f"https://{DOMAIN}/c/physics", name="physics", title="Physics")


def one(b, sql, *args):
    with b.db.connect() as conn:
        return conn.execute(sql, args).fetchone()


@pytest.fixture
def client(settings, bouncer, server):
    """Posts 1-2 in !math, 3-4 in !physics; post 1 already read."""
    for k in range(1, 5):
        p = server.add_post(str(k), f"post {k}", "text", created=utcnow())
        if k > 2:
            server.posts[str(k)] = replace(p, community=PHYSICS)
    bouncer.poll_follow(bouncer.follow_community(f"!math@{DOMAIN}", 15, 30, True))
    server.communities["physics"] = PHYSICS
    bouncer.follow_community(f"!physics@{DOMAIN}", 15, 30, False)
    with bouncer.db.transaction() as conn:
        conn.execute("UPDATE archived_threads SET last_viewed_at=? WHERE id=1", (utcnow(),))
    c = TestClient(create_app(settings, bouncer))
    c.post("/login", data={"password": "pw"})
    return c


def titles(page: str) -> list[str]:
    return re.findall(r'class="pc-title"><a href="/t/\d+">([^<]+)</a>', page)


def community_id(bouncer, name: str) -> int:
    return one(bouncer, "SELECT id FROM communities WHERE name=?", name)[0]


def test_the_main_feed_follows_its_defaults(client, bouncer):
    assert titles(client.get("/").text) == ["post 4", "post 3", "post 2", "post 1"]
    r = client.post("/feed/defaults", data={"sort": "new", "t": "week", "unread": "posts"}, follow_redirects=False)
    assert r.headers["location"] == "/"
    page = client.get("/").text
    assert titles(page) == ["post 4", "post 3", "post 2"]  # unread only, now by default
    assert "New · Past week · Unread posts" in page
    # The address still wins, including "all posts", which links now always spell out.
    assert titles(client.get("/?sort=new&t=all&unread=").text) == ["post 4", "post 3", "post 2", "post 1"]
    assert 'href="/?sort=top&t=week&unread=posts"' in page
    # Community pages use the main feed's defaults too.
    assert "post 1" not in client.get(f"/c/{community_id(bouncer, 'math')}").text


def test_make_this_the_default_is_offered_only_when_it_differs(client):
    assert "Make this the default" not in client.get("/").text
    assert "Make this the default" in client.get("/?sort=top&t=all&unread=").text


def test_bad_defaults_fall_back(bouncer):
    with bouncer.db.transaction() as conn:
        conn.execute("INSERT INTO app_settings(key, value) VALUES ('feed_defaults', ?)",
                     ('{"sort": "nonsense", "window": "decade", "unread": "maybe"}',))
    with bouncer.db.connect() as conn:
        assert feed.load_defaults(conn) == feed.FeedSettings("new", "all", "")


def test_custom_feed_shows_its_communities_its_own_way(client, bouncer):
    physics = community_id(bouncer, "physics")
    r = client.post("/feeds", data={"name": "  Science   stuff ", "community": [physics], "sort": "new", "t": "all",
                                    "unread": "", "view": "list"}, follow_redirects=False)
    fid = one(bouncer, "SELECT id FROM custom_feeds")[0]
    assert r.headers["location"] == f"/f/{fid}"
    page = client.get(f"/f/{fid}").text
    assert titles(page) == ["post 4", "post 3"] and "Science stuff" in page
    # In the sidebar, with its unread count, on every feed page.
    sidebar = client.get("/").text
    assert f'href="/f/{fid}"' in sidebar and re.search(r"Science stuff</span>\s*<span class=\"count\"[^>]*>2<", sidebar)
    # Its own default doesn't touch the main feed's.
    client.post("/feed/defaults", data={"sort": "top", "t": "all", "unread": "posts", "feed_id": fid})
    assert "Top · All time · Unread posts" in client.get(f"/f/{fid}").text
    assert "New · All time · All posts" in client.get("/").text
    # Mark all read covers only this feed's communities.
    client.post("/feed/mark-read", data={"feed_id": fid})
    assert titles(client.get(f"/f/{fid}").text) == []
    assert titles(client.get("/?sort=new&t=all&unread=posts").text) == ["post 2"]


def test_custom_feed_can_be_edited_and_deleted(client, bouncer):
    math, physics = community_id(bouncer, "math"), community_id(bouncer, "physics")
    client.post("/feeds", data={"name": "Mine", "community": [physics]})
    fid = one(bouncer, "SELECT id FROM custom_feeds")[0]
    form = client.get(f"/f/{fid}/edit").text
    assert f'value="{physics}" checked>' in form and f'value="{math}" >' in form
    client.post(f"/f/{fid}/edit", data={"name": "Both", "community": [math, physics], "view": "tiles"})
    custom = one(bouncer, "SELECT name, view_mode FROM custom_feeds WHERE id=?", fid)
    assert tuple(custom) == ("Both", "tiles")
    assert len(titles(client.get(f"/f/{fid}?view=list").text)) == 4
    assert one(bouncer, "SELECT view_mode FROM custom_feeds WHERE id=?", fid)[0] == "list"  # remembered
    client.post(f"/f/{fid}/delete")
    assert client.get(f"/f/{fid}").status_code == 404
    assert one(bouncer, "SELECT COUNT(*) FROM custom_feed_communities")[0] == 0
    assert one(bouncer, "SELECT COUNT(*) FROM archived_threads")[0] == 4  # posts untouched


def test_a_feed_needs_a_name_and_says_when_it_has_no_communities(client, bouncer):
    page = client.post("/feeds", data={"name": "   "}).text
    assert "Give the feed a name." in page
    assert one(bouncer, "SELECT COUNT(*) FROM custom_feeds")[0] == 0
    client.post("/feeds", data={"name": "Empty"})
    fid = one(bouncer, "SELECT id FROM custom_feeds")[0]
    assert "This feed has no communities yet" in client.get(f"/f/{fid}").text


def test_following_panel_collapses_and_stays_collapsed(client):
    assert 'data-remember="/feed/sidebar" open' in client.get("/").text
    client.post("/feed/sidebar", data={"collapsed": "1"})
    page = client.get("/").text
    assert 'data-remember="/feed/sidebar">' in page and 'data-remember="/feed/sidebar" open' not in page
    client.post("/feed/sidebar", data={"collapsed": "0"})
    assert 'data-remember="/feed/sidebar" open' in client.get("/").text
