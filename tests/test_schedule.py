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
    return (parse_ts(nxt) - parse_ts(utcnow())).total_seconds() / 60


def test_recheck_interval_tapers_with_post_age(server, bouncer):
    cid = bouncer.follow_community(f"!math@{DOMAIN}", 15, None)
    expected = {"new": 15, "two_days": 60, "week": 360}
    tids = {}
    for pid, (key, created) in enumerate((("new", ago(hours=2)), ("two_days", ago(days=2)),
                                          ("week", ago(days=7))), start=101):
        server.add_post(str(pid), key, "b", created=created)
        tids[key] = bouncer.ingest_url(f"https://{DOMAIN}/post/{pid}")
    for key, tid in tids.items():
        bouncer.sync_thread(tid)
        assert abs(minutes_until_next_check(bouncer, tid) - expected[key]) < 1, key
    # a slow community interval is never made faster by the taper
    bouncer.update_follow(cid, 120, None)
    bouncer.sync_thread(tids["two_days"])
    assert abs(minutes_until_next_check(bouncer, tids["two_days"]) - 120) < 1


def test_unfollowed_kept_threads_use_default_then_taper(server, bouncer):
    server.add_post("1", "old kept", "b", created=ago(days=30))
    tid = bouncer.ingest_url(f"https://{DOMAIN}/post/1")
    assert abs(minutes_until_next_check(bouncer, tid) - 360) < 1


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
