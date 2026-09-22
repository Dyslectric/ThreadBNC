from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from threadbnc.accounts import AccountError, Poster
from threadbnc.adapters import Lemmy1Adapter, LemmyAdapter, NActor, NInboxItem, PieFedAdapter, RemoteNotFound
from threadbnc.adapters.http import HostThrottle
from threadbnc.adapters.reddit import RedditAdapter
from threadbnc.inbox import Inbox
from threadbnc.vault import TokenVault
from threadbnc.web import create_app

from .conftest import BOB, COMMUNITY, DOMAIN

HOME = "home.test"


def one(b, sql, *args):
    with b.db.connect() as conn:
        return conn.execute(sql, args).fetchone()


@pytest.fixture
def poster(bouncer, settings):
    settings.credentials_key = "test-key"
    return Poster(bouncer, TokenVault("test-key", settings.data_dir))


@pytest.fixture
def inbox(poster):
    return Inbox(poster, every_minutes=5)


@pytest.fixture
def dave(poster):
    return poster.add(HOME, "dave", "hunter2")


def reply_item(remote_id="7", body="Nice post!", unread=True, comment="10") -> NInboxItem:
    return NInboxItem(kind="reply", remote_id=remote_id, unread=unread, author=BOB, author_local_id="901",
                      body=body, created_at="2026-09-20T10:00:00.000000Z", object_type="comment",
                      object_ap_id=f"https://{DOMAIN}/comment/{comment}", object_local_id=comment,
                      post_ap_id=f"https://{DOMAIN}/post/1", post_local_id="1", post_title="Question",
                      community=COMMUNITY)


def message_item(remote_id="3", body="hey, got a minute?") -> NInboxItem:
    return NInboxItem(kind="message", remote_id=remote_id, unread=True, author=BOB, author_local_id="901",
                      body=body, created_at="2026-09-20T11:00:00.000000Z", object_type="message",
                      object_ap_id=f"https://other.test/private_message/{remote_id}", object_local_id=remote_id)


def test_sweep_collects_and_paces_checks(server, inbox, dave, bouncer):
    server.inboxes["dave"] = [reply_item(), message_item()]
    assert inbox.sweep() == 2 and server.inbox_fetches == 1
    assert inbox.unread_count() == 2
    assert inbox.sweep() == 0 and server.inbox_fetches == 1  # not due yet
    server.inboxes["dave"].append(reply_item("8", "another"))
    assert inbox.sweep(force=True) == 1 and inbox.unread_count() == 3
    items, more = inbox.items()
    assert not more and {i["kind"] for i in items} == {"reply", "message"}
    assert all(i["account"].handle == f"dave@{HOME}" for i in items)
    row = one(bouncer, "SELECT * FROM inbox_items WHERE kind='reply' AND remote_id='7'")
    assert row["author_name"] == "bob@other.test" and row["post_title"] == "Question"


def test_read_state_follows_the_server(server, inbox, dave):
    server.inboxes["dave"] = [reply_item(), message_item()]
    inbox.sweep()
    reply = next(i for i in inbox.items()[0] if i["kind"] == "reply")
    inbox.mark_read(reply["id"])
    assert not server.inboxes["dave"][0].unread and inbox.unread_count() == 1
    inbox.mark_read(reply["id"], read=False)
    assert server.inboxes["dave"][0].unread and inbox.unread_count() == 2
    server.inboxes["dave"][1].unread = False  # read in another app
    inbox.sweep(force=True)
    assert inbox.unread_count() == 1 and len(inbox.items(unread_only=True)[0]) == 1
    assert inbox.mark_all_read() == 1 and inbox.unread_count() == 0
    assert not any(i.unread for i in server.inboxes["dave"])


def test_reply_to_a_reply_lands_in_the_archived_thread(server, bouncer, inbox, dave):
    server.add_post("1", "Question", "body")
    server.add_comment("1", "10", "my comment", author=NActor(f"https://{HOME}/u/dave", "dave", HOME))
    tid = bouncer.ingest_url(f"https://{DOMAIN}/post/1")
    server.inboxes["dave"] = [reply_item()]
    inbox.sweep()
    item = inbox.items()[0][0]
    assert item["thread_id"] == tid  # linked to the archive
    oid = inbox.reply(item["id"], "thanks!")
    assert server.outbox[-1][0] == "1" and server.outbox[-1][1].parent_local_id == "10"
    new = one(bouncer, "SELECT o.*, r.body FROM objects o JOIN revisions r ON r.object_id=o.id WHERE o.id=?", oid)
    parent = one(bouncer, "SELECT id FROM objects WHERE canonical_ap_id=?", f"https://{DOMAIN}/comment/10")[0]
    assert new["body"] == "thanks!" and new["thread_id"] == tid and new["parent_id"] == parent
    assert inbox.unread_count() == 0 and not server.inboxes["dave"][0].unread


