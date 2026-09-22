from __future__ import annotations

import itertools
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from threadbnc import federation
from threadbnc.accounts import Poster
from threadbnc.federation import Federation, InboxRelay, Target, describe
from threadbnc.vault import TokenVault
from threadbnc.web import create_app

from .conftest import ALICE, COMMUNITY, DOMAIN

HOME = "home.test"  # your own server, whose inbox is relayed
UPSTREAM = "http://lemmy-home:8536"
_ids = itertools.count(1)


# --- activities, shaped like Lemmy's ------------------------------------------------

def note(server, post_id: str, comment_id: str, body: str | None = None, updated: str | None = None,
         parent_ap: str | None = None) -> dict:
    c = server.comments[post_id][comment_id]
    obj = {"type": "Note", "id": c.ap_id, "attributedTo": c.author.ap_id,
           "inReplyTo": parent_ap or server.posts[post_id].ap_id, "content": "<p>html</p>", "mediaType": "text/html",
           "source": {"content": body if body is not None else c.body, "mediaType": "text/markdown"},
           "published": c.created_at}
    if updated:
        obj["updated"] = updated
    return obj


def page(server, post_id: str) -> dict:
    p = server.posts[post_id]
    return {"type": "Page", "id": p.ap_id, "attributedTo": p.author.ap_id, "name": p.title,
            "source": {"content": p.body, "mediaType": "text/markdown"}, "published": p.created_at}


def activity(kind: str, obj, actor: str = ALICE.ap_id) -> dict:
    return {"id": f"https://{DOMAIN}/activities/{kind.lower()}/{next(_ids)}", "type": kind, "actor": actor,
            "object": obj}


def announce(inner: dict, community: str = COMMUNITY.ap_id) -> dict:
    return {"id": f"https://{DOMAIN}/activities/announce/{next(_ids)}", "type": "Announce", "actor": community,
            "object": inner}


# --- fixtures -----------------------------------------------------------------------

@pytest.fixture
def poster(bouncer, settings):
    return Poster(bouncer, TokenVault("test-key", settings.data_dir))


@pytest.fixture
def fed(bouncer, poster, server):
    """Your account on your own server, relaying its inbox, and !math followed."""
    poster.add(HOME, "dave", "hunter2")
    return Federation(poster, {HOME: UPSTREAM})


@pytest.fixture
def followed(fed, bouncer, server):
    server.add_post("1", "Question", "body")
    cid = bouncer.follow_community(f"!math@{DOMAIN}", 15, 30, True)
    bouncer.poll_follow(cid)
    return cid


def one(b, sql, *args):
    with b.db.connect() as conn:
        return conn.execute(sql, args).fetchone()


def push(fed, act: dict) -> None:
    fed.queue(HOME, "/inbox", json.dumps(act).encode())
    fed.process_pending()


def last_push(b):
    return one(b, "SELECT status, outcome FROM ap_inbox ORDER BY id DESC LIMIT 1")


def versions(b, ap_id: str) -> list:
    with b.db.connect() as conn:
        return [r[0] for r in conn.execute("SELECT r.body FROM revisions r JOIN objects o ON o.id=r.object_id "
                                           "WHERE o.canonical_ap_id=? ORDER BY r.seq", (ap_id,))]


# --- reading activities ---------------------------------------------------------------

def test_describe():
    obj = {"type": "Note", "id": f"https://{DOMAIN}/comment/9", "attributedTo": ALICE.ap_id}
    t = describe(announce(activity("Create", obj)))
    assert isinstance(t, Target) and t.ap_id == obj["id"] and t.payload == obj and t.community == COMMUNITY.ap_id
    assert describe(activity("Delete", obj["id"])) == Target(obj["id"], None, None)
    undo = describe(announce(activity("Undo", activity("Delete", obj["id"]))))
    assert isinstance(undo, Target) and undo.payload is None  # a restore: re-read, no content
    assert describe(announce(activity("Like", obj["id"]))) == "vote"
    assert describe(announce(activity("Undo", activity("Dislike", obj["id"])))) == "vote"
    assert describe(activity("Update", {"type": "Group", "id": COMMUNITY.ap_id})) == "Update of Group"
    assert describe(activity("Follow", COMMUNITY.ap_id)) == "Follow activity"
    # Content from a server other than its author's isn't taken as is.
    spoofed = describe(activity("Create", {**obj, "attributedTo": "https://evil.test/u/x"}))
    assert isinstance(spoofed, Target) and spoofed.payload is None


