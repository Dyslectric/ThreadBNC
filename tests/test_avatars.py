"""Pictures of who posted what, in the timeline view and on Trending (avatars.py)."""

from __future__ import annotations

from dataclasses import replace

from fastapi.testclient import TestClient

from threadbnc import avatars, media, trends
from threadbnc.db import utcnow
from threadbnc.web import create_app

from .conftest import ALICE, DOMAIN
from .test_articles import abouncer, fake_web  # noqa: F401
from .test_bluesky import bsky, post_view  # noqa: F401
from .test_tags import NOTE, PUBLIC, RELAY, client, deliver, fedi, follow, one, run_jobs, tagged  # noqa: F401
from .test_trends import at, rows, stamp, tid

PIC = "https://news.test/img/own.png"


def logged_in(settings, bouncer):
    c = TestClient(create_app(settings, bouncer))
    c.post("/login", data={"password": "pw"})
    return c


def test_a_profiles_picture_is_found_in_what_it_says():
    assert avatars.image_url({"icon": {"type": "Image", "url": PIC}}) == PIC
    assert avatars.image_url({"icon": [{"type": "Image", "url": [{"href": PIC}]}]}) == PIC
    assert avatars.image_url({"icon": PIC}) == PIC
    assert avatars.image_url({"icon": {"url": "http://plain.test/a.png"}}) is None
    assert avatars.image_url({}) is None
    assert avatars.lookable("https://masto.test/users/alice")
    assert not avatars.lookable("https://bsky.app/profile/did:plc:x") and not avatars.lookable("rss:https://x.test/")


def test_a_known_picture_is_downloaded_once_shown_and_kept(settings, server, abouncer):  # noqa: F811
    server.add_post("1", "Hello", "Good morning", created=utcnow())
    server.edit_post("1", author=replace(ALICE, avatar=PIC))
    cid = abouncer.follow_community(f"!math@{DOMAIN}", 10, 7, backfill=True)
    abouncer.poll_follow(cid)
    aid = one(abouncer, "SELECT id FROM actors WHERE avatar_url=?", PIC)["id"]
    abouncer.media.fetch_pending()
    assert rows(abouncer, "SELECT id FROM media WHERE url=?", PIC) == []  # nothing until it's on screen
    web = logged_in(settings, abouncer)
    page = web.get(f"/c/{cid}?view=timeline").text
    key = f"actor:{aid}"
    assert f'data-avatar="{key}"' in page and ">A</a>" in page  # the initial, meanwhile
    # On screen, it's asked for, downloaded, then shown.
    assert web.post("/avatars", data={"a": key}).json() == {"ready": {}, "waiting": [key]}
    abouncer.media.fetch_pending()
    got = web.get("/avatars", params={"a": key}).json()
    mid = one(abouncer, "SELECT avatar_media_id FROM actors WHERE id=?", aid)[0]
    assert got["waiting"] == [] and got["ready"][key].startswith(f"/media/{mid}")
    page = web.get(f"/c/{cid}").text
    assert f'<img src="/media/{mid}' in page.split('class="st-avatar')[1] and "data-avatar" not in page
    # Kept while they are, though no post refers to it.
    with abouncer.db.transaction() as conn:
        media.collect_orphans(conn, abouncer.media_dir)
    assert rows(abouncer, "SELECT status FROM media WHERE id=?", mid) == [{"status": "ok"}]
    # A new picture of theirs replaces it, downloaded when they're next shown.
    server.edit_post("1", author=replace(ALICE, avatar="https://news.test/img/wall.png"))
    abouncer.sync_thread(one(abouncer, "SELECT id FROM archived_threads")["id"], force=True)
    assert one(abouncer, "SELECT avatar_media_id FROM actors WHERE id=?", aid)[0] is None
    assert f'data-avatar="{key}"' in web.get(f"/c/{cid}").text


