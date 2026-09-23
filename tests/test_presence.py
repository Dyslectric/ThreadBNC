"""Subreddits are only checked while someone is using ThreadBNC, one at a time
and spread out, so coming back doesn't set off a burst of requests."""

from __future__ import annotations

from datetime import timedelta

from fastapi.testclient import TestClient

from threadbnc.db import fmt_ts, parse_ts, utcnow
from threadbnc.web import create_app

from .test_reddit import FakeReddit, reddit  # noqa: F401  (the fixture)


def listings(fake: FakeReddit) -> int:
    return sum(1 for r in fake.requests if r.endswith("/r/pics/new"))


def away(bouncer, minutes: int) -> None:
    then = fmt_ts(parse_ts(utcnow()) - timedelta(minutes=minutes))
    with bouncer.db.transaction() as conn:
        conn.execute("UPDATE app_settings SET value=? WHERE key='last_active_at'", (then,))


def test_a_tab_in_use_says_so(settings, bouncer):
    client = TestClient(create_app(settings, bouncer))
    client.post("/login", data={"password": "pw"})
    assert not bouncer.user_active()
    assert client.post("/presence").json() == {"ok": True}
    assert bouncer.user_active()


def test_subreddits_wait_for_you_and_then_come_one_at_a_time(bouncer, reddit):  # noqa: F811
    bouncer.reddit.connect_app("app1", "s3cret")
    bouncer.follow_community("r/pics", None, 30)
    now = [1000.0]
    bouncer._clock = lambda: now[0]
    bouncer.tick()
    assert listings(reddit) == 0  # nobody's here

    bouncer.note_active()
    bouncer.tick()
    assert listings(reddit) == 0  # just back: not at once
    now[0] += 3590  # one subreddit checked hourly: spread over the hour
    bouncer.tick()
    assert listings(reddit) == 0
    now[0] += 20
    bouncer.tick()
    assert listings(reddit) == 1

    away(bouncer, 31)  # the tab's been left alone
    now[0] += 7200
    bouncer.tick()
    assert listings(reddit) == 1
