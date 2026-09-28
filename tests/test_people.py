"""Following people on Mastodon with ThreadBNC's own ActivityPub actor."""

from __future__ import annotations

import json
from dataclasses import replace

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from threadbnc.adapters import (RSS_DOMAIN, TAG_DOMAIN, BSKY_DOMAIN, CommunityRef, HttpClient, UnsupportedSoftware,
                                parse_community_ref)
from threadbnc.bouncer import Bouncer
from threadbnc.db import open_database
from threadbnc.people import PeopleFollows
from threadbnc.web import create_app

from .conftest import FakeAdapter
from .test_tags import ME, PUBLIC, Fediverse, note, one, run_jobs, verify_ours

ALICE = "https://masto.test/users/alice"
INBOX = ALICE + "/inbox"
FOLLOWERS = ALICE + "/followers"


def status(n: int, **changes) -> dict:
    """One of Alice's posts."""
    return note(**{"id": f"{ALICE}/statuses/{n}", "url": f"https://masto.test/@alice/{n}", "tag": [],
                   "content": f"<p>Post number {n}</p>", "attachment": [], **changes})


def create(obj: dict, n: int | None = None) -> dict:
    return {"id": f"{obj['id']}/activity" if n is None else f"{ALICE}/activity/{n}", "type": "Create",
            "actor": ALICE, "to": obj.get("to"), "cc": obj.get("cc"), "object": obj}


class Mastodon(Fediverse):
    """The fake fediverse, with Alice on masto.test: her WebFinger, actor and outbox."""

    def __init__(self) -> None:
        super().__init__()
        self.alice_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = self.alice_key.public_key().public_bytes(serialization.Encoding.PEM,
                                                       serialization.PublicFormat.SubjectPublicKeyInfo).decode()
        self.objects[ALICE] = (200, {
            "id": ALICE, "type": "Person", "preferredUsername": "alice", "name": "Alice Liddell",
            "summary": "<p>Rabbit holes &amp; racks.</p>", "inbox": INBOX, "outbox": ALICE + "/outbox",
            "followers": FOLLOWERS, "url": "https://masto.test/@alice",
            "publicKey": {"id": ALICE + "#main-key", "owner": ALICE, "publicKeyPem": pem}})
        self.objects[ALICE + "/outbox"] = (200, {"id": ALICE + "/outbox", "type": "OrderedCollection",
                                                 "first": ALICE + "/outbox?page=true"})
        self.outbox = [
            create(status(1)),
            create(status(2, inReplyTo="https://other.test/users/bob/statuses/9")),  # a reply: not hers to list
            create(status(3, to=[FOLLOWERS], cc=[])),  # followers only
            {"id": ALICE + "/statuses/4/activity", "type": "Announce", "actor": ALICE, "to": [PUBLIC],
             "object": "https://other.test/users/bob/statuses/10"},  # a boost
            create(status(5, summary="Spoilers", sensitive=True)),
        ]
        self.objects[ALICE + "/outbox?page=true"] = (200, {"type": "OrderedCollectionPage",
                                                           "orderedItems": self.outbox})
        self.webfinger = {"acct:alice@masto.test": {
            "subject": "acct:alice@masto.test",
            "links": [{"rel": "http://webfinger.net/rel/profile-page", "type": "text/html",
                       "href": "https://masto.test/@alice"},
                      {"rel": "self", "type": "application/activity+json", "href": ALICE}]}}

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/.well-known/webfinger":
            self.gets.append(request)
            jrd = self.webfinger.get(request.url.params.get("resource", ""))
            return httpx.Response(200, json=jrd) if jrd else httpx.Response(404, json={"error": "not found"})
        return super().handler(request)

    def from_alice(self, activity: dict) -> tuple[bytes, dict[str, str]]:
        return self.signed(activity, key=self.alice_key, key_id=ALICE + "#main-key")


@pytest.fixture
def fedi():
    return Mastodon()


