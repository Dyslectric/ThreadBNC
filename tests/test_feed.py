from __future__ import annotations

import re

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from threadbnc import feed
from threadbnc.adapters.base import RemoteError
from threadbnc.db import fmt_ts, utcnow
from threadbnc.web import create_app

from .conftest import DOMAIN


def one(b, sql, *args):
    with b.db.connect() as conn:
        return conn.execute(sql, args).fetchone()


def from_now(**delta):
    return fmt_ts(datetime.now(timezone.utc) + timedelta(**delta))


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
    assert "Your feed" in html and "post 3" in html and "<span class=\"\">Keep</span>" in html
    tid = one(bouncer, "SELECT id FROM archived_threads ORDER BY id DESC")[0]
    r = client.post(f"/t/{tid}/keep", data={"anchor": f"p{tid}"}, headers={"referer": "http://testserver/"},
                    follow_redirects=False)
    assert r.headers["location"] == f"/#p{tid}"  # back to the post, not the top of the feed
    assert f'action="/t/{tid}/unkeep"' in client.get("/").text
    assert "post" in client.get("/kept").text
    client.post("/feed/mark-read")
    assert "all caught up" in client.get("/?unread=1").text


def test_post_text_action_only_for_posts_with_a_body(settings, server, bouncer):
    server.add_post("1", "with text", "The full post")
    server.add_post("2", "link and title only", "")
    server.add_post("3", "body containing only a link", "[the destination](https://example.test/story)")
    cid = bouncer.follow_community(f"!math@{DOMAIN}", 10, 7, backfill=True)
    bouncer.poll_follow(cid)
    with bouncer.db.connect() as conn:
        ids = {r["canonical_ap_id"]: r["id"] for r in conn.execute(
            "SELECT t.id, o.canonical_ap_id FROM archived_threads t JOIN objects o ON o.id=t.root_object_id"
        )}
        with_text = ids[f"https://{DOMAIN}/post/1"]
        without_text = ids[f"https://{DOMAIN}/post/2"]
        link_only = ids[f"https://{DOMAIN}/post/3"]
    client = TestClient(create_app(settings, bouncer))
    client.post("/login", data={"password": "pw"})
    html = client.get("/").text
    assert f'href="/t/{with_text}#post-text"' in html
    assert f'href="/t/{without_text}#post-text"' not in html
    assert f'href="/t/{link_only}#post-text"' not in html
    assert 'class="act direct-link" href="https://example.test/story"' in html
    assert 'aria-label="Open link"' in html


def test_mark_all_read_can_be_undone(settings, server, bouncer):
    followed_with_posts(server, bouncer)
    client = TestClient(create_app(settings, bouncer))
    client.post("/login", data={"password": "pw"})
    r = client.post("/feed/mark-read", headers={"referer": "http://testserver/", "X-ThreadBNC-Fetch": "1"})
    data = r.json()  # sent by app.js: where it would go, and the message with its Undo
    assert data["ok"] and data["redirect"].startswith("/?at=") and data["messages"][0]["text"] == "Marked 3 posts as read."
    undo = data["messages"][0]["undo"]
    assert "all caught up" in client.get("/?unread=1").text
    client.post(undo["action"], data=undo["fields"])
    assert "post 3" in client.get("/?unread=1").text


def test_posts_scrolled_past_are_read(settings, server, bouncer):
    followed_with_posts(server, bouncer)
    client = TestClient(create_app(settings, bouncer))
    client.post("/login", data={"password": "pw"})
    assert "data-mark-on-scroll" not in client.get("/").text  # off unless chosen
    client.post("/feed/settings", data={"mark_read_on_scroll": "1"})
    assert "data-mark-on-scroll" in client.get("/").text
    with bouncer.db.connect() as conn:
        ids = {r["title"]: r["id"] for r in conn.execute(
            "SELECT t.id, r.title FROM archived_threads t JOIN revisions r ON r.object_id=t.root_object_id")}
    seen = [ids["post 1"], ids["post 2"]]
    assert client.post("/feed/seen", data={"ids": seen}).json() == {"ok": True, "marked": 2}
    assert client.post("/feed/seen", data={"ids": seen}).json()["marked"] == 0  # already read
    unread = client.get("/?unread=1").text
    assert "post 3" in unread and "post 1" not in unread


