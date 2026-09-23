from __future__ import annotations

from datetime import datetime, timedelta, timezone

from threadbnc.bouncer import AUTO_CAPTURE_PAGE
from threadbnc.db import fmt_ts, parse_ts, utcnow

from .conftest import DOMAIN, FakeAdapter


def ago(**kw) -> str:
    return fmt_ts(datetime.now(timezone.utc) - timedelta(**kw))


def minutes_until_next_check(bouncer, tid):
    with bouncer.db.connect() as conn:
        nxt = conn.execute("SELECT next_check_at FROM archived_threads WHERE id=?", (tid,)).fetchone()[0]
    return None if nxt is None else (parse_ts(nxt) - parse_ts(utcnow())).total_seconds() / 60


def test_votes_are_checked_less_often_as_posts_age(server, bouncer):
    expected = {"new": 5, "forty_minutes": 10, "two_hours": 30, "half_day": 60, "two_days": 1440, "week": None}
    ages = {"new": ago(minutes=5), "forty_minutes": ago(minutes=40), "two_hours": ago(hours=2),
            "half_day": ago(hours=12), "two_days": ago(days=2), "week": ago(days=7, minutes=1)}
    for pid, key in enumerate(expected, start=101):
        server.add_post(str(pid), key, "b", created=ages[key])
        tid = bouncer.ingest_url(f"https://{DOMAIN}/post/{pid}")
        bouncer.sync_thread(tid)
        if expected[key] is None:
            assert minutes_until_next_check(bouncer, tid) is None, key
        else:
            assert abs(minutes_until_next_check(bouncer, tid) - expected[key]) < 1, key


class PagingAdapter(FakeAdapter):
    def list_community_posts(self, ref, sort="New", page=1, limit=20):
        posts = list(self.s.posts.values())
        pinned = [p for p in posts if p.featured]
        rest = sorted([p for p in posts if not p.featured], key=lambda p: p.created_at, reverse=True)
        ordered = pinned + rest  # like Lemmy: pinned first, then newest
        return ordered[(page - 1) * limit: page * limit]


def test_pinned_old_post_does_not_stop_paging(server, bouncer):
    bouncer._adapter_factory = lambda d: PagingAdapter(d, server)
    server.add_post("999", "Community rules", "b", created="2020-01-01T00:00:00.000000Z")
    server.edit_post("999", featured=True)
    cid = bouncer.follow_community(f"!math@{DOMAIN}", 15, 7)
    bouncer.poll_follow(cid)  # first poll: one page, nothing new yet
    n_new = AUTO_CAPTURE_PAGE + 10  # more than one page arrives between polls
    start = datetime.now(timezone.utc) + timedelta(seconds=1)  # posted after the follow
    for k in range(n_new):
        server.add_post(str(1000 + k), f"new {k}", "b", created=fmt_ts(start + timedelta(seconds=k)))
    assert bouncer.poll_follow(cid) == n_new


class CountingAdapter(FakeAdapter):
    comment_fetches = 0

    def fetch_comments(self, post_local_id):
        CountingAdapter.comment_fetches += 1
        return super().fetch_comments(post_local_id)


def test_comment_tree_skipped_when_counters_unchanged(server, bouncer):
    bouncer._adapter_factory = lambda d: CountingAdapter(d, server)
    server.add_post("1", "p", "b", created=ago(hours=1))
    server.add_comment("1", "10", "first")
    server.edit_post("1", comment_count=1, newest_comment_at="2026-09-21T01:00:00.000000Z")
    tid = bouncer.ingest_url(f"https://{DOMAIN}/post/1")
    CountingAdapter.comment_fetches = 0

    bouncer.sync_thread(tid)                      # nothing changed: post only
    assert CountingAdapter.comment_fetches == 0

    server.add_comment("1", "11", "second")       # counters move: full fetch
    server.edit_post("1", comment_count=2, newest_comment_at="2026-09-21T02:00:00.000000Z")
    res = bouncer.sync_thread(tid)
    assert CountingAdapter.comment_fetches == 1 and res.new_objects == 1

    server.edit_comment("1", "10", body="edited")  # edit doesn't move counters...
    bouncer.sync_thread(tid)
    assert CountingAdapter.comment_fetches == 1
    bouncer.sync_thread(tid, force=True)           # ...but "Re-check now" forces a full fetch
    assert CountingAdapter.comment_fetches == 2

    with bouncer.db.transaction() as conn:         # and so does the 6-hour backstop
        conn.execute("UPDATE archived_threads SET last_full_fetch_at=?", (ago(hours=7),))
    bouncer.sync_thread(tid)
    assert CountingAdapter.comment_fetches == 3


def test_missing_counters_always_fetch(server, bouncer):
    bouncer._adapter_factory = lambda d: CountingAdapter(d, server)
    server.add_post("1", "p", "b", created=ago(hours=1))  # no comment_count / newest_comment_at
    tid = bouncer.ingest_url(f"https://{DOMAIN}/post/1")
    CountingAdapter.comment_fetches = 0
    bouncer.sync_thread(tid)
    assert CountingAdapter.comment_fetches == 1