@pytest.fixture
def tagged(settings, server, fedi):
    settings = replace(settings, actor_domain=ME, tag_relay="https://relay.test/tag/{tag}")
    http = HttpClient("test", 5, 0)
    http._client = httpx.Client(transport=httpx.MockTransport(fedi.handler), follow_redirects=True)
    b = Bouncer(open_database(settings), settings, http=http, adapter_factory=lambda d: FakeAdapter(d, server))
    return b, settings


@pytest.fixture
def people(tagged):
    b, _ = tagged
    return PeopleFollows(b, b.actor)


@pytest.fixture
def client(tagged):
    b, settings = tagged
    return TestClient(create_app(settings, b), base_url=f"https://{ME}")


def deliver(client, fedi, activity):
    body, headers = fedi.from_alice(activity)
    return client.post("/threadbnc/inbox", content=body, headers=headers)


def saved(b) -> dict[str, dict]:
    with b.db.connect() as conn:
        return {r["canonical_ap_id"]: dict(r) for r in conn.execute(
            "SELECT o.canonical_ap_id, t.community_id, t.retention, t.source_domain, t.source_local_id "
            "FROM archived_threads t JOIN objects o ON o.id=t.root_object_id")}


def accept(client, fedi, b) -> None:
    follow_id = next(sent for url, sent, _ in fedi.posted if url == INBOX and sent["type"] == "Follow")["id"]
    r = deliver(client, fedi, {"id": "https://masto.test/accept/1", "type": "Accept", "actor": ALICE,
                               "object": {"id": follow_id, "type": "Follow", "actor": b.actor.id, "object": ALICE}})
    assert r.status_code == 202


# --- references ---------------------------------------------------------------------

def test_people_are_named_by_handle_or_profile():
    assert parse_community_ref("@alice@Masto.test") == CommunityRef(TAG_DOMAIN, "@alice@masto.test", TAG_DOMAIN)
    assert parse_community_ref("https://masto.test/@alice") == CommunityRef(TAG_DOMAIN, "https://masto.test/@alice",
                                                                            TAG_DOMAIN)
    assert parse_community_ref("https://masto.test/users/alice/").name == ALICE
    # Everything else is as it was.
    assert parse_community_ref("technology@lemmy.test") == CommunityRef("lemmy.test", "technology", "lemmy.test")
    assert parse_community_ref("@alice.bsky.social").domain == BSKY_DOMAIN
    assert parse_community_ref("https://www.youtube.com/@channel").domain == RSS_DOMAIN
    assert parse_community_ref("https://lemmy.test/c/technology").domain == "lemmy.test"


# --- following ----------------------------------------------------------------------

def test_following_someone_sends_them_a_follow(tagged, people, fedi):
    b, _ = tagged
    cid = b.follow_community("@alice@masto.test", None, 30, False)
    url, sent, request = fedi.posted[-1]
    assert url == INBOX and sent["type"] == "Follow" and sent["object"] == ALICE and sent["actor"] == b.actor.id
    verify_ours(b.actor, request)
    c = one(b, "SELECT * FROM communities WHERE id=?", cid)
    assert (c["canonical_ap_id"], c["name"], c["title"]) == (ALICE, "alice", "Alice Liddell")
    f = one(b, "SELECT * FROM community_follows WHERE community_id=?", cid)
    assert (f["source_domain"], f["source_ref"], f["polling"]) == (TAG_DOMAIN, ALICE, 0)
    assert (f["push_state"], f["push_actor"], f["push_follow_id"]) == ("pending", ALICE, sent["id"])
    # Found once, not looked up again to send the Follow; and nothing read without asking to start with it.
    assert [str(g.url).split("?")[0] for g in fedi.gets] == ["https://masto.test/.well-known/webfinger", ALICE]
    run_jobs(b)
    assert saved(b) == {}