# --- the relay --------------------------------------------------------------------------

def upstream_answering(status: int, seen: list):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, json={"ok": status < 300})
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def not_relayed(scope, receive, send):
    await send({"type": "http.response.start", "status": 299, "headers": []})
    await send({"type": "http.response.body", "body": b"app"})


def test_relay_passes_deliveries_on_and_queues_what_lemmy_accepts():
    seen, queued = [], []
    relay = InboxRelay(not_relayed, {HOME: UPSTREAM}, lambda *a: queued.append(a),
                       client=upstream_answering(200, seen))
    client = TestClient(relay, base_url=f"https://{HOME}")
    body = json.dumps({"id": "https://x.test/a/1", "type": "Create"}).encode()
    r = client.post("/inbox?x=1", content=body, headers={"signature": "keyId=...", "digest": "SHA-256=abc",
                                                          "content-type": "application/activity+json"})
    assert r.status_code == 200 and r.json() == {"ok": True}
    [req] = seen
    assert str(req.url) == f"{UPSTREAM}/inbox?x=1" and req.content == body
    assert req.headers["host"] == HOME  # part of what the signature covers
    assert req.headers["signature"] == "keyId=..." and req.headers["digest"] == "SHA-256=abc"
    assert queued == [(HOME, "/inbox", body)]
    client.post("/u/dave/inbox", content=body)
    client.post("/c/math/inbox", content=body)
    assert len(queued) == 3
    # Not an inbox, not a POST, or not one of your servers: the app's own.
    assert client.get("/inbox").status_code == 299
    assert client.post("/api/v3/post", content=b"{}").status_code == 299
    assert TestClient(relay, base_url="https://threadbnc.test").post("/inbox", content=body).status_code == 299
    assert len(queued) == 3 and len(seen) == 3


def test_relay_queues_nothing_lemmy_refused_and_asks_for_retries_when_down():
    seen, queued = [], []
    refused = TestClient(InboxRelay(not_relayed, {HOME: UPSTREAM}, lambda *a: queued.append(a),
                                    client=upstream_answering(401, seen)), base_url=f"https://{HOME}")
    assert refused.post("/inbox", content=b'{"type": "Create"}').status_code == 401  # e.g. a bad signature
    assert queued == []

    def down(request):
        raise httpx.ConnectError("refused")
    relay = InboxRelay(not_relayed, {HOME: UPSTREAM}, lambda *a: queued.append(a),
                       client=httpx.AsyncClient(transport=httpx.MockTransport(down)))
    client = TestClient(relay, base_url=f"https://{HOME}")
    assert client.post("/inbox", content=b"{}").status_code == 502  # the sender tries again later
    assert client.post("/inbox", content=b"x" * (federation.MAX_ACTIVITY_BYTES + 1)).status_code == 413
    assert queued == []


def test_app_relays_before_signing_in(settings, bouncer, server, monkeypatch):
    """The relay sits in front of the app's sign-in: senders have no session."""
    seen: list = []
    upstream = upstream_answering(202, seen)
    monkeypatch.setattr(federation.httpx, "AsyncClient", lambda **kw: upstream)
    settings.relay_inboxes = {HOME: UPSTREAM}
    app = create_app(settings, bouncer)
    client = TestClient(app, base_url=f"https://{HOME}")
    act = activity("Delete", f"https://{DOMAIN}/comment/1")
    r = client.post("/inbox", content=json.dumps(act).encode())
    assert r.status_code == 202 and len(seen) == 1
    assert one(bouncer, "SELECT activity_id, status FROM ap_inbox")[:] == (act["id"], "pending")
    # A retried delivery is queued once.
    client.post("/inbox", content=json.dumps(act).encode())
    assert one(bouncer, "SELECT COUNT(*) FROM ap_inbox")[0] == 1
    # Everything else on your server's domain still needs signing in.
    assert TestClient(app, base_url=f"https://{HOME}").get("/", follow_redirects=False).status_code == 303


# --- subscriptions ------------------------------------------------------------------