def test_going_back_to_a_feed_shows_the_same_posts(settings, server, bouncer):
    """The page puts the time its list is as of in its address (app.js), so
    Back shows the same posts: read ones stay, new ones wait. A reload, or the
    feed without that time, is up to date."""
    cid = followed_with_posts(server, bouncer)
    client = TestClient(create_app(settings, bouncer))
    client.post("/login", data={"password": "pw"})
    page = client.get("/?unread=1").text
    as_of = re.search(r'data-as-of="([^"]+)"', page).group(1)
    assert "data-snapshot" not in page
    with bouncer.db.connect() as conn:
        ids = {r["title"]: r["id"] for r in conn.execute(
            "SELECT t.id, r.title FROM archived_threads t JOIN revisions r ON r.object_id=t.root_object_id")}
    client.post("/feed/seen", data={"ids": [ids["post 1"]]})  # scrolled past
    client.post(f"/t/{ids['post 2']}/read")  # opened
    server.add_post("4", "post 4", "new", created=utcnow())
    bouncer.poll_follow(cid)

    back = client.get("/", params={"unread": "1", "at": as_of}).text
    assert "data-snapshot" in back and all(f"post {n}" in back for n in (1, 2, 3)) and "post 4" not in back
    for fresh in (client.get("/", params={"unread": "1", "at": as_of}, headers={"cache-control": "max-age=0"}).text,
                  client.get("/?unread=1").text,
                  client.get("/", params={"unread": "1", "at": from_now(days=-2)}).text):  # too old to be a Back
        assert "post 4" in fresh and "post 3" in fresh and "post 1" not in fresh and "post 2" not in fresh
        assert "data-snapshot" not in fresh

    # Mark all read moves the list on to then, so what it marked goes.
    r = client.post("/feed/mark-read", headers={"referer": f"http://testserver/?unread=1&at={as_of}",
                                                "X-ThreadBNC-Fetch": "1"})
    assert "all caught up" in client.get(r.json()["redirect"]).text


def test_trash_undo_restores_every_copy(settings, server, bouncer):
    followed_with_posts(server, bouncer)
    client = TestClient(create_app(settings, bouncer))
    client.post("/login", data={"password": "pw"})
    tid = one(bouncer, "SELECT id FROM archived_threads ORDER BY id DESC")[0]
    data = client.post(f"/t/{tid}/trash", headers={"X-ThreadBNC-Fetch": "1"}).json()
    undo = data["messages"][0]["undo"]
    assert undo["action"] == f"/t/{tid}/restore" and "post 3" not in client.get("/").text
    client.post(undo["action"], data=undo["fields"])
    assert "post 3" in client.get("/").text


def test_theme_is_remembered_per_browser(settings, bouncer):
    client = TestClient(create_app(settings, bouncer))
    client.post("/login", data={"password": "pw"})
    assert "data-theme" not in client.get("/").text.split("<head>")[0]
    client.post("/theme", data={"theme": "dark"}, headers={"X-ThreadBNC-Fetch": "1"})
    assert '<html lang="en" data-theme="dark">' in client.get("/").text
    client.post("/theme", data={"theme": "system"})
    assert "data-theme" not in client.get("/").text.split("<head>")[0]


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
    tiles = client.get("/?view=tiles").text
    assert 'class="tile-action-row tile-action-primary"' in tiles
    assert 'class="tile-action-row tile-action-secondary actions"' in tiles
    assert 'class="vote up"' in tiles and "▲ 12" in tiles
    assert 'class="vote down"' in tiles and "▼ 3" in tiles
    page = client.get(f"/t/{tid}").text
    assert "▲ 12" in page and "▲ 5" in page and "▼ 1" in page
    # vote changes are current state, not edits
    assert one(bouncer, "SELECT revision_count FROM objects WHERE object_type='post'")[0] == 1