def test_following_starts_with_their_latest_public_posts(tagged, people, fedi):
    b, _ = tagged
    cid = b.follow_community("https://masto.test/@alice", None, 30, True)  # the profile's address works too
    run_jobs(b)
    posts = saved(b)
    # Not the reply, the followers-only post, or the boost.
    assert set(posts) == {f"{ALICE}/statuses/1", f"{ALICE}/statuses/5"}
    first = posts[f"{ALICE}/statuses/1"]
    assert (first["community_id"], first["retention"], first["source_domain"]) == (cid, "auto", TAG_DOMAIN)
    assert first["source_local_id"] == f"{ALICE} {ALICE}/statuses/1"
    meta = json.loads(one(b, "SELECT r.metadata_json FROM revisions r JOIN objects o ON o.id=r.object_id "
                             "WHERE o.canonical_ap_id=?", f"{ALICE}/statuses/5")["metadata_json"])
    assert meta["content_warning"] == "Spoilers"
    # Only once: following her again doesn't read her outbox again.
    b.unfollow(cid)
    gets = len(fedi.gets)
    b.follow_community("@alice@masto.test", None, 30, True)
    run_jobs(b)
    assert not any("outbox" in str(g.url) for g in fedi.gets[gets:])


def test_a_follow_that_cant_be_sent_is_sent_again(tagged, people, fedi):
    b, _ = tagged
    doc = fedi.objects[ALICE]
    fedi.objects[ALICE] = (200, {**doc[1], "inbox": None})
    with pytest.raises(Exception):  # no inbox: not someone who can be followed
        b.follow_community("@alice@masto.test", None, 30, False)
    fedi.objects[ALICE] = doc
    cid = b.follow_community("@alice@masto.test", None, 30, False)
    with b.db.transaction() as conn:  # as if sending it had failed
        conn.execute("UPDATE community_follows SET push_state=NULL, push_error='HTTP 502' WHERE community_id=?",
                     (cid,))
    sent = len(fedi.posted)
    people.housekeeping()
    assert len(fedi.posted) == sent + 1 and fedi.posted[-1][1]["type"] == "Follow"
    assert one(b, "SELECT push_state FROM community_follows WHERE community_id=?", cid)["push_state"] == "pending"
    people.housekeeping()  # not again so soon
    assert len(fedi.posted) == sent + 1


def test_unfollowing_undoes_the_follow(tagged, people, fedi):
    b, _ = tagged
    cid = b.follow_community("@alice@masto.test", None, 30, False)
    follow_id = fedi.posted[-1][1]["id"]
    b.unfollow(cid)
    url, sent, _ = fedi.posted[-1]
    assert url == INBOX and sent["type"] == "Undo" and sent["object"]["id"] == follow_id
    assert one(b, "SELECT push_actor FROM community_follows WHERE community_id=?", cid)["push_actor"] is None


def test_someone_on_mastodon_named_like_a_community(settings, server, fedi):
    """alice@masto.test, without the leading @: masto.test runs Mastodon, not Lemmy, so it's her."""
    settings = replace(settings, actor_domain=ME)
    http = HttpClient("test", 5, 0)
    http._client = httpx.Client(transport=httpx.MockTransport(fedi.handler), follow_redirects=True)

    def adapters(domain):
        if domain == "masto.test":
            raise UnsupportedSoftware("masto.test runs 'mastodon', which is not supported yet")
        return FakeAdapter(domain, server)

    b = Bouncer(open_database(settings), settings, http=http, adapter_factory=adapters)
    PeopleFollows(b, b.actor)
    cid = b.follow_community("alice@masto.test", None, 30, False)
    assert one(b, "SELECT canonical_ap_id FROM communities WHERE id=?", cid)["canonical_ap_id"] == ALICE
    with pytest.raises(UnsupportedSoftware, match="mastodon"):  # !name@server is only ever a community
        b.follow_community("!alice@masto.test", None, 30, False)


def test_without_an_actor_people_cant_be_followed(bouncer):
    with pytest.raises(Exception, match="THREADBNC_ACTOR_DOMAIN"):
        bouncer.follow_community("@alice@masto.test", None, 30, False)


# --- the inbox ----------------------------------------------------------------------

