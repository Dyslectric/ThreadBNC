"""Bluesky: accounts and custom feeds followed like communities, through its public API."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from threadbnc.adapters import BSKY_DOMAIN, RemoteAuthError, RemoteNotFound, parse_community_ref, parse_thread_url
from threadbnc.adapters.bluesky import BlueskyAdapter, rich_markdown
from threadbnc.web import create_app

ALICE = "did:plc:alice"
FEEDER = "did:plc:feeder"


def post_view(rkey, text, did=ALICE, handle="alice.bsky.social", likes=3, replies=0, embed=None, facets=None,
              created="2026-09-23T10:00:00.000Z", labels=()):
    record = {"$type": "app.bsky.feed.post", "text": text, "createdAt": created}
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
            raise RemoteNotFound("Unable to resolve handle")
        if method == "app.bsky.actor.getProfile":
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
            return {"posts": [p for p in everything if p["uri"] in params["uris"] and p["uri"] not in self.deleted]}
        if method == "app.bsky.feed.getPostThread":
            if params["uri"] in self.deleted:
                return {"thread": {"$type": "app.bsky.feed.defs#notFoundPost", "uri": params["uri"], "notFound": True}}
            return {"thread": self.threads[params["uri"]]}
        raise AssertionError(f"unexpected {method}")


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