def test_following_subscribes_and_polls_rarely(fed, followed, bouncer, server):
    f = one(bouncer, "SELECT * FROM community_follows WHERE community_id=?", followed)
    assert f["push_domain"] == HOME and f["push_state"] == "subscribed" and ("dave", "77") in server.subscriptions
    # Checked only every 6 hours now, to reconcile.
    assert f["next_poll_at"] > f["last_polled_at"][:11]
    from threadbnc.db import parse_ts
    gap = parse_ts(f["next_poll_at"]) - parse_ts(f["last_polled_at"])
    assert gap.total_seconds() == 360 * 60
    bouncer.unfollow(followed)
    assert server.subscriptions == set()
    assert one(bouncer, "SELECT push_state FROM community_follows WHERE community_id=?", followed)[0] is None


def test_pending_subscription_polls_as_usual_until_accepted(fed, bouncer, server):
    server.subscribe_answer = "pending"  # a private community: a moderator has to accept
    server.add_post("1", "Question", "body")
    cid = bouncer.follow_community(f"!math@{DOMAIN}", 15, 30, True)
    bouncer.poll_follow(cid)
    f = one(bouncer, "SELECT * FROM community_follows WHERE community_id=?", cid)
    from threadbnc.db import parse_ts
    assert f["push_state"] == "pending"
    assert (parse_ts(f["next_poll_at"]) - parse_ts(f["last_polled_at"])).total_seconds() == 15 * 60
    server.subscribe_answer = "subscribed"
    fed.housekeeping()
    assert one(bouncer, "SELECT push_state FROM community_follows WHERE community_id=?", cid)[0] == "subscribed"


def test_no_account_on_your_server_means_polling(bouncer, poster, server):
    poster.add(DOMAIN, "dave", "hunter2")  # an account, but not on a relayed server
    Federation(poster, {HOME: UPSTREAM})
    cid = bouncer.follow_community(f"!math@{DOMAIN}", 15, 30, False)
    assert one(bouncer, "SELECT push_state FROM community_follows WHERE community_id=?", cid)[0] is None
    assert server.subscriptions == set()


# --- the push worker ------------------------------------------------------------------

def test_new_comment_is_recorded_as_it_arrives(fed, followed, bouncer, server):
    c = server.add_comment("1", "10", "hello there", author=ALICE)
    push(fed, announce(activity("Create", note(server, "1", "10"))))
    assert last_push(bouncer)["status"] == "done"
    o = one(bouncer, "SELECT * FROM objects WHERE canonical_ap_id=?", c.ap_id)
    assert o["object_type"] == "comment" and o["discovered_late"] == 1
    assert versions(bouncer, c.ap_id) == ["hello there"]
    assert one(bouncer, "SELECT last_push_at FROM community_follows WHERE community_id=?", followed)[0]


def test_comment_deleted_before_it_was_read_keeps_its_text(fed, followed, bouncer, server):
    c = server.add_comment("1", "10", "regrettable", author=ALICE)
    payload = note(server, "1", "10")
    server.edit_comment("1", "10", deleted=True, body="")  # gone by the time the worker looks
    push(fed, announce(activity("Create", payload)))
    push(fed, announce(activity("Delete", c.ap_id)))
    o = one(bouncer, "SELECT * FROM objects WHERE canonical_ap_id=?", c.ap_id)
    assert o["cur_deleted"] == 1 and versions(bouncer, c.ap_id) == ["regrettable"]
    assert one(bouncer, "SELECT COUNT(*) FROM state_events WHERE object_id=? AND event_type='author_deleted'",
               o["id"])[0] == 1


def test_every_edit_is_kept_even_when_replaced_quickly(fed, followed, bouncer, server):
    c = server.add_comment("1", "10", "first", author=ALICE)
    push(fed, announce(activity("Create", note(server, "1", "10"))))
    server.edit_comment("1", "10", body="third", updated_at="2026-09-01T01:02:00.000000Z")  # the server's latest
    push(fed, announce(activity("Update", note(server, "1", "10", body="second",
                                                updated="2026-09-01T01:01:00.000000Z"))))
    push(fed, announce(activity("Update", note(server, "1", "10", updated="2026-09-01T01:02:00.000000Z"))))
    assert versions(bouncer, c.ap_id) == ["first", "second", "third"]
    # A later poll of the source adds nothing: the pushed versions match what it serves.
    bouncer.sync_thread(one(bouncer, "SELECT id FROM archived_threads")[0], force=True)
    assert versions(bouncer, c.ap_id) == ["first", "second", "third"]