def test_their_posts_arrive_as_theyre_made(client, tagged, fedi):
    b, _ = tagged
    cid = b.follow_community("@alice@masto.test", None, 30, False)
    accept(client, fedi, b)
    f = one(b, "SELECT * FROM community_follows WHERE community_id=?", cid)
    assert f["push_state"] == "subscribed"
    gets = len(fedi.gets)
    post = status(20, content='<p>Look <a href="https://blog.test/x">here</a></p>')
    assert deliver(client, fedi, create(post)).status_code == 202
    for skipped in (status(21, inReplyTo=ALICE + "/statuses/20"), status(22, to=[FOLLOWERS], cc=[])):
        assert deliver(client, fedi, create(skipped)).status_code == 202
    assert deliver(client, fedi, {"id": ALICE + "/statuses/23/activity", "type": "Announce", "actor": ALICE,
                                  "to": [PUBLIC], "object": "https://other.test/users/bob/statuses/10"}).status_code == 202
    run_jobs(b)
    assert set(saved(b)) == {post["id"]}
    assert len(fedi.gets) == gets  # the post came with the delivery: nothing was fetched
    r = one(b, "SELECT r.* FROM revisions r JOIN objects o ON o.id=r.object_id WHERE o.canonical_ap_id=?", post["id"])
    assert r["url"] == "https://blog.test/x" and "Look" in r["body"]
    assert one(b, "SELECT last_push_at FROM community_follows WHERE community_id=?", cid)["last_push_at"]

    # An edit is kept as the post's new version.
    edited = {**post, "content": "<p>Look over there</p>", "updated": "2026-09-27T12:00:00Z"}
    assert deliver(client, fedi, {"id": post["id"] + "#updates/1", "type": "Update", "actor": ALICE,
                                  "to": [PUBLIC], "object": edited}).status_code == 202
    with b.db.connect() as conn:
        bodies = [r["body"] for r in conn.execute("SELECT r.body FROM revisions r JOIN objects o ON o.id=r.object_id "
                                                  "WHERE o.canonical_ap_id=? ORDER BY r.id", (post["id"],))]
    assert len(bodies) == 2 and "over there" in bodies[-1]

    # Her page: a timeline, with where her posts come from.
    auth = {"authorization": "Bearer tok"}
    page = client.get(f"/c/{cid}", headers=auth).text
    assert 'id="items" class="timeline"' in page and "From their server" in page and "@alice" in page
    assert "Look over there" in page


def test_posts_in_her_name_from_elsewhere_are_refused(client, tagged, fedi):
    b, _ = tagged
    b.follow_community("@alice@masto.test", None, 30, False)
    accept(client, fedi, b)
    forged = status(30, attributedTo="https://other.test/users/mallory")
    deliver(client, fedi, create(forged))
    elsewhere = status(31, id="https://other.test/users/alice/statuses/31")
    deliver(client, fedi, create(elsewhere))
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    body, headers = fedi.signed(create(status(32)), key=other, key_id=ALICE + "#main-key")
    assert client.post("/threadbnc/inbox", content=body, headers=headers).status_code == 401
    run_jobs(b)
    assert saved(b) == {}


def test_hashtag_relays_and_people_share_the_inbox(client, tagged, fedi):
    b, _ = tagged
    b.follow_community("@alice@masto.test", None, 30, False)
    b.follow_community("#SelfHosted", None, 30, False)
    tags, people = client.app.state.tags, client.app.state.people
    assert people.expects({"actor": ALICE}) and not tags.expects({"actor": ALICE})
    relay = "https://relay.test/tag/selfhosted"
    assert tags.expects({"actor": relay}) and not people.expects({"actor": relay})


def test_her_server_isnt_checked_in_the_background(client, tagged, fedi):
    b, _ = tagged
    cid = b.follow_community("@alice@masto.test", None, 30, False)
    accept(client, fedi, b)
    with b.db.transaction() as conn:
        conn.execute("UPDATE community_follows SET next_poll_at='2000-01-01T00:00:00.000000Z' WHERE community_id=?",
                     (cid,))
    gets = len(fedi.gets)
    b.tick()
    assert not any("outbox" in str(g.url) for g in fedi.gets[gets:])
    b.set_polling(cid, True)  # unless asked
    b.tick()
    assert any("outbox" in str(g.url) for g in fedi.gets[gets:])
