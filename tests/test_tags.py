"""Following hashtags through a relay, with ThreadBNC's own ActivityPub actor."""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import replace
from email.utils import formatdate
from urllib.parse import urlparse

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fastapi.testclient import TestClient

from threadbnc.actor import Actor, SignatureError, parse_signature
from threadbnc.adapters import TAG_DOMAIN, CommunityRef, HttpClient, parse_community_ref
from threadbnc.adapters.activitypub import shared_link, title_from
from threadbnc.bouncer import Bouncer
from threadbnc.db import open_database
from threadbnc.tags import TagRelays
from threadbnc.web import create_app

from .conftest import FakeAdapter

ME = "bnc.test"
RELAY = "https://relay.test/tag/selfhosted"
NOTE = "https://masto.test/users/alice/statuses/111"
PUBLIC = "https://www.w3.org/ns/activitystreams#Public"


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


class Fediverse:
    """The other servers: a tag relay and a Mastodon server, over a mock transport."""

    def __init__(self) -> None:
        self.relay_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.objects: dict[str, tuple[int, dict]] = {}
        self.api: dict[str, dict] = {}
        self.posted: list[tuple[str, dict, httpx.Request]] = []
        self.gets: list[httpx.Request] = []
        pem = self.relay_key.public_key().public_bytes(serialization.Encoding.PEM,
                                                       serialization.PublicFormat.SubjectPublicKeyInfo).decode()
        self.objects[RELAY] = (200, {"id": RELAY, "type": "Service", "inbox": RELAY,
                                     "publicKey": {"id": RELAY + "#key", "owner": RELAY, "publicKeyPem": pem}})
        self.objects[NOTE] = (200, note())
        self.actor_doc: dict | None = None  # ours, as fetched by the relay (see handler)

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if request.method == "POST":
            self.posted.append((url, json.loads(request.content), request))
            return httpx.Response(202)
        self.gets.append(request)
        if request.url.path.startswith("/api/v1/"):
            data = self.api.get(url.split("?")[0])
            return httpx.Response(200, json=data) if data is not None else httpx.Response(404, json={"error": "nope"})
        status, data = self.objects.get(url, (404, {"error": "Record not found"}))
        return httpx.Response(status, json=data)

    def signed(self, activity: dict, key: rsa.RSAPrivateKey | None = None, key_id: str = RELAY + "#key",
               host: str = ME, date: str | None = None) -> tuple[bytes, dict[str, str]]:
        """A delivery to our inbox, signed as the relay signs."""
        body = json.dumps(activity).encode()
        headers = {"host": host, "date": date or formatdate(usegmt=True),
                   "digest": "SHA-256=" + b64(hashlib.sha256(body).digest())}
        signing = "\n".join(["(request-target): post /threadbnc/inbox"] + [f"{k}: {headers[k]}" for k in headers])
        sig = (key or self.relay_key).sign(signing.encode(), padding.PKCS1v15(), hashes.SHA256())
        headers["signature"] = (f'keyId="{key_id}",algorithm="rsa-sha256",headers="(request-target) host date digest",'
                                f'signature="{b64(sig)}"')
        headers["content-type"] = "application/activity+json"
        return body, headers


def note(**changes) -> dict:
    obj = {"id": NOTE, "type": "Note", "attributedTo": "https://masto.test/users/alice",
           "url": "https://masto.test/@alice/111", "to": [PUBLIC], "cc": ["https://masto.test/users/alice/followers"],
           "published": "2026-09-24T10:00:00Z",
           "content": '<p>New homelab! Reading <a href="https://blog.test/rack?utm_source=x&amp;a=1">about racks</a> '
                      '<a href="https://masto.test/tags/selfhosted" class="mention hashtag" rel="tag">#<span>SelfHosted</span></a></p>',
           "tag": [{"type": "Hashtag", "name": "#SelfHosted", "href": "https://masto.test/tags/selfhosted"}],
           "attachment": [{"type": "Document", "mediaType": "image/jpeg", "url": "https://masto.test/media/1.jpg",
                           "name": "a rack"}],
           "likes": {"totalItems": 7}, "shares": {"totalItems": 2}}
    obj.update(changes)
    return obj


@pytest.fixture
def fedi():
    return Fediverse()


