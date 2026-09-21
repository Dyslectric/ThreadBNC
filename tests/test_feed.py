from __future__ import annotations

from fastapi.testclient import TestClient

from threadbnc import feed
from threadbnc.db import utcnow
from threadbnc.web import create_app

from .conftest import DOMAIN


def one(b, sql, *args):
    with b.db.connect() as conn:
        return conn.execute(sql, args).fetchone()


def followed_with_posts(server, bouncer, n=3):
    for k in range(n):
        server.add_post(str(k + 1), f"post {k + 1}", "some **text**", created=utcnow())
    cid = bouncer.follow_community(f"!math@{DOMAIN}", 10, 7, backfill=True)
    bouncer.poll_follow(cid)
    return cid


def test_feed_lists_followed_posts_and_tracks_unread(server, bouncer):
    followed_with_posts(server, bouncer)
    with bouncer.db.connect() as conn:
        page = feed.load_feed(conn)
    assert [i["title"] for i in page.items] == ["post 3", "post 2", "post 1"]
    assert all(i["unread"] for i in page.items)
    assert page.items[0]["excerpt"] == "some text"
    with bouncer.db.transaction() as conn:
        feed.mark_read(conn, utcnow())
    with bouncer.db.connect() as conn:
        assert feed.load_feed(conn, unread=True).items == []
    # a new comment makes the thread unread again, with a count
    server.add_comment("2", "20", "fresh")
    tid = one(bouncer, "SELECT t.id FROM archived_threads t JOIN objects o ON o.id=t.root_object_id "
                       "WHERE o.canonical_ap_id=?", f"https://{DOMAIN}/post/2")[0]
    bouncer.sync_thread(tid)
    with bouncer.db.connect() as conn:
        items = feed.load_feed(conn, unread=True).items
    assert [(i["title"], i["n_new"]) for i in items] == [("post 2", 1)]


def test_unfollowed_community_leaves_feed(server, bouncer):
    cid = followed_with_posts(server, bouncer)
    bouncer.unfollow(cid)
    with bouncer.db.connect() as conn:
        assert feed.load_feed(conn).items == []
        assert len(feed.load_feed(conn, community_id=cid).items) == 3


def test_unkeep_returns_feed_post_to_auto(server, bouncer):
    followed_with_posts(server, bouncer, 1)
    tid = one(bouncer, "SELECT id FROM archived_threads")[0]
    bouncer.promote(tid)
    assert bouncer.unkeep(tid) == "auto"
    t = one(bouncer, "SELECT * FROM archived_threads WHERE id=?", tid)
    assert t["retention"] == "auto" and t["expires_at"] and t["trashed_at"] is None


def test_unkeep_manual_archive_goes_to_trash(server, bouncer):
    server.add_post("9", "manual", "x")
    tid = bouncer.ingest_url(f"https://{DOMAIN}/post/9")
    assert bouncer.unkeep(tid) == "trash"
    assert one(bouncer, "SELECT trashed_at FROM archived_threads WHERE id=?", tid)[0]


def test_feed_page_ui(settings, server, bouncer):
    followed_with_posts(server, bouncer)
    client = TestClient(create_app(settings, bouncer))
    client.post("/login", data={"password": "pw"})
    html = client.get("/").text
    assert "Your feed" in html and "post 3" in html and "☆ Keep" in html
    tid = one(bouncer, "SELECT id FROM archived_threads ORDER BY id DESC")[0]
    client.post(f"/t/{tid}/keep", headers={"referer": "http://testserver/"})
    assert "★ Kept" in client.get("/").text
    assert "post" in client.get("/kept").text
    client.post("/feed/mark-read")
    assert "all caught up" in client.get("/?unread=1").text


def test_empty_feed_prompts_follow(settings, bouncer):
    client = TestClient(create_app(settings, bouncer))
    client.post("/login", data={"password": "pw"})
    assert "Follow a community to start your feed" in client.get("/").text


def test_vote_counts_shown(settings, server, bouncer):
    followed_with_posts(server, bouncer, 1)
    server.add_comment("1", "10", "a reply")
    server.edit_post("1", upvotes=12, downvotes=3, score=9)
    server.edit_comment("1", "10", upvotes=5, downvotes=1)
    tid = one(bouncer, "SELECT id FROM archived_threads")[0]
    bouncer.sync_thread(tid)
    client = TestClient(create_app(settings, bouncer))
    client.post("/login", data={"password": "pw"})
    assert "▲ 12" in client.get("/").text and "▼ 3" in client.get("/").text
    page = client.get(f"/t/{tid}").text
    assert "▲ 12" in page and "▲ 5" in page and "▼ 1" in page
    # vote changes are current state, not edits
    assert one(bouncer, "SELECT revision_count FROM objects WHERE object_type='post'")[0] == 1
