"""Bluesky: accounts and custom feeds followed like communities, through its public API."""

from __future__ import annotations

import base64
import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from threadbnc.adapters import BSKY_DOMAIN, RemoteAuthError, RemoteNotFound, parse_community_ref, parse_thread_url
from threadbnc.adapters import bluesky
from threadbnc.adapters.bluesky import BlueskyAdapter, facets_for, rich_markdown
from threadbnc.web import create_app

ALICE = "did:plc:alice"
FEEDER = "did:plc:feeder"
DAVE = "did:plc:dave"  # you
PDS = "https://pds.test"


def jwt_exp(token):
    try:
        payload = token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))["exp"]
    except (IndexError, ValueError, KeyError):
        return 0


def post_view(rkey, text, did=ALICE, handle="alice.bsky.social", likes=3, replies=0, embed=None, facets=None,
              created="2026-09-23T10:00:00.000Z", labels=(), reply_to=None):
    record = {"$type": "app.bsky.feed.post", "text": text, "createdAt": created}
    if reply_to:
        record["reply"] = {"root": {"uri": reply_to, "cid": "croot"},
                           "parent": {"uri": reply_to, "cid": "croot"}}
    if facets:
        record["facets"] = facets
    view = {"uri": f"at://{did}/app.bsky.feed.post/{rkey}", "cid": "c" + rkey,
            "author": {"did": did, "handle": handle, "displayName": handle.split(".")[0].title()},
            "record": record, "likeCount": likes, "replyCount": replies, "repostCount": 0,
            "indexedAt": created, "labels": [{"val": v} for v in labels]}
    if embed:
        view["embed"] = embed
    return view


def thread(view, *replies):
    return {"$type": "app.bsky.feed.defs#threadViewPost", "post": view, "replies": list(replies)}