def test_reply_works_without_the_thread_in_the_archive(server, bouncer, inbox, dave):
    server.add_post("1", "Question", "body")  # on the server, but never archived
    server.inboxes["dave"] = [reply_item()]
    inbox.sweep()
    item = inbox.items()[0][0]
    assert item["thread_id"] is None
    assert inbox.reply(item["id"], "thanks!") is None and server.outbox[-1][1].body == "thanks!"
    assert one(bouncer, "SELECT COUNT(*) FROM objects")[0] == 0
    with pytest.raises(AccountError, match="Write something"):
        inbox.reply(item["id"], "   ")


def test_answering_a_message(server, inbox, dave):
    server.inboxes["dave"] = [message_item()]
    inbox.sweep()
    inbox.reply(inbox.items()[0][0]["id"], "sure")
    assert server.messages_sent == [("dave", "901", "sure", "3")]
    assert inbox.unread_count() == 0


def test_logged_out_account_is_reported_and_skipped(server, inbox, dave, poster):
    server.inboxes["dave"] = [reply_item()]
    server.revoked = True
    with pytest.raises(AccountError, match="log in again"):
        inbox.check(dave)
    assert poster.get(dave.id).status == "needs_login"
    status = inbox.status()[0]
    assert "log in again" in status["error"]
    fetches = server.inbox_fetches
    assert inbox.sweep(force=True) == 0 and server.inbox_fetches == fetches


def test_server_down_is_noted_not_fatal(server, inbox, dave, poster):
    server.down = True
    assert inbox.sweep() == 0
    assert "connection refused" in inbox.status()[0]["error"] and poster.get(dave.id).status == "ok"
    server.down = False
    server.inboxes["dave"] = [reply_item()]
    assert inbox.sweep(force=True) == 1 and inbox.status()[0]["error"] is None


def test_removing_the_account_removes_its_inbox(server, inbox, dave, poster, bouncer):
    server.inboxes["dave"] = [reply_item(), message_item()]
    inbox.sweep()
    poster.remove(dave.id)
    assert one(bouncer, "SELECT COUNT(*) FROM inbox_items")[0] == 0


def test_inbox_pages(settings, server, bouncer):
    server.add_post("1", "Question", "body")
    tid = bouncer.ingest_url(f"https://{DOMAIN}/post/1")
    client = TestClient(create_app(settings, bouncer))
    client.post("/login", data={"password": "pw"})
    assert "Add an account" in client.get("/inbox").text
    client.post("/accounts", data={"server": HOME, "username": "dave", "password": "hunter2"})
    server.inboxes["dave"] = [reply_item(body="**Nice** post"), message_item()]
    r = client.post("/inbox/check", headers={"referer": "http://testserver/inbox"})
    assert "Checked. 2 new." in r.text
    assert '<span class="pill new" aria-label="2 unread">2</span>' in client.get("/").text  # header count
    page = client.get("/inbox").text
    assert "<strong>Nice</strong> post" in page and "hey, got a minute?" in page
    assert f'href="/t/{tid}"' in page and "Reply to message" in page
    with bouncer.db.connect() as conn:
        rid = conn.execute("SELECT id FROM inbox_items WHERE kind='reply'").fetchone()[0]
        mid = conn.execute("SELECT id FROM inbox_items WHERE kind='message'").fetchone()[0]
    client.post(f"/inbox/{rid}/read", data={"read": "1"})
    page = client.get("/inbox").text
    assert "Nice" not in page and "hey, got a minute?" in page
    assert "Nice" in client.get("/inbox?show=all").text
    assert "got a minute" not in client.get("/inbox?show=all&kind=reply").text
    r = client.post(f"/inbox/{mid}/reply", data={"body": "on my way"}, headers={"referer": "http://testserver/inbox"})
    assert "Reply sent." in r.text and server.messages_sent[-1][2] == "on my way"
    assert "Nothing unread." in client.get("/inbox").text


# -- what each server sends back -----------------------------------------------------------

class ReplayHttp:
    def __init__(self, routes: dict[tuple[str, str], Any]):
        self.routes = routes
        self.throttle = HostThrottle(0)
        self.calls: list[tuple[str, str, dict, dict | None]] = []

    def get_json(self, domain, path, params=None, token=None):
        return self.request_json("GET", domain, path, params=params, token=token)

    def request_json(self, method, domain, path, *, params=None, json=None, token=None, throttle=True):
        for base in ("/api/v3", "/api/v4", "/api/alpha"):
            path = path.removeprefix(base)
        self.calls.append((method, path, {k: v for k, v in (params or {}).items() if v is not None}, json))
        if (method, path) not in self.routes:
            raise RemoteNotFound(f"no route for {method} {path}")
        return self.routes[(method, path)]