@pytest.fixture
def tagged(settings, server, fedi):
    """A bouncer with its own actor at bnc.test, its HTTP going to the fake fediverse."""
    settings = replace(settings, actor_domain=ME, tag_relay="https://relay.test/tag/{tag}")
    http = HttpClient("test", 5, 0)
    http._client = httpx.Client(transport=httpx.MockTransport(fedi.handler), follow_redirects=True)
    b = Bouncer(open_database(settings), settings, http=http, adapter_factory=lambda d: FakeAdapter(d, server))
    return b, settings


@pytest.fixture
def relays(tagged):
    b, settings = tagged
    return TagRelays(b, b.actor, settings.tag_relay)


def one(b, sql, *args):
    with b.db.connect() as conn:
        return conn.execute(sql, args).fetchone()


def run_jobs(b):
    while b.run_one_job():
        pass


def verify_ours(actor: Actor, request: httpx.Request) -> None:
    """What the relay does: check our signature with the key our actor document publishes."""
    params = parse_signature(request.headers["signature"])
    assert params["keyId"] == actor.key_id
    names = params["headers"].split()
    lines = [f"(request-target): {request.method.lower()} {request.url.raw_path.decode()}"]
    lines += [f"{n}: {request.headers[n]}" for n in names[1:]]
    key = serialization.load_pem_public_key(actor.document()["publicKey"]["publicKeyPem"].encode())
    key.verify(base64.b64decode(params["signature"]), "\n".join(lines).encode(), padding.PKCS1v15(), hashes.SHA256())
    if request.content:
        assert request.headers["digest"] == "SHA-256=" + b64(hashlib.sha256(request.content).digest())


# --- references and helpers ----------------------------------------------------------

def test_hashtags_are_communities():
    assert parse_community_ref("#SelfHosted") == CommunityRef(TAG_DOMAIN, "selfhosted", TAG_DOMAIN)
    assert parse_community_ref("#self-hosted") == CommunityRef(TAG_DOMAIN, "selfhosted", TAG_DOMAIN)
    assert parse_community_ref("tag:selfhosted").name == "selfhosted"
    with pytest.raises(ValueError):
        parse_community_ref("#not a/tag")


def test_shared_link_skips_mentions_and_hashtags():
    html = ('<p><span class="h-card"><a href="https://x.test/@bob" class="u-url mention">@bob</a></span> '
            '<a href="https://x.test/tags/a" class="mention hashtag">#a</a> <a href="https://site.test/p?a=1&amp;b=2">x</a></p>')
    assert shared_link(html) == "https://site.test/p?a=1&b=2"
    assert shared_link("<p>no links</p>") is None


def test_title_from_cuts_at_a_word():
    assert title_from("Short one\nsecond line") == "Short one"
    long = "word " * 40
    assert title_from(long).endswith("…") and len(title_from(long)) <= 81


# --- the actor -------------------------------------------------------------------------

def test_actor_document_and_key_are_stable(tagged):
    b, _ = tagged
    doc = b.actor.document()
    assert doc["id"] == f"https://{ME}/threadbnc/actor" and doc["inbox"] == f"https://{ME}/threadbnc/inbox"
    assert doc["publicKey"]["owner"] == doc["id"] and "BEGIN PUBLIC KEY" in doc["publicKey"]["publicKeyPem"]
    again = Actor(ME, b.db, b.actor.vault, b.http)  # a restart: the same key, from the database
    assert again.public_pem() == b.actor.public_pem()
    assert "PRIVATE" not in (b.db.get_setting("actor_private_key") or "")  # stored encrypted
    assert b.actor.webfinger(f"acct:threadbnc@{ME}")["links"][0]["href"] == doc["id"]
    assert b.actor.webfinger("acct:dave@bnc.test") is None


def test_signed_fetch_verifies_with_the_published_key(tagged, fedi):
    b, _ = tagged
    assert b.actor.fetch(NOTE)["id"] == NOTE
    verify_ours(b.actor, fedi.gets[-1])


