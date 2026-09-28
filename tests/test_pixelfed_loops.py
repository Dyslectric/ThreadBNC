"""Pixelfed and Loops accounts, followed like communities through their servers' public APIs."""

from __future__ import annotations

import html

import httpx
import pytest

from threadbnc.adapters import (RemoteNotFound, media_software, parse_community_ref, parse_thread_url,
                                profile_ref)
from threadbnc.adapters.loops import LoopsAdapter
from threadbnc.adapters.pixelfed import PixelfedAdapter
from threadbnc.videos import video_of

from .conftest import FakeAdapter
from .test_mastodon import HOME, WithHome, sign_in, status
from .test_tags import client, one, tagged  # noqa: F401

PIX = "pix.test"
LOOPS = "loops.test"
ALICE = f"https://{PIX}/users/alice"
CAROL = f"https://{LOOPS}/ap/users/3"


def pf_status(sid, text, **more):
    account = {"id": "7", "username": "alice", "acct": "alice", "display_name": "Alice", "local": True,
               "url": f"https://{PIX}/alice", "avatar": f"https://{PIX}/a/alice.jpg"}
    return {"id": sid, "uri": f"https://{PIX}/p/alice/{sid}", "url": f"https://{PIX}/p/alice/{sid}",
            "content": html.escape(text), "account": account, "visibility": "public", "spoiler_text": "",
            "sensitive": False, "created_at": "2026-09-27T10:00:00.000Z", "favourites_count": 5, "reply_count": 0,
            "in_reply_to_id": None, "reblog": None, "media_attachments": [], **more}


def pf_comment(cid, text, parent, who="bob", who_id="8", replies=0):
    account = {"id": who_id, "username": who, "acct": who, "local": True, "url": f"https://{PIX}/{who}"}
    return {"id": cid, "uri": f"https://{PIX}/p/{who}/{cid}", "content": html.escape(text), "account": account,
            "in_reply_to_id": parent, "created_at": "2026-09-27T11:00:00.000Z", "favourites_count": 1,
            "reply_count": replies, "spoiler_text": "", "media_attachments": []}


def loop(vid, caption, **more):
    return {"id": vid, "account": {"id": "3", "username": "carol", "name": "Carol", "avatar": f"https://{LOOPS}/a.webp"},
            "caption": caption, "url": f"https://{LOOPS}/v/code{vid}", "shortcode": f"code{vid}", "is_sensitive": False,
            "media": {"thumbnail": f"https://cdn.{LOOPS}/{vid}.jpg", "src_url": f"https://cdn.{LOOPS}/{vid}.720p.mp4",
                      "alt_text": "a cat"},
            "pinned": False, "likes": 12, "comments": 2, "lang": "en", "permissions": {"can_comment": True},
            "created_at": "2026-09-27T09:00:00+00:00", **more}


def loops_comment(cid, text, who_id="4", who="dan", replies=0, **more):
    return {"id": cid, "account": {"id": who_id, "username": who, "name": who.title()}, "caption": text,
            "replies": replies, "likes": 2, "remote_url": None, "tombstone": False,
            "created_at": "2026-09-27T12:00:00+00:00", **more}


