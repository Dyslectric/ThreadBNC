"""Lemmy and PieFed communities arrive by push; checking them on a schedule is
something you turn on for one that can't be pushed."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from threadbnc.web import create_app

from .conftest import DOMAIN, FakeAdapter


@pytest.fixture
def listed(monkeypatch):
    seen: list[str] = []
    real = FakeAdapter.list_community_posts

    def spy(self, ref, *args, **kwargs):
        seen.append(ref.domain)
        return real(self, ref, *args, **kwargs)

    monkeypatch.setattr(FakeAdapter, "list_community_posts", spy)
    return seen


def one(b, sql, *args):
    with b.db.connect() as conn:
        return conn.execute(sql, args).fetchone()


def test_new_follows_are_not_checked_on_a_schedule(server, bouncer, listed):
    cid = bouncer.follow_community(f"!math@{DOMAIN}", 15, 30)
    assert one(bouncer, "SELECT polling FROM community_follows WHERE community_id=?", cid)[0] == 0
    bouncer.tick()
    assert listed == []


def test_checking_can_be_turned_on_and_off(server, bouncer, listed):
    server.add_post("1", "hello", "b", created="2099-01-01T00:00:00.000000Z")
    cid = bouncer.follow_community(f"!math@{DOMAIN}", 15, 30)
    bouncer.set_polling(cid, True)
    bouncer.tick()
    assert listed == [DOMAIN]
    assert one(bouncer, "SELECT COUNT(*) FROM archived_threads")[0] == 1
    bouncer.set_polling(cid, False)
    bouncer.tick()
    assert listed == [DOMAIN]


def test_following_with_checking_on(server, bouncer, listed):
    cid = bouncer.follow_community(f"!math@{DOMAIN}", 15, 30, polling=True)
    assert one(bouncer, "SELECT polling FROM community_follows WHERE community_id=?", cid)[0] == 1
    bouncer.tick()
    assert listed == [DOMAIN]


def test_pushed_communities_are_looked_over_on_your_own_server(server, bouncer, listed):
    cid = bouncer.follow_community(f"!math@{DOMAIN}", 15, 30)
    with bouncer.db.transaction() as conn:
        conn.execute("UPDATE community_follows SET push_state='subscribed', push_domain='home.test' "
                     "WHERE community_id=?", (cid,))
    bouncer.tick()
    assert listed == ["home.test"]


def test_feeds_and_subreddits_are_always_checked(bouncer):
    assert bouncer.always_polled("rss") and bouncer.always_polled("reddit.com")
    assert not bouncer.always_polled(DOMAIN)


def test_follows_from_before_keep_being_checked(bouncer):
    """Upgrading doesn't empty anyone's feed: follows stored before polling was
    opt-in get the column's default, on."""
    with bouncer.db.transaction() as conn:
        conn.execute("INSERT INTO communities(canonical_ap_id, name, first_seen_at, last_seen_at) "
                     "VALUES ('https://old.test/c/x', 'x', '2026-01-01', '2026-01-01')")
        cid = conn.execute("SELECT id FROM communities WHERE name='x'").fetchone()[0]
        conn.execute("INSERT INTO community_follows(community_id, followed_at, capture_since, poll_interval_minutes, "
                     "source_domain, source_ref) VALUES (?, '2026-01-01', '2026-01-01', 15, 'old.test', 'x')", (cid,))
    assert one(bouncer, "SELECT polling FROM community_follows WHERE community_id=?", cid)[0] == 1


def test_the_page_says_when_nothing_arrives(settings, server, bouncer):
    client = TestClient(create_app(settings, bouncer))
    client.post("/login", data={"password": "pw"})
    r = client.post("/follow", data={"community": f"!math@{DOMAIN}", "retention_days": "30"})
    assert "nothing will arrive yet" in r.text
    assert "Not arriving" in r.text and "Check it on a schedule" in r.text
    cid = one(bouncer, "SELECT community_id FROM community_follows")[0]
    r = client.post(f"/c/{cid}/polling", data={"on": "1"})
    assert "Stop checking" in r.text and "Not arriving" not in r.text
    assert "not arriving" not in client.get("/communities").text
    client.post(f"/c/{cid}/polling", data={"on": "0"})
    assert "not arriving" in client.get("/communities").text
