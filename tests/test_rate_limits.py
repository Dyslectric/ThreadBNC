"""Servers that answer 429 Too Many Requests are left alone for as long as they ask."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import httpx
import pytest

from threadbnc import media
from threadbnc.adapters import HttpClient, RemotePaused
from threadbnc.adapters.http import HostThrottle, retry_after
from threadbnc.db import parse_ts, utcnow

from .conftest import DOMAIN, FakeAdapter


def test_retry_after_reads_seconds_and_dates():
    assert retry_after("120") == 120
    assert retry_after(" 5 ") == 5
    later = format_datetime(datetime.now(timezone.utc) + timedelta(minutes=10), usegmt=True)
    assert 590 < retry_after(later) <= 600
    past = format_datetime(datetime.now(timezone.utc) - timedelta(minutes=10), usegmt=True)
    assert retry_after(past) == 0
    assert retry_after(None) is None
    assert retry_after("soon") is None


def test_a_429_pauses_only_that_server():
    t = HostThrottle(0)
    t.note("busy.test", 429, {"retry-after": "300"})
    with pytest.raises(RemotePaused) as exc:
        t.wait("busy.test")
    assert 298 < exc.value.seconds <= 300
    t.wait("quiet.test")  # other servers carry on


def test_your_own_servers_are_paused_too():
    t = HostThrottle(0)
    t.exempt.add("home.test")
    t.note("home.test", 429, {"retry-after": "30"})
    with pytest.raises(RemotePaused):
        t.wait("home.test")


def test_a_429_without_retry_after_backs_off_and_doubles():
    t = HostThrottle(0)
    t.note("busy.test", 429, {})
    assert 59 < t.paused_for("busy.test") <= 60
    t.note("busy.test", 429, {})
    assert 119 < t.paused_for("busy.test") <= 120
    t.note("busy.test", 200, {})  # a normal answer ends the run
    t._paused.clear()
    t.note("busy.test", 429, {})
    assert 59 < t.paused_for("busy.test") <= 60


def test_503_pauses_only_when_it_says_for_how_long():
    t = HostThrottle(0)
    t.note("down.test", 503, {})
    assert t.paused_for("down.test") == 0
    t.note("down.test", 503, {"retry-after": "90"})
    assert t.paused_for("down.test") > 80


def test_absurd_retry_after_is_capped_at_a_day():
    t = HostThrottle(0)
    t.note("busy.test", 429, {"retry-after": str(10 * 86400)})
    assert t.paused_for("busy.test") <= 86400


def test_api_client_stops_asking_after_a_429():
    calls = []

    def answer(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(429, headers={"retry-after": "120"}, json={"error": "rate_limit_error"})

    client = HttpClient("test", min_interval=0)
    client._client = httpx.Client(transport=httpx.MockTransport(answer))
    with pytest.raises(RemotePaused):
        client.get_json(DOMAIN, "/api/v3/post/list")
    with pytest.raises(RemotePaused):  # not even asked
        client.get_json(DOMAIN, "/api/v3/post/list")
    with pytest.raises(RemotePaused):  # nor as one of your accounts
        client.request_json("POST", DOMAIN, "/api/v3/comment", json={"content": "hi"}, throttle=False)
    assert len(calls) == 1


def test_paused_community_check_is_rescheduled_not_failed(server, bouncer, monkeypatch):
    cid = bouncer.follow_community(f"!math@{DOMAIN}", 15, None)

    def paused(*args, **kwargs):
        raise RemotePaused(f"{DOMAIN} asked us to slow down", 600)

    monkeypatch.setattr(FakeAdapter, "list_community_posts", paused)
    with pytest.raises(RemotePaused):
        bouncer.poll_follow(cid)
    with bouncer.db.connect() as conn:
        f = conn.execute("SELECT * FROM community_follows WHERE community_id=?", (cid,)).fetchone()
    assert f["consecutive_failures"] == 0 and f["last_error"] is None
    wait = (parse_ts(f["next_poll_at"]) - parse_ts(utcnow())).total_seconds()
    assert 590 < wait <= 600


def test_paused_thread_check_is_rescheduled_not_failed(server, bouncer, monkeypatch):
    server.add_post("1", "hello", "b")
    tid = bouncer.ingest_url(f"https://{DOMAIN}/post/1")

    def paused(*args, **kwargs):
        raise RemotePaused(f"{DOMAIN} asked us to slow down", 600)

    monkeypatch.setattr(FakeAdapter, "fetch_post", paused)
    with pytest.raises(RemotePaused):
        bouncer.sync_thread(tid)
    with bouncer.db.connect() as conn:
        t = conn.execute("SELECT * FROM archived_threads WHERE id=?", (tid,)).fetchone()
    assert t["consecutive_failures"] == 0
    assert 590 < (parse_ts(t["next_check_at"]) - parse_ts(utcnow())).total_seconds() <= 600


def test_paused_job_is_retried_without_using_up_attempts(server, bouncer, monkeypatch):
    server.add_post("1", "hello", "b")
    jid = bouncer.enqueue("ingest", {"url": f"https://{DOMAIN}/post/1"})

    def paused(*args, **kwargs):
        raise RemotePaused(f"{DOMAIN} asked us to slow down", 600)

    monkeypatch.setattr(FakeAdapter, "resolve_url", paused)
    assert bouncer.run_one_job()
    with bouncer.db.connect() as conn:
        job = conn.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
    assert job["status"] == "queued" and job["attempts"] == 0
    assert (parse_ts(job["run_after"]) - parse_ts(utcnow())).total_seconds() > 590


def test_media_download_waits_out_a_429(server, bouncer):
    calls = []

    def answer(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(429, headers={"retry-after": "300"})

    bouncer.media = media.MediaFetcher(bouncer.db, bouncer.media_dir, "t", 10_000_000,
                                       client=httpx.Client(transport=httpx.MockTransport(answer)),
                                       check_host=False)
    server.add_post("1", "pics", "![a](https://img.test/a.gif) ![b](https://img.test/b.gif)")
    bouncer.ingest_url(f"https://{DOMAIN}/post/1")
    bouncer.media.fetch_pending()
    assert len(calls) == 1  # the second picture wasn't asked for
    with bouncer.db.connect() as conn:
        rows = conn.execute("SELECT * FROM media").fetchall()
    for r in rows:
        assert r["status"] == "pending" and r["attempts"] == 0
        assert (parse_ts(r["next_attempt_at"]) - parse_ts(utcnow())).total_seconds() > 290