class WithMedia(WithHome):
    """A Pixelfed server (pix.test) and a Loops server (loops.test) beside the rest of the fediverse."""

    def __init__(self) -> None:
        super().__init__()
        self.pixelfed = [
            pf_status("103", "Shared by Alice", reblog={"id": "99"}),
            pf_status("102", "A reply", in_reply_to_id="50"),
            pf_status("101", "Autumn walk #photography", favourites_count=9, reply_count=2, media_attachments=[
                {"type": "image", "url": f"https://{PIX}/m/1.jpg"}, {"type": "image", "url": f"https://{PIX}/m/2.jpg"}]),
            pf_status("100", "Private matters", sensitive=True, media_attachments=[
                {"type": "image", "url": f"https://{PIX}/m/0.jpg"}]),
        ]
        self.pf_comments = {("7", "101"): [pf_comment("201", "Lovely", "101", replies=1),
                                           pf_comment("202", "Where?", "101", who="cat", who_id="9")],
                            ("8", "201"): [pf_comment("301", "Thanks!", "201", who="alice", who_id="7"),
                                           # a deleted account's: Pixelfed still lists it, with no one and no address
                                           {**pf_comment("302", "Spam", "201"), "uri": "/404", "account": None}]}
        self.loops = [loop("51", "Pinned intro", pinned=True), loop("52", "My cat\n#cats", is_sensitive=True),
                      loop("50", "")]
        self.loops_comments = {"52": [loops_comment("61", "So cute", replies=1),
                                      loops_comment("62", "gone", tombstone=True),
                                      loops_comment("63", "hi from afar", who="eve@masto.test",
                                                    remote_url="https://masto.test/@eve/5")]}
        self.loops_replies = {"61": [loops_comment("71", "Isn't he", who_id="3", who="carol")]}
        self.asked: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        host, path, q = request.url.host, request.url.path, request.url.params
        if host == PIX:
            self.asked.append(str(request.url))
            if path == "/.well-known/nodeinfo":
                return httpx.Response(200, json={"links": [{"rel": "http://nodeinfo.diaspora.software/ns/schema/2.0",
                                                            "href": f"https://{PIX}/api/nodeinfo/2.0.json"}]})
            if path == "/api/nodeinfo/2.0.json":
                return httpx.Response(200, json={"software": {"name": "pixelfed", "version": "0.12.6"}})
            if path == "/api/v1/accounts/lookup":
                if q["acct"] != "alice":
                    return httpx.Response(404, json={"error": "Record not found"})
                return httpx.Response(200, json={"id": "7", "username": "alice", "acct": "alice", "locked": False,
                                                 "display_name": "Alice", "note": "<p>Photos, mostly</p>"})
            if path == "/api/pixelfed/v1/accounts/7/statuses":
                older = [s for s in self.pixelfed if "max_id" not in q or int(s["id"]) < int(q["max_id"])]
                return httpx.Response(200, json=older[:int(q["limit"])])
            if path.startswith("/api/v2/profile/alice/status/"):
                sid = path.rsplit("/", 1)[1]
                found = [s for s in self.pixelfed if s["id"] == sid]
                return httpx.Response(200, json={"status": found[0]}) if found else httpx.Response(404, json={})
            if path.startswith("/api/v2/comments/"):
                _, _, _, account, _, sid = path.strip("/").split("/")
                if (account, sid) not in self.pf_comments and not any(s["id"] == sid for s in self.pixelfed):
                    return httpx.Response(404, json={"error": "No query results"})
                data = self.pf_comments.get((account, sid), [])
                return httpx.Response(200, json={"data": data, "meta": {"pagination": {"total_pages": 1}}})
        if host == LOOPS:
            self.asked.append(str(request.url))
            if path == "/api/v1/account/username/carol":
                return httpx.Response(200, json={"data": {"id": "3", "username": "carol", "name": "Carol",
                                                          "bio": "Cats #cats", "local": True, "remote_url": None}})
            if path == "/ap/users/3":
                return httpx.Response(200, json={"id": CAROL, "type": "Person", "preferredUsername": "carol"})
            if path == "/api/v1/feed/account/3":
                return httpx.Response(200, json={"data": self.loops, "meta": {"next_cursor": None}})
            if path.startswith("/api/v1/video/comments/"):
                vid = path.split("/")[5]
                if path.endswith("/replies"):
                    data = self.loops_replies.get(q["cr"], [])
                else:
                    data = self.loops_comments.get(vid, [])
                return httpx.Response(200, json={"data": data, "meta": {"next_cursor": None}})
            if path.startswith("/api/v1/video/"):
                found = [v for v in self.loops if v["id"] == path.rsplit("/", 1)[1]]
                return httpx.Response(200, json={"data": found[0]}) if found else \
                    httpx.Response(404, json={"data": [], "error": {"code": 404, "message": "Record not found."}})
            if path.startswith("/v/code"):
                return httpx.Response(200, json={"id": f"{CAROL}/video/{path[len('/v/code'):]}", "type": "Note"})
            return httpx.Response(404, json={"message": "Not Found."})
        return super().handler(request)


@pytest.fixture
def fedi():
    return WithMedia()


@pytest.fixture
def b(tagged, server, fedi):
    bouncer, _ = tagged
    media = {PIX: PixelfedAdapter(PIX, bouncer.http), LOOPS: LoopsAdapter(LOOPS, bouncer.http)}
    bouncer._adapter_factory = lambda d: media.get(d) or FakeAdapter(d, server)
    return bouncer


@pytest.fixture
def web(client, b):
    client.post("/login", data={"password": "pw"})
    return client


def root(bouncer, ap_id):
    return one(bouncer, "SELECT t.id AS tid, o.id AS oid, r.title, r.body, r.url, r.metadata_json, o.upvotes, "
                        "o.reply_count, o.thumbnail_url, t.next_check_at FROM archived_threads t "
                        "JOIN objects o ON o.id=t.root_object_id JOIN revisions r ON r.object_id=o.id "
                        "WHERE o.canonical_ap_id=?", ap_id)