BOB_V3 = {"id": 901, "name": "bob", "actor_id": "https://other.test/u/bob"}
ME_V3 = {"id": 900, "name": "dave", "actor_id": f"https://{HOME}/u/dave"}
POST_V3 = {"id": 9, "name": "My post", "ap_id": f"https://{DOMAIN}/post/9"}
COMMUNITY_V3 = {"id": 4, "name": "selfhosted", "actor_id": f"https://{DOMAIN}/c/selfhosted"}


def comment_view(cid: int, text: str) -> dict:
    return {"comment": {"id": cid, "content": text, "published": "2026-09-20T10:00:00.000000Z", "deleted": False,
                        "removed": False, "ap_id": f"https://{DOMAIN}/comment/{cid}", "path": f"0.54.{cid}"},
            "creator": BOB_V3, "post": POST_V3, "community": COMMUNITY_V3, "counts": {"score": 1}}


def pm_view(pid: int, creator: dict, text: str, read: bool = False) -> dict:
    return {"private_message": {"id": pid, "content": text, "read": read, "deleted": False,
                                "published": "2026-09-20T11:00:00.000000Z", "ap_id": f"https://x.test/pm/{pid}"},
            "creator": creator, "recipient": ME_V3 if creator is BOB_V3 else BOB_V3}


def test_lemmy_inbox():
    http = ReplayHttp({
        ("GET", "/user/replies"): {"replies": [
            {"comment_reply": {"id": 7, "comment_id": 55, "read": False, "published": "x"}, **comment_view(55, "a reply")}]},
        ("GET", "/user/mention"): {"mentions": [
            {"person_mention": {"id": 8, "comment_id": 56, "read": True, "published": "x"}, **comment_view(56, "hi @dave")}]},
        ("GET", "/private_message/list"): {"private_messages": [
            pm_view(3, BOB_V3, "hello"), pm_view(4, ME_V3, "my own message")]},
        ("POST", "/user/mention/mark_as_read"): {}, ("POST", "/comment/mark_as_read"): {},
        ("POST", "/private_message"): {},
    })
    lemmy = LemmyAdapter(HOME, http)
    reply, mention, message = lemmy.inbox("tok", f"https://{HOME}/u/dave")  # my own message is left out
    assert (reply.kind, reply.remote_id, reply.unread, reply.object_local_id, reply.post_local_id) == (
        "reply", "7", True, "55", "9")
    assert reply.post_title == "My post" and reply.community.name == "selfhosted" and reply.author_local_id == "901"
    assert (mention.kind, mention.remote_id, mention.unread, mention.body) == ("mention", "8", False, "hi @dave")
    assert (message.kind, message.remote_id, message.body, message.author.username) == ("message", "3", "hello", "bob")
    assert http.calls[0][2]["unread_only"] == "false"
    lemmy.mark_inbox_read("tok", "mention", "8")
    assert http.calls[-1] == ("POST", "/user/mention/mark_as_read", {}, {"person_mention_id": 8, "read": True})
    lemmy.send_message("tok", "901", "yo", in_reply_to="3")
    assert http.calls[-1][3] == {"content": "yo", "recipient_id": 901}


def test_piefed_inbox_mentions_are_comment_replies():
    http = ReplayHttp({
        ("GET", "/user/replies"): {"replies": []},
        ("GET", "/user/mentions"): {"replies": [
            {"comment_reply": {"id": 12, "comment_id": 56, "read": False, "published": "x"}, **comment_view(56, "@dave")}]},
        ("GET", "/private_message/list"): {"private_messages": []},
        ("POST", "/comment/mark_as_read"): {},
    })
    piefed = PieFedAdapter(HOME, http)
    [mention] = piefed.inbox("tok", "me")
    assert (mention.kind, mention.remote_id) == ("mention", "12")
    piefed.mark_inbox_read("tok", "mention", "12", read=True)
    assert http.calls[-1] == ("POST", "/comment/mark_as_read", {}, {"comment_reply_id": 12, "read": True})