def test_reply_is_placed_under_its_parent(fed, followed, bouncer, server):
    parent = server.add_comment("1", "10", "parent", author=ALICE)
    push(fed, announce(activity("Create", note(server, "1", "10"))))
    child = server.add_comment("1", "11", "child", parent="10", author=ALICE)
    push(fed, announce(activity("Create", note(server, "1", "11", parent_ap=parent.ap_id))))
    p = one(bouncer, "SELECT id FROM objects WHERE canonical_ap_id=?", parent.ap_id)[0]
    assert one(bouncer, "SELECT parent_id FROM objects WHERE canonical_ap_id=?", child.ap_id)[0] == p


def test_new_post_in_a_followed_community_is_captured_at_once(fed, followed, bouncer, server):
    server.add_post("2", "Breaking", "news", created="2099-01-01T00:00:00.000000Z")
    push(fed, announce(activity("Create", page(server, "2"))))
    assert last_push(bouncer)["status"] == "done"
    t = one(bouncer, "SELECT t.* FROM archived_threads t JOIN objects o ON o.id=t.root_object_id "
                     "WHERE o.canonical_ap_id=?", server.posts["2"].ap_id)
    assert t["retention"] == "auto" and t["source_domain"] == DOMAIN  # read from the community's home after that


def test_removal_and_lock_are_recorded(fed, followed, bouncer, server):
    from dataclasses import replace
    post_ap = server.posts["1"].ap_id
    server.posts["1"] = replace(server.posts["1"], locked=True)
    push(fed, announce(activity("Lock", post_ap)))
    oid = one(bouncer, "SELECT id FROM objects WHERE canonical_ap_id=?", post_ap)[0]
    assert one(bouncer, "SELECT cur_locked FROM objects WHERE id=?", oid)[0] == 1
    server.posts["1"] = replace(server.posts["1"], locked=False)
    push(fed, announce(activity("Undo", activity("Lock", post_ap))))
    assert one(bouncer, "SELECT cur_locked FROM objects WHERE id=?", oid)[0] == 0


def test_pushes_keep_the_source_servers_vote_counts(fed, followed, bouncer, server, monkeypatch):
    from dataclasses import replace
    from .conftest import FakeAdapter
    post_ap = server.posts["1"].ap_id
    with bouncer.db.transaction() as conn:
        conn.execute("UPDATE objects SET upvotes=500, score=480 WHERE canonical_ap_id=?", (post_ap,))
    real = FakeAdapter.fetch_post
    monkeypatch.setattr(FakeAdapter, "fetch_post", lambda self, lid: replace(real(self, lid), upvotes=3, score=3))
    server.add_comment("1", "10", "hi", author=ALICE)
    push(fed, announce(activity("Create", note(server, "1", "10"))))
    assert tuple(one(bouncer, "SELECT upvotes, score FROM objects WHERE canonical_ap_id=?", post_ap)) == (500, 480)


@pytest.mark.parametrize("act, why", [
    (announce(activity("Like", f"https://{DOMAIN}/post/1")), "vote"),
    (announce(activity("Update", {"type": "Group", "id": COMMUNITY.ap_id})), "Update of Group"),
])
def test_uninteresting_activities_are_skipped(fed, followed, bouncer, act, why):
    push(fed, act)
    assert tuple(last_push(bouncer)) == ("skipped", why)


def test_posts_outside_followed_communities_are_skipped(fed, bouncer, server):
    server.add_post("1", "Question", "body")  # nothing followed or kept
    push(fed, announce(activity("Create", page(server, "1"))))
    assert tuple(last_push(bouncer)) == ("skipped", "not in a followed community")
    assert one(bouncer, "SELECT COUNT(*) FROM archived_threads")[0] == 0


def test_server_trouble_is_retried(fed, followed, bouncer, server):
    server.add_comment("1", "10", "hi", author=ALICE)
    server.down = True
    push(fed, announce(activity("Create", note(server, "1", "10"))))
    row = one(bouncer, "SELECT * FROM ap_inbox ORDER BY id DESC LIMIT 1")
    assert row["status"] == "pending" and row["attempts"] == 1
    server.down = False
    with bouncer.db.transaction() as conn:
        conn.execute("UPDATE ap_inbox SET processed_at='2000-01-01T00:00:00.000000Z'")
    fed.process_pending()
    assert last_push(bouncer)["status"] == "done"