def test_verify_accepts_the_relay_and_refuses_forgeries(tagged, fedi):
    b, _ = tagged
    act = {"id": "https://relay.test/a/1", "type": "Accept", "actor": RELAY, "object": "x"}
    body, headers = fedi.signed(act)
    assert b.actor.verify("POST", "/threadbnc/inbox", headers, body) == RELAY
    with pytest.raises(SignatureError, match="body"):
        b.actor.verify("POST", "/threadbnc/inbox", headers, body + b" ")
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    body, headers = fedi.signed(act, key=other)
    with pytest.raises(SignatureError, match="match"):
        b.actor.verify("POST", "/threadbnc/inbox", headers, body)
    body, headers = fedi.signed(act, date="Mon, 01 Jan 2024 00:00:00 GMT")
    with pytest.raises(SignatureError, match="old"):
        b.actor.verify("POST", "/threadbnc/inbox", headers, body)
    with pytest.raises(SignatureError, match="unsigned"):
        b.actor.verify("POST", "/threadbnc/inbox", {k: v for k, v in headers.items() if k != "signature"}, body)


# --- following a hashtag -----------------------------------------------------------------

def follow(b, relays) -> int:
    return b.follow_community("#SelfHosted", None, 30, False)


def test_following_a_hashtag_follows_its_relay(tagged, relays, fedi):
    b, _ = tagged
    cid = follow(b, relays)
    url, sent, request = fedi.posted[-1]
    assert url == RELAY and sent["type"] == "Follow" and sent["object"] == RELAY and sent["actor"] == b.actor.id
    verify_ours(b.actor, request)
    f = one(b, "SELECT * FROM community_follows WHERE community_id=?", cid)
    assert (f["push_state"], f["push_actor"], f["push_follow_id"], f["polling"]) == ("pending", RELAY, sent["id"], 0)
    c = one(b, "SELECT * FROM communities WHERE id=?", cid)
    assert (c["canonical_ap_id"], c["name"]) == ("tag:selfhosted", "selfhosted")


def test_a_relay_that_cant_be_reached_is_asked_again(tagged, relays, fedi):
    b, _ = tagged
    relay = fedi.objects.pop(RELAY)
    cid = follow(b, relays)
    f = one(b, "SELECT * FROM community_follows WHERE community_id=?", cid)
    assert f["push_state"] is None and "404" in f["push_error"]
    fedi.objects[RELAY] = relay
    relays.housekeeping()
    assert one(b, "SELECT push_state FROM community_follows WHERE community_id=?", cid)["push_state"] == "pending"


def test_unfollowing_undoes_the_follow(tagged, relays, fedi):
    b, _ = tagged
    cid = follow(b, relays)
    follow_id = fedi.posted[-1][1]["id"]
    b.unfollow(cid)
    url, sent, _ = fedi.posted[-1]
    assert url == RELAY and sent["type"] == "Undo" and sent["object"]["id"] == follow_id
    assert one(b, "SELECT push_state FROM community_follows WHERE community_id=?", cid)["push_state"] is None


# --- the inbox -------------------------------------------------------------------------

@pytest.fixture
def client(tagged, fedi):
    b, settings = tagged
    app = create_app(settings, b)
    return TestClient(app, base_url=f"https://{ME}")


def deliver(client, fedi, activity, **kw):
    body, headers = fedi.signed(activity, **kw)
    return client.post("/threadbnc/inbox", content=body, headers=headers)


def test_actor_and_webfinger_are_public_the_archive_is_not(client):
    r = client.get("/threadbnc/actor")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/activity+json")
    assert r.json()["preferredUsername"] == "threadbnc"
    r = client.get("/.well-known/webfinger", params={"resource": f"acct:threadbnc@{ME}"})
    assert r.status_code == 200 and r.json()["subject"] == f"acct:threadbnc@{ME}"
    assert client.get("/threadbnc/outbox").json()["totalItems"] == 0
    assert client.get("/", follow_redirects=False).status_code == 303  # still behind sign-in


