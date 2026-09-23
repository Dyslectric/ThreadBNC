"""Votes are updated on a schedule that slows as posts age and stops after a
week, asking as little as possible: your own server for pushed communities,
one listing for a checked community, and a single post only when it's kept on
its own."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from threadbnc.bouncer import vote_minutes
from threadbnc.db import fmt_ts, utcnow

from .conftest import DOMAIN, FakeAdapter


def ago(**kw) -> str:
    return fmt_ts(datetime.now(timezone.utc) - timedelta(**kw))


@pytest.fixture
def asked(monkeypatch):
    seen: list[str] = []
    for name in ("list_community_posts", "fetch_post", "fetch_comments"):
        real = getattr(FakeAdapter, name)

        def spy(self, *args, _real=real, _name=name, **kwargs):
            seen.append(f"{_name} {self.domain}")
            return _real(self, *args, **kwargs)

        monkeypatch.setattr(FakeAdapter, name, spy)
    return seen


def one(b, sql, *args):
    with b.db.connect() as conn:
        return conn.execute(sql, args).fetchone()


def make_due(b):
    with b.db.transaction() as conn:
        conn.execute("UPDATE archived_threads SET next_check_at='2000-01-01T00:00:00.000000Z'")


def vote(server, pid, score):
    server.posts[pid] = replace(server.posts[pid], score=score, upvotes=score, downvotes=0)


def score_of(b, server, pid):
    return one(b, "SELECT score FROM objects WHERE canonical_ap_id=?", server.posts[pid].ap_id)[0]


def test_the_schedule():
    now = utcnow()
    assert vote_minutes(ago(minutes=1), now) == 5
    assert vote_minutes(ago(minutes=45), now) == 10
    assert vote_minutes(ago(hours=3), now) == 30
    assert vote_minutes(ago(hours=20), now) == 60
    assert vote_minutes(ago(days=6), now) == 1440
    assert vote_minutes(ago(days=8), now) is None
    assert vote_minutes(None, now) is None


def test_a_checked_community_costs_one_listing_for_all_its_posts(server, bouncer, asked):
    for pid in ("1", "2", "3"):
        server.add_post(pid, f"post {pid}", "b", created=ago(minutes=10))
    cid = bouncer.follow_community(f"!math@{DOMAIN}", 15, 30, backfill=True, polling=True)
    bouncer.poll_follow(cid)
    for pid, score in (("1", 7), ("2", 8), ("3", 9)):
        vote(server, pid, score)
    make_due(bouncer)
    asked.clear()
    assert bouncer.check_votes() == 3
    assert asked == [f"list_community_posts {DOMAIN}"]
    assert [score_of(bouncer, server, p) for p in ("1", "2", "3")] == [7, 8, 9]
    nxt = one(bouncer, "SELECT MIN(next_check_at) FROM archived_threads")[0]
    assert nxt > utcnow()


def test_a_pushed_community_is_asked_of_your_own_server(server, bouncer, asked):
    server.add_post("1", "post", "b", created=ago(minutes=10))
    cid = bouncer.follow_community(f"!math@{DOMAIN}", 15, 30, backfill=True, polling=True)
    bouncer.poll_follow(cid)
    with bouncer.db.transaction() as conn:
        conn.execute("UPDATE community_follows SET push_state='subscribed', push_domain='home.test'")
    vote(server, "1", 12)
    make_due(bouncer)
    asked.clear()
    bouncer.check_votes()
    assert asked == ["list_community_posts home.test"]
    assert score_of(bouncer, server, "1") == 12


def test_a_post_kept_on_its_own_is_asked_about_by_itself(server, bouncer, asked):
    server.add_post("1", "kept by link", "b", created=ago(hours=2))
    server.add_comment("1", "10", "a comment")
    bouncer.ingest_url(f"https://{DOMAIN}/post/1")
    vote(server, "1", 20)
    make_due(bouncer)
    asked.clear()
    bouncer.check_votes()
    assert asked == [f"fetch_post {DOMAIN}"]  # never its comments
    assert score_of(bouncer, server, "1") == 20


def test_a_community_nothing_arrives_from_is_not_asked(server, bouncer, asked):
    server.add_post("1", "post", "b", created=ago(minutes=10))
    cid = bouncer.follow_community(f"!math@{DOMAIN}", 15, 30, backfill=True, polling=True)
    bouncer.poll_follow(cid)
    bouncer.set_polling(cid, False)  # not pushed and no longer checked
    make_due(bouncer)
    asked.clear()
    bouncer.check_votes()
    assert asked == []
    assert one(bouncer, "SELECT next_check_at FROM archived_threads")[0] is None


def test_votes_stop_after_a_week(server, bouncer):
    server.add_post("1", "old", "b", created=ago(days=8))
    bouncer.ingest_url(f"https://{DOMAIN}/post/1")
    assert one(bouncer, "SELECT next_check_at FROM archived_threads")[0] is None


def test_opening_a_post_updates_its_votes_even_after_a_week(server, bouncer):
    server.add_post("1", "old", "b", created=ago(days=8))
    tid = bouncer.ingest_url(f"https://{DOMAIN}/post/1")
    vote(server, "1", 33)
    with bouncer.db.transaction() as conn:
        conn.execute("UPDATE archived_threads SET last_full_fetch_at=NULL")
    bouncer.open_threads([tid])
    assert score_of(bouncer, server, "1") == 33


def test_community_checks_update_the_votes_they_see(server, bouncer):
    server.add_post("1", "post", "b", created=ago(days=9))  # past the vote schedule
    cid = bouncer.follow_community(f"!math@{DOMAIN}", 15, 30, backfill=True, polling=True)
    bouncer.poll_follow(cid)
    vote(server, "1", 40)
    bouncer.poll_follow(cid)
    assert score_of(bouncer, server, "1") == 40
