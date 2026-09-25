"""Following hashtags on Bluesky: posts picked out of its Jetstream."""

from __future__ import annotations

import json
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from threadbnc import jetstream
from threadbnc.jetstream import CURSOR, JOB, BlueskyTags, match
from threadbnc.web import create_app

from .test_bluesky import ALICE, bsky, one, post_view, signed_in, thread  # noqa: F401

POST = "app.bsky.feed.post"


def at(rkey: str) -> str:
    return f"at://{ALICE}/{POST}/{rkey}"


def stamp(**delta: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(**delta)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def event(rkey, text, tags=(), beside=None, reply=False, op="create", time_us=None) -> str:
    """One event from Jetstream: a post made (or deleted) on Bluesky."""
    record = {"$type": POST, "text": text, "createdAt": stamp()}
    if tags:
        record["facets"] = [{"index": {"byteStart": 0, "byteEnd": 1},
                             "features": [{"$type": "app.bsky.richtext.facet#tag", "tag": t}]} for t in tags]
    if beside:
        record["tags"] = beside
    if reply:
        record["reply"] = {"root": {"uri": at("root"), "cid": "c"}, "parent": {"uri": at("root"), "cid": "c"}}
    commit = {"rev": "r", "operation": op, "collection": POST, "rkey": rkey}
    if op == "create":
        commit |= {"record": record, "cid": "c" + rkey}
    return json.dumps({"did": ALICE, "time_us": time_us or int(time.time() * 1_000_000), "kind": "commit",
                       "commit": commit})


@pytest.fixture
def listening(bouncer, bsky):  # noqa: F811
    return BlueskyTags(bouncer, "wss://jetstream.test/subscribe", "test")


def run_due_now(b):
    """Run the jobs queued, those put off for later too."""
    with b.db.transaction() as conn:
        conn.execute("UPDATE jobs SET run_after=created_at WHERE status='queued'")
    while b.run_one_job():
        pass


def jobs(b):
    with b.db.connect() as conn:
        return [json.loads(r["payload_json"]) for r in conn.execute(
            "SELECT payload_json FROM jobs WHERE kind=? ORDER BY id", (JOB,))]


def test_only_new_posts_with_a_followed_hashtag_are_picked_out():
    tags = {"selfhosted", "cats"}
    assert match(event("a", "New rack #SelfHosted", ["SelfHosted"]), tags) == (at("a"), "selfhosted")
    assert match(event("b", "A rack", beside=["Self-Hosted"]), tags) == (at("b"), "selfhosted")  # tags beside the text
    assert match(event("c", "#cats and #selfhosted", ["cats", "selfhosted"]).encode(), tags) == (at("c"), "cats")
    assert match(event("d", "#dogs", ["dogs"]), tags) is None
    assert match(event("e", "#cats", ["cats"], reply=True), tags) is None  # replies belong under their post
    assert match(event("f", "", op="delete"), tags) is None
    assert match(event("g", "No hashtags at all"), tags) is None
    assert match("not JSON, #tag", tags) is None


def test_posts_picked_out_are_captured_into_their_hashtag(bouncer, bsky, listening):  # noqa: F811
    cid = bouncer.follow_community("#cats", None, 30, False)  # no ActivityPub actor here: Bluesky alone
    new = post_view("t1", "Look #cats", created=stamp(minutes=1), labels=["sexual"])
    old = post_view("t0", "Before the follow #cats", created=stamp(days=-1))
    bsky.author_feed += [{"post": new}, {"post": old}]
    got = listening.capture({"posts": {at("t1"): "cats", at("t0"): "cats", at("gone"): "cats"}})
    assert got == {"captured": 1, "missing": 1}
    t = one(bouncer, "SELECT t.*, o.canonical_ap_id FROM archived_threads t JOIN objects o ON o.id=t.root_object_id "
                     "WHERE t.community_id=?", cid)
    assert (t["source_domain"], t["retention"], t["canonical_ap_id"]) == (
        "bsky.app", "auto", f"https://bsky.app/profile/{ALICE}/post/t1")
    assert t["expires_at"]  # like any post captured for you
    # One the AppView hasn't got yet is asked for once more, a little later, then left out.
    assert jobs(bouncer) == [{"posts": {at("gone"): "cats"}, "again": True}]
    run_due_now(bouncer)
    assert len(jobs(bouncer)) == 1
    # Opening it reads its replies from Bluesky, and it stays under its hashtag.
    bsky.threads[new["uri"]] = thread(new, thread(post_view("re1", "Cute!", did="did:plc:bob",
                                                            handle="bob.bsky.social", reply_to=new["uri"])))
    bouncer.open_threads([t["id"]])
    assert one(bouncer, "SELECT community_id FROM archived_threads WHERE id=?", t["id"])["community_id"] == cid
    assert one(bouncer, "SELECT COUNT(*) AS n FROM objects WHERE thread_id=?", t["id"])["n"] == 2
    # Kept already: not asked for again.
    bsky.requests.clear()
    assert listening.capture({"posts": {at("t1"): "cats"}}) == {"captured": 0}
    assert bsky.requests == []


def test_posts_for_a_hashtag_no_longer_followed_are_dropped(bouncer, bsky, listening):  # noqa: F811
    cid = bouncer.follow_community("#cats", None, 30, False)
    bouncer.unfollow(cid)
    assert listening.capture({"posts": {at("t1"): "cats"}}) == {"captured": 0}
    assert bsky.requests == []


class FakeStream:
    """Jetstream's WebSocket: the events given, then ThreadBNC stopping."""

    def __init__(self, events, listener, each=None):
        self.events, self.listener, self.each = events, listener, each

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def recv(self, timeout=None):
        if self.events:
            if self.each:
                self.each()
            return self.events.pop(0)
        self.listener.stop()
        raise TimeoutError


def test_listening_notes_posts_and_carries_on_where_it_left_off(bouncer, bsky, listening, monkeypatch):  # noqa: F811
    bouncer.follow_community("#cats", None, 30, False)
    seen_at = int(time.time() * 1_000_000)
    events = [event("x1", "#cats!", ["cats"]), event("x2", "#dogs", ["dogs"]), event("x3", "plain", time_us=seen_at)]
    urls = []

    def connect(url, **kw):
        urls.append(url)
        return FakeStream(events, listening)

    monkeypatch.setattr(jetstream, "connect", connect)
    listening.run_forever()
    assert urls == ["wss://jetstream.test/subscribe?wantedCollections=app.bsky.feed.post"]
    assert jobs(bouncer) == [{"posts": {at("x1"): "cats"}}]
    assert bouncer.db.get_setting(CURSOR) == str(seen_at)
    assert listening.connected_since is None and listening.last_error is None
    # Starting again, it carries on from there; a place over an hour old is forgotten.
    listening._stop.clear()
    listening.run_forever()
    assert urls[-1].endswith(f"&cursor={seen_at}")
    with bouncer.db.transaction() as conn:
        conn.execute("UPDATE app_settings SET value=? WHERE key=?", (str(seen_at - 2 * 3600 * 1_000_000), CURSOR))
    listening._stop.clear()
    listening.run_forever()
    assert "cursor" not in urls[-1]


def test_listening_stops_when_no_hashtag_is_followed(bouncer, bsky, listening, monkeypatch):  # noqa: F811
    cid = bouncer.follow_community("#cats", None, 30, False)
    events = [event("x1", "#cats", ["cats"]), event("x2", "#cats", ["cats"])]
    monkeypatch.setattr(jetstream, "connect",
                        lambda url, **kw: FakeStream(events, listening, each=lambda: bouncer.unfollow(cid)))
    listening._listen({"cats"})  # unfollowed while reading the first: returns, rather than reading on
    assert len(events) == 1 and jobs(bouncer) == [{"posts": {at("x1"): "cats"}}]


def test_a_stream_that_cant_be_reached_is_tried_again(bouncer, bsky, listening, monkeypatch):  # noqa: F811
    bouncer.follow_community("#cats", None, 30, False)
    waits = []

    def connect(url, **kw):
        raise OSError("Connection refused")

    monkeypatch.setattr(jetstream, "connect", connect)
    monkeypatch.setattr(listening._stop, "wait", lambda s: (waits.append(s), len(waits) == 3 and listening.stop()))
    listening.run_forever()
    assert waits == [1.0, 2.0, 4.0] and listening.last_error == "Connection refused"


def test_a_bluesky_post_in_a_hashtag_is_liked_as_your_bluesky_account(settings, bouncer, bsky, listening):  # noqa: F811
    client = signed_in(settings, bouncer, bsky)
    cid = bouncer.follow_community("#cats", None, 30, False)
    bsky.author_feed.append({"post": post_view("t1", "Look #cats", created=stamp(minutes=1))})
    listening.capture({"posts": {at("t1"): "cats"}})
    page = client.get(f"/c/{cid}").text
    assert 'title="Like as @dave.bsky.social"' in page  # not your Mastodon account, which this hashtag's other posts use
    tid = one(bouncer, "SELECT id FROM archived_threads WHERE community_id=?", cid)["id"]
    assert "Comment as @dave.bsky.social" in client.get(f"/t/{tid}").text


def test_a_hashtags_page_says_its_listening_to_bluesky(settings, bouncer, bsky):  # noqa: F811
    settings = replace(settings, embedded_bouncer=True)
    app = create_app(settings, bouncer)  # the listener isn't started without the app's lifespan
    client = TestClient(app)
    client.post("/login", data={"password": "pw"})
    cid = bouncer.follow_community("#cats", None, 30, False)
    assert "Connecting" in client.get(f"/c/{cid}").text
    app.state.bluesky_tags.connected_since = stamp()
    page = client.get(f"/c/{cid}").text
    assert "From Bluesky" in page and "posts tagged #cats are kept" in page
    app.state.bluesky_tags.connected_since, app.state.bluesky_tags.last_error = None, "Connection refused"
    assert "can&#39;t be reached" in client.get(f"/c/{cid}").text or "can't be reached" in client.get(f"/c/{cid}").text