class FakeBluesky:
    """public.api.bsky.app, as get_json sees it."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, dict]] = []
        self.author_feed = [
            {"post": post_view("p2", "Second post, with a picture", likes=7, replies=2, embed={
                "$type": "app.bsky.embed.images#view",
                "images": [{"thumb": "https://cdn.bsky.app/t/1.jpg", "fullsize": "https://cdn.bsky.app/f/1.jpg",
                            "alt": "a cat"}]})},
            {"post": post_view("r1", "Someone else's post", did="did:plc:bob", handle="bob.bsky.social"),
             "reason": {"$type": "app.bsky.feed.defs#reasonRepost"}},
            {"post": post_view("p1", "First post", likes=1, created="2026-09-22T10:00:00.000Z")},
        ]
        self.feed_posts = [{"post": post_view("f1", "In the feed", did="did:plc:bob", handle="bob.bsky.social")}]
        self.feed_needs_login = False
        self.deleted: set[str] = set()
        self.threads: dict[str, dict] = {}
        self.sent: list[tuple] = []  # (method, url, query, headers, body) to your PDS
        self.records: dict[str, dict] = {}  # rkey -> createRecord's body, on your account
        self.access_token = self.refresh_token = None
        self.timeline: list[dict] = []
        self.notifications: list[dict] = []
        self.uploads: list[tuple[str, bytes]] = []
        self.seen_at = None

    def get_json(self, domain, path, params=None):
        assert domain == "public.api.bsky.app"
        method = path.removeprefix("/xrpc/")
        params = params or {}
        self.requests.append((method, params))
        if method == "com.atproto.identity.resolveHandle":
            if params["handle"] == "alice.bsky.social":
                return {"did": ALICE}
            if params["handle"] == "feeder.bsky.social":
                return {"did": FEEDER}
            if params["handle"] == "dave.bsky.social":
                return {"did": DAVE}
            if params["handle"] == "bob.bsky.social":
                return {"did": "did:plc:bob"}
            raise RemoteNotFound("Unable to resolve handle")
        if method == "app.bsky.actor.getProfile":
            if params["actor"] == DAVE:
                return {"did": DAVE, "handle": "dave.bsky.social", "displayName": "Dave"}
            assert params["actor"] == ALICE
            return {"did": ALICE, "handle": "alice.bsky.social", "displayName": "Alice", "description": "Hi."}
        if method == "app.bsky.feed.getAuthorFeed":
            assert params["filter"] == "posts_no_replies"
            return {"feed": self.author_feed}
        if method == "app.bsky.feed.getFeedGenerator":
            return {"view": {"uri": params["feed"], "displayName": "Cat Pics", "description": "Cats.",
                             "creator": {"did": FEEDER, "handle": "feeder.bsky.social"}},
                    "isOnline": True, "isValid": True}
        if method == "app.bsky.feed.getFeed":
            assert params["feed"] == f"at://{FEEDER}/app.bsky.feed.generator/cats"
            if self.feed_needs_login:
                raise RemoteAuthError("public.api.bsky.app: AuthMissing", "AuthMissing")
            return {"feed": self.feed_posts[:params.get("limit", 50)]}
        if method == "app.bsky.feed.getPosts":
            everything = [i["post"] for i in self.author_feed + self.feed_posts]

            def replies(node):
                for r in node.get("replies") or []:
                    everything.append(r["post"])
                    replies(r)

            for t in self.threads.values():
                replies(t)
            return {"posts": [p for p in everything if p["uri"] in params["uris"] and p["uri"] not in self.deleted]}
        if method == "app.bsky.feed.getPostThread":
            if params["uri"] in self.deleted:
                return {"thread": {"$type": "app.bsky.feed.defs#notFoundPost", "uri": params["uri"], "notFound": True}}
            return {"thread": self.threads[params["uri"]]}
        raise AssertionError(f"unexpected {method}")

    # -- your own account's server (a PDS), and the PLC directory ----------------------
    def send(self, method, url, headers=None, content=None, throttle=True):
        u = httpx.URL(url)
        body = json.loads(content) if content and headers.get("Content-Type") == "application/json" else None
        self.sent.append((method, str(u.copy_with(query=None)), dict(u.params), headers or {}, body))

        def answer(status, data):
            return httpx.Response(status, json=data)

        for key, value in (body or {}).items():  # as Bluesky's servers check a request's fields
            if value is None:
                return answer(400, {"error": "InvalidRequest",
                                    "message": f"Expected string value type (got null) at $.{key}"})

        if str(u) == f"https://plc.directory/{DAVE}":
            return answer(200, {"id": DAVE, "service": [{"id": "#atproto_pds", "type": "AtprotoPersonalDataServer",
                                                           "serviceEndpoint": PDS}]})
        assert str(u).startswith(PDS + "/xrpc/"), url
        name = u.path.removeprefix("/xrpc/")
        bearer = (headers or {}).get("Authorization", "").removeprefix("Bearer ")
        if name in ("com.atproto.server.refreshSession", "com.atproto.server.deleteSession") and content:
            return answer(400, {"error": "InvalidRequest",
                                "message": "A request body was provided when none was expected"})
        if name == "com.atproto.server.createSession":
            if body["password"] != "abcd-efgh-ijkl-mnop":
                return answer(401, {"error": "AuthenticationRequired", "message": "Invalid identifier or password"})
            return answer(200, {"did": DAVE, "handle": "dave.bsky.social", "accessJwt": self.new_token("access"),
                                "refreshJwt": self.new_token("refresh")})
        if name == "com.atproto.server.refreshSession":
            assert bearer == self.refresh_token
            return answer(200, {"did": DAVE, "handle": "dave.bsky.social", "accessJwt": self.new_token("access"),
                                "refreshJwt": self.new_token("refresh")})
        if name == "com.atproto.server.deleteSession":
            return answer(200, {})
        if bearer != self.access_token or jwt_exp(bearer) < time.time():
            return answer(400, {"error": "ExpiredToken", "message": "Token has expired"})
        if name == "app.bsky.feed.getFeed":
            assert headers["atproto-proxy"] == "did:web:api.bsky.app#bsky_appview"
            return answer(200, {"feed": self.feed_posts})
        if name == "app.bsky.feed.getPosts":
            assert headers["atproto-proxy"] == "did:web:api.bsky.app#bsky_appview"
            uri = u.params["uris"]
            viewer = {kind: f"at://{DAVE}/app.bsky.feed.{kind}/{rkey}" for rkey, r in self.records.items()
                      for kind in ("like", "repost")
                      if r["collection"] == f"app.bsky.feed.{kind}" and r["record"]["subject"]["uri"] == uri}
            return answer(200, {"posts": [{"uri": uri, "viewer": viewer}]})
        if name == "app.bsky.feed.getTimeline":
            assert headers["atproto-proxy"] == "did:web:api.bsky.app#bsky_appview"
            return answer(200, {"feed": self.timeline})
        if name == "app.bsky.notification.listNotifications":
            assert headers["atproto-proxy"] == "did:web:api.bsky.app#bsky_appview"
            return answer(200, {"notifications": self.notifications})
        if name == "app.bsky.notification.updateSeen":
            self.seen_at = body["seenAt"]
            for n in self.notifications:
                n["isRead"] = True
            return answer(200, {})
        if name == "com.atproto.repo.uploadBlob":
            self.uploads.append((headers["Content-Type"], content))
            return answer(200, {"blob": {"$type": "blob", "ref": {"$link": "bafyblob"}, "mimeType": headers["Content-Type"],
                                         "size": len(content)}})
        if name == "com.atproto.repo.createRecord":
            assert body["repo"] == DAVE
            rkey = f"r{len(self.records) + 1}"
            self.records[rkey] = body
            return answer(200, {"uri": f"at://{DAVE}/{body['collection']}/{rkey}", "cid": "c" + rkey})
        if name == "com.atproto.repo.deleteRecord":
            assert body["repo"] == DAVE
            del self.records[body["rkey"]]
            return answer(200, {})
        raise AssertionError(f"unexpected {name}")

    def new_token(self, kind, lasts=7200):
        payload = base64.urlsafe_b64encode(json.dumps({"exp": time.time() + lasts, "n": len(self.sent)}).encode())
        token = f"h.{payload.decode().rstrip('=')}.s"
        setattr(self, f"{kind}_token", token)
        return token


@pytest.fixture
def bsky(bouncer):
    fake = FakeBluesky()
    bouncer.bluesky_adapter = BlueskyAdapter(fake)
    return fake


def one(b, sql, *args):
    with b.db.connect() as conn:
        return conn.execute(sql, args).fetchone()


def logged_in(settings, bouncer):
    client = TestClient(create_app(settings, bouncer))
    client.post("/login", data={"password": "pw"})
    return client


def test_accounts_and_feeds_are_recognised():
    for text in ("@alice.bsky.social", "https://bsky.app/profile/alice.bsky.social", "bsky.app/profile/Alice.bsky.social/"):
        assert parse_community_ref(text).name == "alice.bsky.social"
    ref = parse_community_ref("https://bsky.app/profile/did:plc:feeder/feed/cats")
    assert (ref.domain, ref.name) == (BSKY_DOMAIN, "did:plc:feeder/feed/cats")
    assert parse_community_ref("at://did:plc:feeder/app.bsky.feed.generator/cats").name == ref.name
    assert parse_community_ref("!math@lemmy.ml").domain == "lemmy.ml"  # the rest as before
    with pytest.raises(ValueError):
        parse_community_ref("https://bsky.app/profile/alice.bsky.social/post/3kabc")
    t = parse_thread_url("https://bsky.app/profile/alice.bsky.social/post/p2")
    assert (t.domain, t.local_id) == (BSKY_DOMAIN, "alice.bsky.social/p2")


def test_text_keeps_its_links_mentions_and_tags():
    text = "Café *news*: see example.com/x… by @bob.bsky.social #cats"
    b = text.encode()

    def span(part):
        start = b.index(part.encode())
        return {"byteStart": start, "byteEnd": start + len(part.encode())}

    facets = [{"index": span("example.com/x…"), "features": [{"$type": "app.bsky.richtext.facet#link",
                                                              "uri": "https://example.com/x(1)"}]},
              {"index": span("@bob.bsky.social"), "features": [{"$type": "app.bsky.richtext.facet#mention",
                                                                "did": "did:plc:bob"}]},
              {"index": span("#cats"), "features": [{"$type": "app.bsky.richtext.facet#tag", "tag": "cats"}]}]
    assert rich_markdown(text, facets) == (
        "Café \\*news\\*: see [example.com/x…](https://example.com/x%281%29) by "
        "[@bob.bsky.social](https://bsky.app/profile/did:plc:bob) #cats".replace(
            "#cats", "[#cats](https://bsky.app/hashtag/cats)"))
    assert rich_markdown("one\ntwo\n\n- three") == "one\\\ntwo\n\n\\- three"


def test_following_an_account_captures_its_posts(settings, bouncer, bsky):
    cid = bouncer.follow_community("@alice.bsky.social", backfill=True)
    c = one(bouncer, "SELECT * FROM communities WHERE id=?", cid)
    assert (c["canonical_ap_id"], c["name"], c["title"]) == ("https://bsky.app/profile/did:plc:alice",
                                                             "alice.bsky.social", "Alice")
    f = one(bouncer, "SELECT * FROM community_follows WHERE community_id=?", cid)
    assert (f["source_domain"], f["source_ref"], f["polling"], f["poll_interval_minutes"]) == (
        BSKY_DOMAIN, ALICE, 1, 30)
    assert bouncer.poll_follow(cid) == 2  # not the repost
    with bouncer.db.connect() as conn:
        rows = conn.execute("SELECT o.canonical_ap_id, r.title, r.body, o.upvotes, o.thumbnail_url, t.next_check_at "
                            "FROM archived_threads t JOIN objects o ON o.id=t.root_object_id "
                            "JOIN revisions r ON r.object_id=o.id ORDER BY o.created_at").fetchall()
    assert [r["canonical_ap_id"] for r in rows] == ["https://bsky.app/profile/did:plc:alice/post/p1",
                                                    "https://bsky.app/profile/did:plc:alice/post/p2"]
    assert (rows[1]["title"], rows[1]["body"], rows[1]["upvotes"]) == (
        "Second post, with a picture", "Second post, with a picture", 7)
    assert rows[1]["thumbnail_url"] == "https://cdn.bsky.app/f/1.jpg"
    assert rows[1]["next_check_at"] is None  # likes come with each check instead

    bsky.author_feed[0]["post"]["likeCount"] = 9
    bouncer.poll_follow(cid)
    assert one(bouncer, "SELECT upvotes FROM objects WHERE canonical_ap_id LIKE '%/p2'")[0] == 9

    page = logged_in(settings, bouncer).get("/").text
    assert "Second post, with a picture" in page and "alice.bsky.social" in page
    assert "@bsky.app" not in page  # a handle is shown as it is
    assert "Add an account to vote" not in page  # likes are only shown


def test_opening_a_post_reads_its_replies(settings, bouncer, bsky):
    cid = bouncer.follow_community("https://bsky.app/profile/alice.bsky.social", backfill=True)
    bouncer.poll_follow(cid)
    uri = f"at://{ALICE}/app.bsky.feed.post/p2"
    bob = post_view("c1", "Nice **cat**", did="did:plc:bob", handle="bob.bsky.social", likes=2, replies=1)
    alice = post_view("c2", "Thanks!", embed={
        "$type": "app.bsky.embed.images#view", "images": [{"fullsize": "https://cdn.bsky.app/f/2.jpg", "alt": ""}]})
    bsky.threads[uri] = thread(bsky.author_feed[0]["post"], thread(bob, thread(alice)))
    tid = one(bouncer, "SELECT t.id FROM archived_threads t JOIN objects o ON o.id=t.root_object_id "
                       "WHERE o.canonical_ap_id LIKE '%/p2'")[0]
    bouncer.open_threads([tid])
    with bouncer.db.connect() as conn:
        comments = conn.execute("SELECT o.canonical_ap_id, r.body, p.canonical_ap_id AS parent, o.upvotes "
                                "FROM objects o JOIN revisions r ON r.object_id=o.id "
                                "LEFT JOIN objects p ON p.id=o.parent_id "
                                "WHERE o.object_type='comment' ORDER BY o.id").fetchall()
    assert [(c["body"], c["upvotes"]) for c in comments] == [
        ("Nice \\*\\*cat\\*\\*", 2), ("Thanks!\n\n![](https://cdn.bsky.app/f/2.jpg)", 3)]
    assert comments[1]["parent"] == comments[0]["canonical_ap_id"]
    page = logged_in(settings, bouncer).get(f"/t/{tid}").text
    assert "Nice" in page and "bob.bsky.social" in page and "Comment as" not in page
    assert 'href="https://bsky.app/profile/did:plc:alice/post/p2"' in page  # the original

    # Deleted on Bluesky: the reply goes, and so does the post when it's gone too.
    bsky.threads[uri] = thread(bsky.author_feed[0]["post"])
    bouncer.sync_thread(tid, force=True)
    assert one(bouncer, "SELECT COUNT(*) FROM objects WHERE object_type='comment' AND cur_deleted=0 "
                        "AND cur_missing=0")[0] == 0


def test_following_a_feed(settings, bouncer, bsky):
    cid = bouncer.follow_community("https://bsky.app/profile/feeder.bsky.social/feed/cats", backfill=True)
    c = one(bouncer, "SELECT * FROM communities WHERE id=?", cid)
    assert (c["canonical_ap_id"], c["name"]) == ("https://bsky.app/profile/did:plc:feeder/feed/cats", "Cat Pics")
    assert one(bouncer, "SELECT source_ref FROM community_follows")[0] == f"{FEEDER}/feed/cats"
    assert bouncer.poll_follow(cid) == 1
    assert one(bouncer, "SELECT community_id FROM objects WHERE canonical_ap_id LIKE '%/f1'")[0] == cid
    assert "Cat Pics" in logged_in(settings, bouncer).get("/").text


def test_a_feed_omits_replies_and_names_who_reposted(settings, bouncer, bsky):
    root = f"at://{ALICE}/app.bsky.feed.post/root"
    bsky.feed_posts = [
        {"post": post_view("f1", "An original post", did="did:plc:bob", handle="bob.bsky.social"),
         "reason": {"$type": "app.bsky.feed.defs#reasonRepost",
                    "by": {"did": "did:plc:carol", "handle": "carol.bsky.social"}}},
        {"post": post_view("reply", "This belongs in the thread", did="did:plc:bob",
                           handle="bob.bsky.social", reply_to=root)},
    ]
    cid = bouncer.follow_community("https://bsky.app/profile/feeder.bsky.social/feed/cats", backfill=True)
    assert bouncer.poll_follow(cid) == 1
    with bouncer.db.connect() as conn:
        saved = conn.execute("SELECT r.metadata_json FROM revisions r JOIN objects o ON o.id=r.object_id "
                             "WHERE o.object_type='post'").fetchone()
    assert json.loads(saved["metadata_json"])["reposted_by"] == {
        "handle": "carol.bsky.social", "url": "https://bsky.app/profile/did:plc:carol"}

    client = logged_in(settings, bouncer)
    for view in ("list", "pictures", "tiles"):
        page = client.get(f"/?view={view}").text
        assert "This belongs in the thread" not in page
        assert "Reposted by" in page and "@carol.bsky.social" in page
        assert "@bob.bsky.social" in page or view == "tiles"


def test_a_feed_only_shown_signed_in_says_so(bouncer, bsky):
    bsky.feed_needs_login = True
    with pytest.raises(RemoteNotFound, match="signed in"):
        bouncer.follow_community("https://bsky.app/profile/did:plc:feeder/feed/cats")


def test_a_post_can_be_kept_by_its_link(bouncer, bsky):
    uri = f"at://{ALICE}/app.bsky.feed.post/p1"
    bsky.threads[uri] = thread(bsky.author_feed[2]["post"])
    tid = bouncer.ingest_url("https://bsky.app/profile/alice.bsky.social/post/p1")
    t = one(bouncer, "SELECT t.retention, c.canonical_ap_id FROM archived_threads t "
                     "JOIN communities c ON c.id=t.community_id WHERE t.id=?", tid)
    assert (t["retention"], t["canonical_ap_id"]) == ("manual", "https://bsky.app/profile/did:plc:alice")


# -- signed in as your own account -------------------------------------------------------------

def signed_in(settings, bouncer, bsky):
    client = logged_in(settings, bouncer)
    r = client.post("/accounts/bluesky", data={"identifier": "@dave.bsky.social", "app_password": "abcd-efgh-ijkl-mnop"})
    assert r.status_code == 200 and "Signed in to Bluesky as @dave.bsky.social" in r.text
    return client


def alice_followed(bouncer):
    cid = bouncer.follow_community("@alice.bsky.social", backfill=True)
    bouncer.poll_follow(cid)
    return cid


def post_oid(bouncer, rkey):
    return one(bouncer, "SELECT id FROM objects WHERE canonical_ap_id LIKE ?", f"%/post/{rkey}")[0]


def test_signing_in_with_an_app_password(settings, bouncer, bsky):
    client = logged_in(settings, bouncer)
    r = client.post("/accounts/bluesky", data={"identifier": "dave.bsky.social", "app_password": "wrong"})
    assert "Bluesky didn&#39;t accept that" in r.text or "Bluesky didn't accept that" in r.text
    assert one(bouncer, "SELECT COUNT(*) FROM accounts")[0] == 0
    client = signed_in(settings, bouncer, bsky)
    a = one(bouncer, "SELECT * FROM accounts")
    assert (a["domain"], a["username"], a["actor_ap_id"], a["is_default"]) == (
        BSKY_DOMAIN, "dave.bsky.social", f"https://bsky.app/profile/{DAVE}", 0)
    assert b"abcd-efgh" not in (a["token_enc"] or "").encode() and bsky.access_token not in a["token_enc"]
    assert bsky.sent[0][1] == f"https://plc.directory/{DAVE}"  # found its PDS, and signed in there
    page = client.get("/accounts").text
    assert "Signed in as <strong>@dave.bsky.social</strong>" in page
    assert "Post, vote and reply as" not in client.get("/").text  # not in the header switcher


def test_liking_and_unliking(settings, bouncer, bsky):
    alice_followed(bouncer)
    client = signed_in(settings, bouncer, bsky)
    oid = post_oid(bouncer, "p2")
    feed = client.get("/").text
    assert 'title="Like as @dave.bsky.social"' in feed and "downvote" not in feed.split(f'id="a{oid}"')[1][:2000]
    client.post(f"/o/{oid}/vote", data={"score": "1"})
    (like,) = bsky.records.values()
    assert like["collection"] == "app.bsky.feed.like"
    assert like["record"]["subject"] == {"uri": f"at://{ALICE}/app.bsky.feed.post/p2", "cid": "cp2"}
    assert one(bouncer, "SELECT upvotes FROM objects WHERE id=?", oid)[0] == 8
    assert 'title="Remove like as @dave.bsky.social"' in client.get("/").text
    client.post(f"/o/{oid}/vote", data={"score": "0"})
    assert bsky.records == {} and one(bouncer, "SELECT upvotes FROM objects WHERE id=?", oid)[0] == 7
    r = client.post(f"/o/{oid}/vote", data={"score": "-1"})
    assert "likes, not downvotes" in r.text


def test_replying(settings, bouncer, bsky):
    alice_followed(bouncer)
    uri = f"at://{ALICE}/app.bsky.feed.post/p2"
    bsky.threads[uri] = thread(bsky.author_feed[0]["post"], thread(post_view("c1", "First!", did="did:plc:bob",
                                                                             handle="bob.bsky.social")))
    client = signed_in(settings, bouncer, bsky)
    tid = one(bouncer, "SELECT thread_id FROM objects WHERE id=?", post_oid(bouncer, "p2"))[0]
    bouncer.open_threads([tid])
    page = client.get(f"/t/{tid}").text
    assert "Comment as @dave.bsky.social" in page and "Edit</a>" not in page
    client.post(f"/t/{tid}/reply", data={"body": "Lovely, @bob.bsky.social #cats"})
    reply = next(r for r in bsky.records.values() if r["collection"] == "app.bsky.feed.post")["record"]
    assert reply["reply"] == {"root": {"uri": uri, "cid": "cp2"}, "parent": {"uri": uri, "cid": "cp2"}}
    assert [f["features"][0]["$type"].rsplit("#", 1)[1] for f in reply["facets"]] == ["mention", "tag"]
    c1 = one(bouncer, "SELECT id FROM objects WHERE canonical_ap_id LIKE '%/post/c1'")[0]
    client.post(f"/t/{tid}/reply", data={"body": "And to you", "parent_id": str(c1)})
    second = [r["record"] for r in bsky.records.values()][-1]
    assert second["reply"]["parent"]["uri"] == f"at://did:plc:bob/app.bsky.feed.post/c1"
    mine = one(bouncer, "SELECT o.id, r.body FROM objects o JOIN revisions r ON r.object_id=o.id "
                        "WHERE o.canonical_ap_id=?", f"https://bsky.app/profile/{DAVE}/post/r1")
    assert mine["body"] == ("Lovely, [@bob.bsky.social](https://bsky.app/profile/did:plc:bob) "
                            "[#cats](https://bsky.app/hashtag/cats)")
    r = client.post(f"/t/{tid}/reply", data={"body": "x" * 301})
    assert "at most 300 characters" in r.text
    client.post(f"/o/{mine['id']}/delete", data={})
    assert "r1" not in bsky.records


def test_the_session_is_renewed_before_it_runs_out(settings, bouncer, bsky, monkeypatch):
    alice_followed(bouncer)
    client = signed_in(settings, bouncer, bsky)
    stored = one(bouncer, "SELECT token_enc FROM accounts")[0]
    monkeypatch.setattr(bluesky, "REFRESH_BEFORE", 10_000)  # longer than it lasts: it's due now
    old = bsky.access_token
    client.post(f"/o/{post_oid(bouncer, 'p2')}/vote", data={"score": "1"})
    assert any(s[1].endswith("refreshSession") for s in bsky.sent) and bsky.access_token != old
    assert len(bsky.records) == 1 and one(bouncer, "SELECT token_enc FROM accounts")[0] != stored


def test_a_feed_only_shown_signed_in_is_read_as_you(settings, bouncer, bsky):
    bsky.feed_needs_login = True
    signed_in(settings, bouncer, bsky)
    cid = bouncer.follow_community("https://bsky.app/profile/did:plc:feeder/feed/cats", backfill=True)
    assert bouncer.poll_follow(cid) == 1
    assert any(s[1] == f"{PDS}/xrpc/app.bsky.feed.getFeed" for s in bsky.sent)


def test_posting_on_your_account(settings, bouncer, bsky):
    client = signed_in(settings, bouncer, bsky)
    r = client.get("/bluesky/post")
    assert "Post as @dave.bsky.social" in r.text and "up to 300 characters" in r.text
    cid = int(str(r.url).split("/c/")[1].split("/")[0])
    r = client.post(f"/c/{cid}/submit", data={"title": "Hello from ThreadBNC", "url": "https://example.com/a"})
    (made,) = bsky.records.values()
    assert made["record"]["text"] == "Hello from ThreadBNC"
    assert made["record"]["embed"]["external"]["uri"] == "https://example.com/a"
    assert "Hello from ThreadBNC" in r.text  # its thread, kept
    t = one(bouncer, "SELECT t.retention, t.community_id FROM archived_threads t JOIN objects o "
                     "ON o.id=t.root_object_id WHERE o.canonical_ap_id=?", f"https://bsky.app/profile/{DAVE}/post/r1")
    assert (t["retention"], t["community_id"]) == ("manual", cid)
    alice = alice_followed(bouncer)
    r = client.post(f"/c/{alice}/submit", data={"title": "Not mine"})
    assert "only post to your own account" in r.text


def test_facets_for_what_you_write():
    text = "See https://example.com/@x and @bob.bsky.social, @nobody.example #cats #2"
    got = facets_for(text, lambda h: "did:plc:bob" if h == "bob.bsky.social" else None)
    b = text.encode()
    assert [(b[f["index"]["byteStart"]:f["index"]["byteEnd"]].decode(), f["features"][0]["$type"].rsplit("#")[1])
            for f in got] == [("https://example.com/@x", "link"), ("@bob.bsky.social", "mention"), ("#cats", "tag")]


def notification(rkey, reason, text, reply_to=None, read=False):
    record = {"$type": "app.bsky.feed.post", "text": text, "createdAt": "2026-09-24T09:00:00.000Z"}
    if reply_to:
        record["reply"] = {"root": {"uri": reply_to, "cid": "croot"}, "parent": {"uri": reply_to, "cid": "croot"}}
    return {"uri": f"at://did:plc:bob/app.bsky.feed.post/{rkey}", "cid": "c" + rkey, "reason": reason,
            "author": {"did": "did:plc:bob", "handle": "bob.bsky.social"}, "record": record, "isRead": read,
            "indexedAt": "2026-09-24T09:00:01.000Z"}


def test_notifications_arrive_in_the_inbox(settings, bouncer, bsky):
    client = signed_in(settings, bouncer, bsky)
    mine = f"at://{DAVE}/app.bsky.feed.post/mine"
    bsky.notifications = [notification("n1", "reply", "Nice one", reply_to=mine),
                          notification("n2", "mention", "Hey @dave.bsky.social"),
                          notification("n3", "quote", "Look at this"),
                          notification("n4", "like", ""), notification("n5", "follow", "")]
    client.post("/inbox/check")
    page = client.get("/inbox").text
    assert "@bob.bsky.social" in page and "replied on" in page and "mentioned you on" in page
    assert "quoted your post in" in page and "Nice one" in page and page.count('class="card inbox-item') == 3
    rows = {r["remote_id"].rsplit("/", 1)[1]: r for r in _rows(bouncer, "SELECT * FROM inbox_items")}
    assert rows["n1"]["post_local_id"] == f"{mine} croot" and rows["n1"]["object_type"] == "comment"
    assert rows["n2"]["post_local_id"] == "at://did:plc:bob/app.bsky.feed.post/n2 cn2"

    # Answering one replies under it; one marked read stays read though Bluesky still says unread.
    client.post(f"/inbox/{rows['n1']['id']}/reply", data={"body": "Thanks!"})
    reply = [r["record"] for r in bsky.records.values()][-1]
    assert reply["reply"] == {"root": {"uri": mine, "cid": "croot"},
                              "parent": {"uri": "at://did:plc:bob/app.bsky.feed.post/n1", "cid": "cn1"}}
    client.post(f"/inbox/{rows['n2']['id']}/read", data={"read": "1"})
    client.post("/inbox/check")
    unread = {r["remote_id"].rsplit("/", 1)[1] for r in _rows(bouncer, "SELECT * FROM inbox_items WHERE unread=1")}
    assert unread == {"n3"}
    client.post("/inbox/read-all", data={"confirmed": "1"})
    assert bsky.seen_at and all(n["isRead"] for n in bsky.notifications)


def _rows(bouncer, sql):
    with bouncer.db.connect() as conn:
        return conn.execute(sql).fetchall()


def test_reposting_and_quoting(settings, bouncer, bsky):
    alice_followed(bouncer)
    client = signed_in(settings, bouncer, bsky)
    oid = post_oid(bouncer, "p2")
    tid = one(bouncer, "SELECT thread_id FROM objects WHERE id=?", oid)[0]
    assert "Repost</button>" in client.get(f"/t/{tid}").text
    client.post(f"/o/{oid}/bluesky-repost", data={"on": "1"})
    (repost,) = bsky.records.values()
    assert repost["collection"] == "app.bsky.feed.repost"
    assert repost["record"]["subject"] == {"uri": f"at://{ALICE}/app.bsky.feed.post/p2", "cid": "cp2"}
    assert "Reposted</button>" in client.get(f"/t/{tid}").text
    client.post(f"/o/{oid}/bluesky-repost", data={"on": "0"})
    assert bsky.records == {} and "Repost</button>" in client.get(f"/t/{tid}").text

    r = client.post(f"/o/{oid}/bluesky-quote", data={"body": "So good"})
    (quote,) = bsky.records.values()
    assert quote["record"]["text"] == "So good"
    assert quote["record"]["embed"] == {"$type": "app.bsky.embed.record",
                                        "record": {"uri": f"at://{ALICE}/app.bsky.feed.post/p2", "cid": "cp2"}}
    assert "So good" in r.text and "quoted post" in r.text  # its thread, the quote shown under it


def test_a_link_card_shows_the_page(settings, bouncer, bsky, monkeypatch):
    client = signed_in(settings, bouncer, bsky)
    monkeypatch.setattr(bouncer.articles, "link_card", lambda url: {
        "title": "An article", "description": "What it says", "image": (b"\x89PNG...", "image/png")})
    cid = int(str(client.get("/bluesky/post").url).split("/c/")[1].split("/")[0])
    client.post(f"/c/{cid}/submit", data={"title": "Read this", "url": "https://example.com/a"})
    (made,) = bsky.records.values()
    external = made["record"]["embed"]["external"]
    assert (external["title"], external["description"], external["thumb"]["ref"]["$link"]) == (
        "An article", "What it says", "bafyblob")
    assert bsky.uploads == [("image/png", b"\x89PNG...")]
    assert one(bouncer, "SELECT thumbnail_url FROM objects WHERE canonical_ap_id LIKE '%/r1'")[0] == \
        f"https://cdn.bsky.app/img/feed_thumbnail/plain/{DAVE}/bafyblob@jpeg"


def test_following_your_timeline(settings, bouncer, bsky):
    client = logged_in(settings, bouncer)
    r = client.post("/bluesky/timeline")
    assert "Log in to Bluesky first" in r.text
    client = signed_in(settings, bouncer, bsky)
    bsky.timeline = [{"post": post_view("t1", "From someone I follow", did="did:plc:bob", handle="bob.bsky.social")},
                     {"post": post_view("t2", "Reposted", did="did:plc:bob", handle="bob.bsky.social"),
                      "reason": {"$type": "app.bsky.feed.defs#reasonRepost"}}]
    r = client.post("/bluesky/timeline")
    c = one(bouncer, "SELECT * FROM communities WHERE canonical_ap_id=?", f"https://bsky.app/profile/{DAVE}/timeline")
    assert c["name"] == "Following" and f"/c/{c['id']}" in str(r.url)
    assert "Following · Bluesky timeline" in r.text.replace('<span class="muted"> · ', " · ").replace("</span>", "")
    assert bouncer.poll_follow(c["id"]) == 1
    with pytest.raises(RemoteNotFound, match="own"):
        bouncer.follow_community("https://bsky.app/profile/did:plc:alice/timeline")