def test_mark_one_post_read_and_unread(settings, server, bouncer):
    followed_with_posts(server, bouncer)
    client = TestClient(create_app(settings, bouncer))
    client.post("/login", data={"password": "pw"})
    tid = one(bouncer, "SELECT id FROM archived_threads ORDER BY id DESC")[0]  # post 3
    assert f'action="/t/{tid}/read"' in client.get("/").text
    r = client.post(f"/t/{tid}/read", data={"read": "1", "anchor": f"p{tid}"},
                    headers={"referer": "http://testserver/"}, follow_redirects=False)
    assert r.headers["location"] == f"/#p{tid}"
    unread = client.get("/?unread=1").text
    assert "post 3" not in unread and "post 2" in unread
    assert "Mark unread" in client.get("/").text
    client.post(f"/t/{tid}/read", data={"read": "0"}, headers={"referer": "http://testserver/"})
    assert "post 3" in client.get("/?unread=1").text


def test_unread_filter_picks_unread_posts_or_new_comments(settings, server, bouncer):
    followed_with_posts(server, bouncer)
    tid = one(bouncer, "SELECT t.id FROM archived_threads t JOIN objects o ON o.id=t.root_object_id "
                       "WHERE o.canonical_ap_id=?", f"https://{DOMAIN}/post/2")[0]
    with bouncer.db.transaction() as conn:
        feed.set_read(conn, utcnow(), [tid])
    server.add_comment("2", "20", "fresh")
    bouncer.sync_thread(tid)

    def titles(unread):
        with bouncer.db.connect() as conn:
            return [i["title"] for i in feed.load_feed(conn, unread=unread).items]
    assert titles("posts") == ["post 3", "post 1"]
    assert titles("comments") == ["post 2"]
    assert titles("any") == titles(True) == ["post 3", "post 2", "post 1"]
    assert feed.unread_mode("1") == "any" and feed.unread_mode("nonsense") == ""

    client = TestClient(create_app(settings, bouncer))
    client.post("/login", data={"password": "pw"})
    html = client.get("/?unread=comments").text
    assert "post 2" in html and "post 3" not in html and "unread=comments" in html
    client.post("/feed/mark-read")
    assert "No new comments on posts you" in client.get("/?unread=comments").text


def test_stale_check_failure_clears_once_the_server_answers(settings, server, bouncer):
    cid = followed_with_posts(server, bouncer)
    tid = one(bouncer, "SELECT id FROM archived_threads ORDER BY id LIMIT 1")[0]
    server.down = True
    for _ in range(8):  # a long outage: the next check is backed off by up to a day
        with bouncer.db.transaction() as conn:
            conn.execute("UPDATE community_follows SET next_poll_at=? WHERE community_id=?", (utcnow(), cid))
        try:
            bouncer.poll_follow(cid)
        except RemoteError:
            pass
    f = one(bouncer, "SELECT * FROM community_follows WHERE community_id=?", cid)
    assert f["last_error"] and f["next_poll_at"] > from_now(hours=12)

    client = TestClient(create_app(settings, bouncer))
    client.post("/login", data={"password": "pw"})
    assert "Checking this community is failing." in client.get(f"/c/{cid}").text

    # Its server answers again: a thread syncing brings the check back to its usual interval.
    server.down = False
    bouncer.sync_thread(tid)
    f = one(bouncer, "SELECT * FROM community_follows WHERE community_id=?", cid)
    assert f["next_poll_at"] <= from_now(minutes=f["poll_interval_minutes"])

    # Or check now, from the warning.
    client.post(f"/c/{cid}/check-now")
    assert one(bouncer, "SELECT next_poll_at FROM community_follows WHERE community_id=?", cid)[0] <= utcnow()
    bouncer.poll_follow(cid)
    assert "Checking this community is failing." not in client.get(f"/c/{cid}").text