def comments(bouncer, tid):
    with bouncer.db.connect() as conn:
        return conn.execute("SELECT o.canonical_ap_id AS ap_id, r.body, p.canonical_ap_id AS parent, o.cur_deleted "
                            "FROM objects o JOIN revisions r ON r.object_id=o.id "
                            "LEFT JOIN objects p ON p.id=o.parent_id "
                            "WHERE o.thread_id=? AND o.object_type='comment' ORDER BY o.canonical_ap_id",
                            (tid,)).fetchall()


def test_accounts_and_posts_are_recognised():
    assert parse_community_ref("@alice@pix.test").domain == "pix.test"
    assert parse_community_ref("@alice@pix.test").name == "alice"
    assert parse_community_ref("!math@lemmy.ml").name == "math"  # the rest as before
    for url, name in (("https://pix.test/alice", "alice"), ("https://pix.test/users/alice/", "alice"),
                      ("https://loops.test/@carol", "carol"), ("https://loops.test/ap/users/3", "3")):
        assert (profile_ref(url).domain, profile_ref(url).name) == (url.split("/")[2], name)
    for url in ("https://blog.test/feed", "https://blog.test/index.xml", "https://blog.test/a/b", "https://x.test/?p=1"):
        assert profile_ref(url) is None
    t = parse_thread_url("https://pix.test/p/alice/101")
    assert (t.domain, t.kind, t.local_id) == (PIX, "post", "alice/101")
    assert parse_thread_url("https://loops.test/v/code52").local_id == "code52"


def test_following_a_pixelfed_account_captures_its_posts(web, b, fedi):
    cid = b.follow_community("@alice@pix.test", backfill=True)
    c = one(b, "SELECT * FROM communities WHERE id=?", cid)
    assert (c["canonical_ap_id"], c["name"], c["title"]) == (ALICE, "alice", "Alice")
    f = one(b, "SELECT * FROM community_follows WHERE community_id=?", cid)
    assert (f["source_domain"], f["source_ref"], f["polling"], f["poll_interval_minutes"]) == (PIX, "alice", 1, 30)
    assert media_software(PIX) == "pixelfed"

    assert b.poll_follow(cid) == 2  # not the repost or the reply
    walk = root(b, f"https://{PIX}/p/alice/101")
    assert (walk["title"], walk["upvotes"], walk["reply_count"]) == ("Autumn walk #photography", 9, 2)
    assert walk["thumbnail_url"] == f"https://{PIX}/m/1.jpg" and walk["next_check_at"] is None
    assert '"nsfw": true' in root(b, f"https://{PIX}/p/alice/100")["metadata_json"]
    assert one(b, "SELECT a.canonical_ap_id FROM objects o JOIN actors a ON a.id=o.author_id "
                  "WHERE o.canonical_ap_id=?", f"https://{PIX}/p/alice/101")[0] == ALICE

    fedi.pixelfed[2]["favourites_count"] = 11  # likes come with each check
    b.poll_follow(cid)
    assert root(b, f"https://{PIX}/p/alice/101")["upvotes"] == 11

    page = web.get("/").text
    assert "Autumn walk" in page and "@pix.test · Pixelfed" in page
    assert f'name="community" value="{ALICE}"' not in web.get(f"/c/{cid}").text  # no Follow: followed already
    b.unfollow(cid)
    assert f'name="community" value="{ALICE}"' in web.get(f"/c/{cid}").text


def test_a_profile_link_is_followed_as_the_account_once_its_server_says(web, b, fedi, server):
    b._adapter_factory = None  # asked of the server itself, as in real life
    cid = b.follow_community("https://pix.test/alice")
    assert one(b, "SELECT canonical_ap_id FROM communities WHERE id=?", cid)[0] == ALICE
    assert one(b, "SELECT software FROM instances WHERE domain=?", PIX)[0] == "pixelfed"


def test_opening_a_pixelfed_post_reads_its_comments_and_their_replies(b, fedi):
    cid = b.follow_community("@alice@pix.test", backfill=True)
    b.poll_follow(cid)
    tid = root(b, f"https://{PIX}/p/alice/101")["tid"]
    b.open_threads([tid])
    got = [(c["ap_id"].rsplit("/", 2)[-2:], c["body"], c["parent"] and c["parent"].rsplit("/", 1)[-1])
           for c in comments(b, tid)]
    assert sorted(got) == [(["alice", "301"], "Thanks!", "201"), (["bob", "201"], "Lovely", "101"),
                           (["cat", "202"], "Where?", "101")]