def test_lemmy1_notifications():
    v4_comment = {"comment": {"id": 55, "content": "a reply", "published_at": "2026-09-20T10:00:00Z",
                              "ap_id": f"https://{DOMAIN}/comment/55", "path": "0.54.55"},
                  "creator": BOB_V3, "post": POST_V3, "community": COMMUNITY_V3}
    v4_pm = {"private_message": {"id": 3, "content": "hello", "published_at": "2026-09-20T11:00:00Z",
                                 "ap_id": "https://x.test/pm/3", "deleted": False, "removed": False},
             "creator": BOB_V3, "recipient": ME_V3}
    v4_post = {"post": {"id": 9, "name": "Look @dave", "body": "see this", "ap_id": POST_V3["ap_id"],
                        "published_at": "2026-09-20T09:00:00Z"}, "creator": BOB_V3, "community": COMMUNITY_V3}
    http = ReplayHttp({
        ("GET", "/account/notification/list"): {"items": [
            {"notification": {"id": 70, "kind": "reply", "read": False}, "data": {"type_": "comment", **v4_comment}},
            {"notification": {"id": 71, "kind": "private_message", "read": True}, "data": {"type_": "private_message", **v4_pm}},
            {"notification": {"id": 72, "kind": "mention", "read": False}, "data": {"type_": "post", **v4_post}},
            {"notification": {"id": 73, "kind": "mod_action", "read": False}, "data": {"type_": "mod_action"}},
        ], "next_page": "abc"},
        ("POST", "/account/notification/mark_as_read"): {}, ("POST", "/account/notification/mark_as_read/all"): {},
    })
    lemmy1 = Lemmy1Adapter(HOME, http)
    reply, message, mention = lemmy1.inbox("tok", "me")
    assert (reply.kind, reply.remote_id, reply.object_local_id, reply.post_local_id) == ("reply", "70", "55", "9")
    assert (message.kind, message.remote_id, message.unread, message.body) == ("message", "71", False, "hello")
    assert (mention.object_type, mention.post_title, mention.object_local_id) == ("post", "Look @dave", "9")
    assert len([c for c in http.calls if c[1] == "/account/notification/list"]) == 1  # the first page only
    lemmy1.mark_inbox_read("tok", "reply", "70")
    assert http.calls[-1][3] == {"notification_id": 70, "read": True}


class RedditReader:
    def __init__(self, listing: dict):
        self.listing = listing
        self.posts: list[tuple[str, dict]] = []

    def get(self, path, params=None):
        assert path == "/message/inbox"
        return self.listing

    def post(self, path, form):
        self.posts.append((path, form))
        return {}


def test_reddit_inbox():
    reader = RedditReader({"kind": "Listing", "data": {"children": [
        {"kind": "t1", "data": {"name": "t1_abc", "id": "abc", "type": "comment_reply", "new": True, "author": "bob",
                                "body": "reply!", "created_utc": 1758362400, "subreddit": "selfhosted",
                                "link_title": "My setup", "context": "/r/selfhosted/comments/p9/my_setup/abc/?context=3"}},
        {"kind": "t1", "data": {"name": "t1_def", "id": "def", "type": "username_mention", "new": False,
                                "author": "carol", "body": "u/dave look", "created_utc": 1758362500,
                                "subreddit": "pics", "context": "/r/pics/comments/q1/x/def/?context=3"}},
        {"kind": "t4", "data": {"name": "t4_ghi", "id": "ghi", "new": True, "author": None, "subreddit": "pics",
                                "subject": "Your post", "body": "rule 3", "created_utc": 1758362600}},
    ]}})
    reddit = RedditAdapter(reader)
    reply, mention, message = reddit.inbox("", "me")
    assert (reply.kind, reply.remote_id, reply.unread, reply.post_local_id, reply.object_local_id) == (
        "reply", "t1_abc", True, "p9", "abc")
    assert reply.object_ap_id == "https://www.reddit.com/r/selfhosted/comments/p9/_/abc/"
    assert reply.post_title == "My setup" and reply.community.name == "selfhosted"
    assert (mention.kind, mention.unread) == ("mention", False)
    assert (message.kind, message.subject, message.author.username) == ("message", "Your post", "r/pics")
    reddit.mark_all_inbox_read("", [("reply", "t1_abc"), ("message", "t4_ghi")])
    assert reader.posts[-1] == ("/api/read_message", {"id": "t1_abc,t4_ghi"})
    reddit.send_message("", "", "ok", in_reply_to="t4_ghi")
    assert reader.posts[-1] == ("/api/comment", {"thing_id": "t4_ghi", "text": "ok"})


def test_reddit_sign_in_without_the_inbox_scope(bouncer, poster, inbox):
    bouncer.reddit._save("reddit_connection", {"mode": "user", "client_id": "c", "username": "dave",
                                               "scope": "identity read mysubreddits vote submit edit",
                                               "status": "ok"})
    poster.sync_reddit_account()
    account = next(a for a in poster.list() if a.is_reddit)
    assert "Connect Reddit again" in inbox.can_check(account)
    assert inbox.sweep() == 0
    cfg = bouncer.reddit.config()
    cfg["scope"] += " privatemessages"
    bouncer.reddit._save("reddit_connection", cfg)
    assert inbox.can_check(account) is None