# --- the pages ---------------------------------------------------------------------------

def test_community_page_turns_pushes_off_and_on(settings, bouncer, server, poster):
    settings.relay_inboxes = {HOME: UPSTREAM}
    settings.credentials_key = "test-key"
    server.add_post("1", "Question", "body")
    client = TestClient(create_app(settings, bouncer))
    client.post("/login", data={"password": "pw"})
    client.post("/accounts", data={"server": HOME, "username": "dave", "password": "hunter2"})
    cid = bouncer.follow_community(f"!math@{DOMAIN}", 15, 30, False)
    assert "Pushed" in client.get(f"/c/{cid}").text
    client.post(f"/c/{cid}/push", data={"on": "0"})
    assert server.subscriptions == set()
    assert "Get pushes" in client.get(f"/c/{cid}").text
    assert "Subscribe to all" in client.get("/communities").text
    client.post("/communities/push-all")
    assert server.subscriptions == {("dave", "77")}
    assert "pushed" in client.get("/communities").text


def test_summaries_say_what_happened():
    note_obj = {"type": "Note", "id": f"https://{DOMAIN}/comment/9"}
    s = federation.summarize(json.dumps(announce(activity("Create", note_obj))))
    assert s == {"label": "New comment", "types": "Create Note", "object": note_obj["id"], "community": COMMUNITY.ap_id}
    assert federation.summarize(json.dumps(activity("Undo", activity("Lock", note_obj["id"]))))["label"] == "Unlocked"
    assert federation.summarize(json.dumps(announce(activity("Like", note_obj["id"]))))["label"] == "Vote"
    assert federation.summarize("not json")["label"] == "Unreadable"


def test_pushes_page_lists_deliveries_and_retries_failures(settings, bouncer, server):
    settings.relay_inboxes = {HOME: UPSTREAM}
    settings.credentials_key = "test-key"
    server.add_post("1", "Question", "body")
    app = create_app(settings, bouncer)
    fed = app.state.federation
    client = TestClient(app)
    client.post("/login", data={"password": "pw"})
    client.post("/accounts", data={"server": HOME, "username": "dave", "password": "hunter2"})
    cid = bouncer.follow_community(f"!math@{DOMAIN}", 15, 30, True)
    bouncer.poll_follow(cid)
    c = server.add_comment("1", "10", "hello", author=ALICE)
    push(fed, announce(activity("Create", note(server, "1", "10"))))
    push(fed, announce(activity("Like", c.ap_id)))
    server.down = True
    push(fed, announce(activity("Delete", c.ap_id)))
    with bouncer.db.transaction() as conn:
        conn.execute("UPDATE ap_inbox SET status='failed' WHERE status='pending'")
    oid, tid = one(bouncer, "SELECT id, thread_id FROM objects WHERE canonical_ap_id=?", c.ap_id)[:]

    page = client.get("/pushes").text
    assert "New comment" in page and "Vote" in page and "Deleted" in page
    assert f'href="/t/{tid}#o{oid}"' in page  # the recorded comment, in its thread
    assert "!math" in page and "pushed" in page and "Try failed again" in page
    only_failed = client.get("/pushes?status=failed").text
    assert "Deleted" in only_failed and "New comment" not in only_failed
    server.down = False
    client.post("/pushes/retry")
    assert one(bouncer, "SELECT status FROM ap_inbox WHERE activity_type='Announce' ORDER BY id DESC LIMIT 1")[0] \
        == "pending"
    fed.process_pending()
    assert one(bouncer, "SELECT cur_deleted FROM objects WHERE id=?", oid)[0] == 0  # the server never deleted it
    assert one(bouncer, "SELECT COUNT(*) FROM ap_inbox WHERE status='failed'")[0] == 0


def test_pushes_page_only_with_a_relayed_server(settings, bouncer):
    client = TestClient(create_app(settings, bouncer))
    client.post("/login", data={"password": "pw"})
    assert client.get("/pushes").status_code == 404