def test_a_pixelfed_post_can_be_kept_by_its_link(b, fedi):
    tid = b.ingest_url("https://pix.test/p/alice/101")
    t = one(b, "SELECT retention, source_domain, source_local_id FROM archived_threads WHERE id=?", tid)
    assert (t["retention"], t["source_domain"], t["source_local_id"]) == ("manual", PIX, "alice/101")
    assert len(comments(b, tid)) == 3
    with pytest.raises(RemoteNotFound):
        b.ingest_url("https://pix.test/p/alice/999")


def test_pixelfed_posts_are_liked_as_your_mastodon_account(web, b, fedi):
    cid = b.follow_community("@alice@pix.test", backfill=True)
    b.poll_follow(cid)
    oid = root(b, f"https://{PIX}/p/alice/101")["oid"]
    r = web.post(f"/o/{oid}/vote", data={"score": "1"})
    assert "Add an account on the Accounts page first." in r.text and fedi.home.favourites == []
    web.get("/accounts/mastodon/callback", params={"state": sign_in(web, fedi), "code": "good"})
    fedi.home.statuses["9500"] = status("9500", f"https://{PIX}/p/alice/101", "alice@pix.test", "Autumn walk")
    web.post(f"/o/{oid}/vote", data={"score": "1"})
    assert fedi.home.favourites == ["+9500"]


def test_following_a_loops_account_captures_its_videos(web, b, fedi):
    cid = b.follow_community("https://loops.test/@carol", backfill=True)
    c = one(b, "SELECT * FROM communities WHERE id=?", cid)
    assert (c["canonical_ap_id"], c["name"], c["title"]) == (CAROL, "carol", "Carol")
    assert b.poll_follow(cid) == 3
    cat = root(b, f"{CAROL}/video/52")
    assert (cat["title"], cat["url"], cat["thumbnail_url"]) == (
        "My cat", f"https://cdn.{LOOPS}/52.720p.mp4", f"https://cdn.{LOOPS}/52.jpg")
    assert "[#cats](https://loops.test/tag/cats)" in cat["body"] and '"nsfw": true' in cat["metadata_json"]
    assert (cat["upvotes"], cat["reply_count"]) == (12, 2)
    assert root(b, f"{CAROL}/video/50")["title"] == "Untitled loop"
    assert one(b, "SELECT cur_featured FROM objects WHERE canonical_ap_id=?", f"{CAROL}/video/51")[0]

    page = web.get(f"/t/{cat['tid']}").text
    assert f'<video class="media" src="https://cdn.{LOOPS}/52.720p.mp4"' in page
    assert "@carol@loops.test (Loops)" in page and "This is on Loops." in page


def test_opening_a_loops_video_reads_its_comments(b, fedi):
    cid = b.follow_community("@carol@loops.test", backfill=True)
    b.poll_follow(cid)
    tid = root(b, f"{CAROL}/video/52")["tid"]
    b.open_threads([tid])
    got = {c["ap_id"]: c for c in comments(b, tid)}
    assert set(got) == {f"https://{LOOPS}/ap/users/4/comment/61", f"https://{LOOPS}/ap/users/4/comment/62",
                        "https://masto.test/@eve/5", f"{CAROL}/reply/71"}
    assert got[f"{CAROL}/reply/71"]["parent"] == f"https://{LOOPS}/ap/users/4/comment/61"
    assert got[f"https://{LOOPS}/ap/users/4/comment/62"]["cur_deleted"]


def test_a_loops_video_can_be_kept_by_its_link(b, fedi):
    tid = b.ingest_url("https://loops.test/v/code52")
    assert one(b, "SELECT source_local_id FROM archived_threads WHERE id=?", tid)[0] == "52"
    assert root(b, f"{CAROL}/video/52")["tid"] == tid


def test_loops_links_play_in_loops_player(b):
    b.adapter_for(LOOPS)  # known to run Loops
    v = video_of("https://loops.test/v/code52")
    assert (v.kind, v.embed, v.fetch_url, v.site) == ("loops", "https://loops.test/embed/code52",
                                                      "https://loops.test/v/code52", "Loops")
    assert video_of("https://elsewhere.test/v/code52") is None  # not known to be Loops
    assert video_of("https://loops.video/v/eQYqneK5va").kind == "loops"


def test_hashtag_posts_from_pixelfed_read_their_comments_there(b, fedi):
    got = b.tag_adapter.fetch_comments(f"autumn https://{PIX}/p/alice/101")
    assert not got.complete and {c.body for c in got} == {"Lovely", "Where?", "Thanks!"}
