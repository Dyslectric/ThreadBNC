from __future__ import annotations

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from threadbnc import dupes
from threadbnc.adapters import NCommunity
from threadbnc.db import open_database
from threadbnc.web import create_app

from .conftest import BOB, DOMAIN

HOME = "home.test"
PHYSICS = NCommunity(ap_id=f"https://{DOMAIN}/c/physics", name="physics", domain=DOMAIN, title="Physics")


def one(b, sql, *args):
    with b.db.connect() as conn:
        return conn.execute(sql, args).fetchone()


def test_link_keys_ignore_trivial_differences():
    same = ["https://www.example.com/story/?utm_source=x&id=4", "http://example.com/story?id=4#top",
            "https://example.com/story?fbclid=abc&id=4"]
    assert len({dupes.post_key("t", None, u) for u in same}) == 1
    assert dupes.post_key("t", None, "https://youtu.be/abc123?si=zz") == \
        dupes.post_key("t", None, "https://www.youtube.com/watch?v=abc123&feature=share")
    assert dupes.post_key("t", None, "https://example.com/a") != dupes.post_key("t", None, "https://example.com/b")


def test_text_keys_see_through_crosspost_quoting():
    original = dupes.post_key("Big news", "We did it.\n\nSecond line", None)
    crosspost = dupes.post_key("Big news", "cross-posted from: https://lemmy.test/post/1\n\n"
                                           "> We did it.\n>\n> Second line", None)
    assert original == crosspost
    assert dupes.post_key("Weekly thread", "", None) is None  # a bare title is too weak


@pytest.fixture
def pair(server, bouncer):
    """The same link posted in !math by alice and in !physics by bob, each
    with a copy of the same comment from bob and one comment of their own."""
    p1 = server.add_post("1", "A great article", "")
    server.posts["1"] = replace(p1, url="https://example.com/article?utm_source=feed", upvotes=10, downvotes=1,
                                score=9)
    p2 = server.add_post("2", "Great read", "")
    server.posts["2"] = replace(p2, url="https://www.example.com/article", community=PHYSICS, author=BOB,
                                upvotes=4, downvotes=2, score=2)
    server.add_comment("1", "10", "Source is paywalled, here's a mirror")
    server.add_comment("2", "20", "Source is paywalled, here's  a mirror")  # same text, same author (bob)
    server.add_comment("1", "11", "Only in math")
    server.add_comment("2", "21", "Only in physics")
    server.add_comment("1", "12", "reply under math copy", parent="10")
    server.add_comment("2", "22", "reply under physics copy", parent="20")
    t1 = bouncer.ingest_url(f"https://{DOMAIN}/post/1")
    t2 = bouncer.ingest_url(f"https://{DOMAIN}/post/2")
    return t1, t2


def client_for(settings, bouncer, account=False):
    client = TestClient(create_app(settings, bouncer))
    client.post("/login", data={"password": "pw"})
    if account:
        client.post("/accounts", data={"server": HOME, "username": "dave", "password": "hunter2"})
    return client


def test_duplicates_share_a_key(bouncer, pair):
    t1, t2 = pair
    k1 = one(bouncer, "SELECT o.dupe_key FROM archived_threads t JOIN objects o ON o.id=t.root_object_id "
                      "WHERE t.id=?", t1)[0]
    k2 = one(bouncer, "SELECT o.dupe_key FROM archived_threads t JOIN objects o ON o.id=t.root_object_id "
                      "WHERE t.id=?", t2)[0]
    assert k1 == k2 == "url:example.com/article"


def test_feed_shows_duplicates_once_with_every_community(settings, bouncer, server, pair):
    server.communities["physics"] = PHYSICS
    bouncer.follow_community(f"!math@{DOMAIN}", 15, 30, False)
    bouncer.follow_community(f"!physics@{DOMAIN}", 15, 30, False)
    assert one(bouncer, "SELECT COUNT(*) FROM community_follows WHERE active=1")[0] == 2
    client = client_for(settings, bouncer)
    page = client.get("/").text
    assert page.count('class="pc-title"') == 1
    assert "×2" in page and "!physics" in page and "bob@other.test" in page
    assert "Votes by server" in page and "▲ 14" in page  # 10 + 4 upvotes, broken down per copy
    assert "6 comments" in page
    # The community page shows its own copy, and where else it was posted.
    with bouncer.db.connect() as conn:
        physics = conn.execute("SELECT id FROM communities WHERE name='physics'").fetchone()[0]
    cpage = client.get(f"/c/{physics}").text
    assert "Great read" in cpage and "!math" in cpage