def test_accept_then_announce_captures_the_post(client, tagged, fedi):
    b, _ = tagged
    relays = client.app.state.tags
    cid = follow(b, relays)
    follow_id = fedi.posted[-1][1]["id"]
    r = deliver(client, fedi, {"id": "https://relay.test/accept/1", "type": "Accept", "actor": RELAY,
                               "object": {"id": follow_id, "type": "Follow", "actor": b.actor.id, "object": RELAY}})
    assert r.status_code == 202
    assert one(b, "SELECT push_state FROM community_follows WHERE community_id=?", cid)["push_state"] == "subscribed"

    r = deliver(client, fedi, {"id": "https://relay.test/announce/1", "type": "Announce", "actor": RELAY,
                               "to": [PUBLIC], "object": NOTE})
    assert r.status_code == 202
    run_jobs(b)
    t = one(b, "SELECT t.*, o.canonical_ap_id FROM archived_threads t JOIN objects o ON o.id=t.root_object_id "
               "WHERE o.canonical_ap_id=?", NOTE)
    assert t is not None and t["community_id"] == cid and t["retention"] == "auto"
    assert (t["source_domain"], t["source_local_id"]) == (TAG_DOMAIN, f"selfhosted {NOTE}")
    assert t["next_check_at"] is None  # no background vote checks: read when opened
    r = one(b, "SELECT r.* FROM revisions r JOIN objects o ON o.id=r.object_id WHERE o.canonical_ap_id=?", NOTE)
    assert r["title"] == "New homelab! Reading about racks #SelfHosted"
    assert r["url"] == "https://blog.test/rack?utm_source=x&a=1"
    assert "about racks" in r["body"] and json.loads(r["metadata_json"])["untitled"] is True
    a = one(b, "SELECT a.* FROM actors a JOIN objects o ON o.author_id=a.id WHERE o.canonical_ap_id=?", NOTE)
    assert a["username"] == "alice"
    verify_ours(b.actor, next(g for g in fedi.gets if str(g.url) == NOTE))

    page = client.get(f"/c/{cid}", headers={"authorization": "Bearer tok"})
    assert page.status_code == 200 and "#selfhosted" in page.text and "From the relay" in page.text


def test_deliveries_from_strangers_and_forgeries_are_dropped(client, tagged, fedi):
    b, _ = tagged
    follow(b, client.app.state.tags)
    stranger = {"id": "https://evil.test/a/1", "type": "Announce", "actor": "https://evil.test/actor", "object": NOTE}
    before = len(fedi.gets)
    assert deliver(client, fedi, stranger, key_id="https://evil.test/actor#key").status_code == 202
    assert len(fedi.gets) == before  # nothing fetched for someone we don't follow
    forged = {"id": "https://relay.test/announce/2", "type": "Announce", "actor": RELAY, "object": NOTE}
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    assert deliver(client, fedi, forged, key=other).status_code == 401
    body, headers = fedi.signed(forged)
    assert client.post("/threadbnc/inbox", content=body, headers={**headers, "signature": ""}).status_code == 401
    run_jobs(b)
    assert one(b, "SELECT COUNT(*) AS n FROM archived_threads")["n"] == 0


def test_followers_only_posts_are_not_captured(client, tagged, fedi):
    b, _ = tagged
    follow(b, client.app.state.tags)
    fedi.objects[NOTE] = (200, note(to=["https://masto.test/users/alice/followers"], cc=[]))
    deliver(client, fedi, {"id": "https://relay.test/announce/3", "type": "Announce", "actor": RELAY, "object": NOTE})
    run_jobs(b)
    assert one(b, "SELECT COUNT(*) AS n FROM archived_threads")["n"] == 0


def test_a_post_is_filed_under_the_first_followed_hashtag_it_lists(client, tagged, fedi):
    b, _ = tagged
    relays = client.app.state.tags
    homelab = b.follow_community("#homelab", None, 30, False)
    follow(b, relays)
    fedi.objects[NOTE] = (200, note(tag=[{"type": "Hashtag", "name": "#Homelab"},
                                         {"type": "Hashtag", "name": "#selfhosted"}]))
    deliver(client, fedi, {"id": "https://relay.test/announce/4", "type": "Announce", "actor": RELAY, "object": NOTE})
    run_jobs(b)
    t = one(b, "SELECT t.community_id FROM archived_threads t JOIN objects o ON o.id=t.root_object_id "
               "WHERE o.canonical_ap_id=?", NOTE)
    assert t["community_id"] == homelab