def test_a_relayed_posts_author_is_looked_up_once(client, tagged, fedi):  # noqa: F811
    b, _ = tagged
    cid = follow(b, client.app.state.tags)
    alice = "https://masto.test/users/alice"
    fedi.objects[alice] = (200, {"id": alice, "type": "Person", "preferredUsername": "alice",
                                 "icon": {"type": "Image", "url": "https://masto.test/avatars/alice.png"}})
    deliver(client, fedi, {"id": "https://relay.test/announce/a1", "type": "Announce", "actor": RELAY,
                           "to": [PUBLIC], "object": NOTE})
    run_jobs(b)
    aid = one(b, "SELECT id FROM actors WHERE canonical_ap_id=?", alice)["id"]
    auth = {"authorization": "Bearer tok"}
    key = f"actor:{aid}"
    assert f'data-avatar="{key}"' in client.get(f"/c/{cid}", headers=auth).text  # (a hashtag: the timeline)
    assert not any(str(g.url) == alice for g in fedi.gets)  # not looked up until shown
    assert client.post("/avatars", data={"a": key}, headers=auth).json()["waiting"] == [key]
    assert client.post("/avatars", data={"a": key}, headers=auth).json()["waiting"] == [key]
    run_jobs(b)
    assert sum(1 for g in fedi.gets if str(g.url) == alice) == 1  # once, however often it's asked for
    row = one(b, "SELECT a.avatar_url, m.status FROM actors a JOIN media m ON m.id=a.avatar_media_id WHERE a.id=?",
              aid)
    assert (row["avatar_url"], row["status"]) == ("https://masto.test/avatars/alice.png", "pending")
    # Someone without one isn't looked up again for a month.
    with b.db.transaction() as conn:
        conn.execute("UPDATE actors SET avatar_url=NULL, avatar_media_id=NULL WHERE id=?", (aid,))
    assert client.post("/avatars", data={"a": key}, headers=auth).json() == {"ready": {}, "waiting": []}
    assert "data-avatar" not in client.get(f"/c/{cid}", headers=auth).text


def test_trending_posts_are_a_timeline_with_their_authors_pictures(settings, abouncer, bsky):  # noqa: F811
    busy = tid()
    view = post_view(busy, "Big thread", likes=40, replies=12, created=stamp(hours=-1))
    view["author"]["avatar"] = PIC
    bsky.author_feed = [{"post": view}]
    tally = trends.Tally("bluesky")
    tally.reply(at(busy), trends.post_time(at(busy)))
    tally.flush(abouncer.db)
    trends.Trends(abouncer).check_bluesky()
    web = logged_in(settings, abouncer)
    page = web.get("/trending").text
    key = f"trend:bluesky {at(busy)}"
    items = page.split('id="items" class="timeline"')[1]
    assert '<article class="status trending-post"' in items and f'data-avatar="{key}"' in items
    assert 'class="st-name"' in items and "@alice.bsky.social" in items and ">A</a>" in items
    web.post("/avatars", data={"a": key})
    abouncer.media.fetch_pending()
    got = web.get("/avatars", params={"a": key}).json()
    assert got["waiting"] == [] and key in got["ready"]
    with abouncer.db.connect() as conn:
        assert trends.pictures_of(conn, [("bluesky", at(busy))]) == {}  # not one of the post's pictures
    page = web.get("/trending").text
    assert "data-avatar" not in page and f'<img src="{got["ready"][key]}"' in page


def test_pictures_asked_for_survive_a_restart(bouncer):
    """The bouncer's first pass over what's waiting stops downloads of links that
    aren't pictures: a picture asked for without a file extension isn't one of them."""
    now = utcnow()
    bare = "https://cdn.test/img/avatar/plain/did:plc:x/abc"
    with bouncer.db.transaction() as conn:
        conn.execute("INSERT INTO actors(canonical_ap_id, username, instance, first_seen_at, last_seen_at, "
                     "avatar_url) VALUES (?,?,?,?,?,?)", ("https://bsky.app/profile/did:plc:x", "x", "bsky.app",
                                                         now, now, bare))
        aid = conn.execute("SELECT id FROM actors").fetchone()[0]
        avatars.want(conn, [f"actor:{aid}"], now, False)
        media.skip_unprobed_links(conn)
    assert rows(bouncer, "SELECT status FROM media WHERE url=?", bare) == [{"status": "pending"}]