def test_thread_page_combines_copies_and_squashes_repeated_comments(settings, bouncer, pair):
    t1, t2 = pair
    client = client_for(settings, bouncer)
    page = client.get(f"/t/{t1}").text
    assert "Posted 2 times" in page and "Great read" in page  # the other copy's title
    assert "Only in math" in page and "Only in physics" in page
    assert page.count("Source is paywalled") == 1
    assert "×2" in page and "1 repeat squashed" in page and "5 comments" in page
    # Both replies hang under the one squashed comment.
    squashed = page.index("Source is paywalled")
    assert squashed < page.index("reply under math copy") and squashed < page.index("reply under physics copy")
    assert "Votes by server" in page
    # Each copy's comments are a section of their own; the squashed one sits in the first's.
    math, physics = page.index(f'id="cs{t1}"'), page.index(f'id="cs{t2}"')
    assert math < squashed < page.index("Only in math") < physics < page.index("Only in physics")
    assert 'class="section-rail"' in page
    # Viewing one copy marks both read.
    assert one(bouncer, "SELECT COUNT(*) FROM archived_threads WHERE last_viewed_at IS NULL")[0] == 0
    alone = client.get(f"/t/{t1}?merge=0").text
    assert "Only in physics" not in alone and "Show them together" in alone


def test_comment_goes_to_the_chosen_copies(settings, bouncer, server, pair):
    t1, t2 = pair
    client = client_for(settings, bouncer, account=True)
    page = client.get(f"/t/{t1}").text
    assert 'name="to"' in page
    # Each copy's section has a box of its own, for a comment on just that one.
    sections = page[page.index(f'id="cs{t1}"'):]
    assert f'action="/t/{t1}/reply"' in sections and f'action="/t/{t2}/reply"' in sections
    roots = {t: one(bouncer, "SELECT root_object_id FROM archived_threads WHERE id=?", t)[0] for t in pair}
    r = client.post(f"/t/{t1}/reply", data={"body": "on both", "choose": "1", "to": [roots[t1], roots[t2]]},
                    follow_redirects=False)
    assert r.status_code == 303
    assert {pid for pid, c in server.outbox if c.body == "on both"} == {"1", "2"}
    r = client.post(f"/t/{t1}/reply", data={"body": "only physics", "choose": "1", "to": [roots[t2]]})
    assert [pid for pid, c in server.outbox if c.body == "only physics"] == ["2"]
    r = client.post(f"/t/{t1}/reply", data={"body": "nowhere", "choose": "1"})
    assert "Pick at least one" in r.text and not any(c.body == "nowhere" for _, c in server.outbox)
    # Our own comment on both copies shows once, squashed.
    page = client.get(f"/t/{t1}").text
    assert page.count("on both") == 1


def test_reply_to_a_squashed_comment_can_go_under_each_copy(settings, bouncer, server, pair):
    t1, _ = pair
    client = client_for(settings, bouncer, account=True)
    ids = [one(bouncer, "SELECT id FROM objects WHERE canonical_ap_id=?", f"https://{DOMAIN}/comment/{n}")[0]
           for n in ("10", "20")]
    client.post(f"/t/{t1}/reply", data={"body": "thanks", "choose": "1", "to": ids})
    assert sorted((pid, c.parent_local_id) for pid, c in server.outbox) == [("1", "10"), ("2", "20")]


def test_vote_on_squashed_copies(settings, bouncer, server, pair):
    t1, t2 = pair
    client = client_for(settings, bouncer, account=True)
    roots = [one(bouncer, "SELECT root_object_id FROM archived_threads WHERE id=?", t)[0] for t in pair]
    client.post(f"/o/{roots[0]}/vote", data={"score": "1", "also": [roots[1]]})
    assert server.votes[("dave", "post:1")] == 1 and server.votes[("dave", "post:2")] == 1


def test_hiding_a_duplicate_hides_every_copy(settings, bouncer, pair):
    t1, t2 = pair
    client = client_for(settings, bouncer)
    client.post(f"/t/{t1}/trash", data={"group": "1"})
    assert one(bouncer, "SELECT COUNT(*) FROM archived_threads WHERE trashed_at IS NOT NULL")[0] == 2


def test_keys_are_backfilled_for_older_databases(settings, bouncer, pair):
    with bouncer.db.transaction() as conn:
        conn.execute("UPDATE objects SET dupe_key=NULL")
        conn.execute("DELETE FROM app_settings WHERE key='dupe_keys_v1'")
    db = open_database(settings)
    with db.connect() as conn:
        keys = {r[0] for r in conn.execute("SELECT dupe_key FROM objects WHERE object_type='post'")}
    assert keys == {"url:example.com/article"}