def test_opening_reads_replies_from_the_posts_server(client, tagged, fedi):
    b, _ = tagged
    follow(b, client.app.state.tags)
    deliver(client, fedi, {"id": "https://relay.test/announce/5", "type": "Announce", "actor": RELAY, "object": NOTE})
    run_jobs(b)
    tid = one(b, "SELECT t.id FROM archived_threads t JOIN objects o ON o.id=t.root_object_id "
                 "WHERE o.canonical_ap_id=?", NOTE)["id"]
    reply = {"id": "201", "uri": "https://other.test/users/bob/statuses/201", "in_reply_to_id": "111",
             "content": "<p>Nice rack</p>", "created_at": "2026-09-24T10:05:00Z", "favourites_count": 3,
             "replies_count": 1, "spoiler_text": "", "media_attachments": [],
             "account": {"username": "bob", "uri": "https://other.test/users/bob"}}
    nested = {**reply, "id": "202", "uri": "https://masto.test/users/alice/statuses/202", "in_reply_to_id": "201",
              "content": "<p>Thanks!</p>", "account": {"username": "alice", "uri": "https://masto.test/users/alice"}}
    fedi.api["https://masto.test/api/v1/statuses/111/context"] = {"ancestors": [], "descendants": [reply, nested]}
    b.open_threads([tid])
    with b.db.connect() as conn:
        rows = {r["canonical_ap_id"]: r for r in conn.execute(
            "SELECT o.canonical_ap_id, o.parent_id, o.id FROM objects o "
            "WHERE o.thread_id=? AND o.object_type='comment'", (tid,)).fetchall()}
    assert set(rows) == {reply["uri"], nested["uri"]}
    assert rows[nested["uri"]]["parent_id"] == rows[reply["uri"]]["id"]
    # A reply the server no longer lists isn't taken for deleted: servers only know what reached them.
    fedi.api["https://masto.test/api/v1/statuses/111/context"] = {"ancestors": [], "descendants": [reply]}
    with b.db.transaction() as conn:
        conn.execute("UPDATE archived_threads SET last_full_fetch_at=NULL WHERE id=?", (tid,))
    b.open_threads([tid])
    gone = one(b, "SELECT cur_deleted, cur_missing FROM objects WHERE canonical_ap_id=?", nested["uri"])
    assert not gone["cur_deleted"] and not gone["cur_missing"]


def test_a_deleted_post_is_marked_missing_when_opened(client, tagged, fedi):
    b, _ = tagged
    follow(b, client.app.state.tags)
    deliver(client, fedi, {"id": "https://relay.test/announce/6", "type": "Announce", "actor": RELAY, "object": NOTE})
    run_jobs(b)
    tid = one(b, "SELECT t.id FROM archived_threads t JOIN objects o ON o.id=t.root_object_id "
                 "WHERE o.canonical_ap_id=?", NOTE)["id"]
    fedi.objects[NOTE] = (410, {"id": NOTE, "type": "Tombstone"})
    b.sync_thread(tid, force=True)
    assert one(b, "SELECT cur_missing FROM objects WHERE canonical_ap_id=?", NOTE)["cur_missing"]
    assert one(b, "SELECT body FROM revisions r JOIN objects o ON o.id=r.object_id WHERE o.canonical_ap_id=?",
               NOTE)["body"]  # the text stays


def test_without_an_actor_hashtags_cant_be_followed(bouncer):
    with pytest.raises(Exception, match="THREADBNC_ACTOR_DOMAIN"):
        bouncer.follow_community("#selfhosted", None, 30, False)
    assert urlparse(RELAY).hostname == "relay.test"


def test_the_relays_own_name_for_a_hashtag_is_followed(tagged, relays, fedi):
    """FediBuzz transliterates: #café is its actor .../tag/cafe, which then signs and announces."""
    b, _ = tagged
    fedi.objects["https://relay.test/tag/caf%C3%A9"] = (200, {**fedi.objects[RELAY][1], "id": "https://relay.test/tag/cafe",
                                                            "inbox": "https://relay.test/tag/cafe"})
    cid = b.follow_community("#Café", None, 30, False)
    url, sent, _ = fedi.posted[-1]
    assert url == "https://relay.test/tag/cafe" and sent["object"] == "https://relay.test/tag/cafe"
    assert one(b, "SELECT push_actor FROM community_follows WHERE community_id=?", cid)["push_actor"] == \
        "https://relay.test/tag/cafe"
    assert relays.expects({"actor": ["https://relay.test/tag/cafe"]})
