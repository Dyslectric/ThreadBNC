"""Your Mastodon account: signing in, and liking and replying to posts from hashtags and their replies."""

from __future__ import annotations

import base64
import hashlib
import html
import json
import re
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from .test_tags import NOTE, RELAY, Fediverse, client, deliver, follow, one, run_jobs, tagged  # noqa: F401

HOME = "home.test"  # your Mastodon account's server
TOKEN = "tok-dave"
ME = {"id": "1", "username": "dave", "acct": "dave", "display_name": "Dave", "uri": f"https://{HOME}/users/dave"}
BOB_REPLY = "https://other.test/users/bob/statuses/201"


def status(sid, uri, acct, text, **more):
    account = {"id": acct, "acct": acct, "username": acct.split("@")[0],
               "uri": f"https://{acct.split('@')[1]}/users/{acct.split('@')[0]}" if "@" in acct else ME["uri"]}
    return {"id": sid, "uri": uri, "content": f"<p>{html.escape(text)}</p>", "account": account,
            "visibility": "public", "spoiler_text": "", "sensitive": False, "mentions": [],
            "created_at": "2026-09-24T12:00:00.000Z", "favourites_count": 0, "replies_count": 0, **more}


class FakeHome:
    """Your Mastodon server, as its client API answers ThreadBNC."""

    def __init__(self) -> None:
        self.apps: list[dict] = []
        self.challenge: str | None = None
        self.revoked: list[str] = []
        self.favourites: list[str] = []
        self.posted: list[dict] = []
        self.deleted: list[str] = []
        self.context: list[dict] = []  # the replies to any post, for anyone asking
        self.live_feeds = {"local": "public", "remote": "public"}  # what it says of its live feeds
        self.trending: list[dict] = []  # /api/v1/trends/tags
        self.statuses = {
            "9001": status("9001", NOTE, "alice@masto.test", "New homelab!", favourites_count=7),
            "9002": status("9002", BOB_REPLY, "bob@other.test", "Nice rack", spoiler_text="racks",
                           mentions=[{"acct": "alice@masto.test"}]),
        }

    def handle(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        body = json.loads(request.content) if request.content else {}
        if path == "/api/v1/instance":
            return httpx.Response(200, json={"uri": HOME, "title": "Home"})
        if path == "/api/v2/instance":
            return httpx.Response(200, json={"domain": HOME, "title": "Home", "configuration": {
                "urls": {"streaming": "wss://streaming.home.test"},
                "timelines_access": {"live_feeds": self.live_feeds}}})
        if path == "/api/v1/apps" and method == "POST":
            self.apps.append(body)
            return httpx.Response(200, json={"client_id": "cid", "client_secret": "csecret"})
        if path == "/oauth/token" and method == "POST":
            verifier = body.get("code_verifier", "")
            made = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
            if body.get("code") != "good" or made != self.challenge or body.get("client_secret") != "csecret":
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(200, json={"access_token": TOKEN, "token_type": "Bearer", "scope": "read write"})
        if path == "/oauth/revoke" and method == "POST":
            self.revoked.append(body["token"])
            return httpx.Response(200, json={})
        public = re.fullmatch(r"/api/v1/statuses/(\w+)(/context)?", path)
        if public and method == "GET" and "authorization" not in request.headers:  # anyone can read a public post
            if public.group(2):
                return httpx.Response(200, json={"ancestors": [], "descendants": self.context})
            return httpx.Response(200, json=self.statuses[public.group(1)])
        if request.headers.get("authorization") != f"Bearer {TOKEN}":
            return httpx.Response(401, json={"error": "The access token is invalid"})
        if path == "/api/v1/trends/tags":
            return httpx.Response(200, json=self.trending[:int(request.url.params.get("limit", 10))])
        if path == "/api/v1/accounts/verify_credentials":
            return httpx.Response(200, json=ME)
        if path == "/api/v2/search":
            q = request.url.params["q"]
            assert request.url.params["resolve"] == "true"
            found = [s for s in self.statuses.values() if s["uri"] == q]
            return httpx.Response(200, json={"accounts": [], "hashtags": [], "statuses": found})
        m = re.fullmatch(r"/api/v1/statuses/(\w+)(?:/(favourite|unfavourite))?", path)
        if m and method == "GET":
            return httpx.Response(200, json=self.statuses[m.group(1)])
        if m and m.group(2):
            self.favourites.append(("+" if m.group(2) == "favourite" else "-") + m.group(1))
            return httpx.Response(200, json=self.statuses[m.group(1)])
        if m and method == "DELETE":
            self.deleted.append(m.group(1))
            return httpx.Response(200, json=self.statuses.pop(m.group(1)))
        if path == "/api/v1/statuses" and method == "POST":
            self.posted.append(body)
            sid = str(9100 + len(self.posted))
            self.statuses[sid] = status(sid, f"https://{HOME}/users/dave/statuses/{sid}", "dave", body["status"],
                                        in_reply_to_id=body.get("in_reply_to_id"),
                                        spoiler_text=body.get("spoiler_text", ""))
            return httpx.Response(200, json=self.statuses[sid])
        raise AssertionError(f"unexpected {method} {request.url}")


class WithHome(Fediverse):
    def __init__(self) -> None:
        super().__init__()
        self.home = FakeHome()

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == HOME:
            return self.home.handle(request)
        return super().handler(request)


@pytest.fixture
def fedi():
    return WithHome()


@pytest.fixture
def web(client):
    client.post("/login", data={"password": "pw"})
    return client


def sign_in(web, fedi):
    r = web.post("/accounts/mastodon", data={"server": HOME}, follow_redirects=False)
    assert r.headers["location"] == "/accounts/mastodon/go"
    page = web.get("/accounts/mastodon/go").text
    authorize = html.unescape(re.search(r'url=([^"]+)"', page).group(1))
    asked = urlparse(authorize)
    q = {k: v[0] for k, v in parse_qs(asked.query).items()}
    assert (asked.netloc, asked.path) == (HOME, "/oauth/authorize")
    assert q["redirect_uri"] == "https://bnc.test/accounts/mastodon/callback" and q["client_id"] == "cid"
    assert q["scope"] == "read write" and q["code_challenge_method"] == "S256"
    fedi.home.challenge = q["code_challenge"]
    return q["state"]


def captured(b, relays_client, fedi) -> int:
    """The hashtag post, relayed in and opened with the reply from Bob."""
    follow(b, relays_client.app.state.tags)
    deliver(relays_client, fedi, {"id": "https://relay.test/announce/9", "type": "Announce", "actor": RELAY,
                                  "object": NOTE})
    run_jobs(b)
    tid = one(b, "SELECT t.id FROM archived_threads t JOIN objects o ON o.id=t.root_object_id "
                 "WHERE o.canonical_ap_id=?", NOTE)["id"]
    reply = {"id": "201", "uri": BOB_REPLY, "in_reply_to_id": "111", "content": "<p>Nice rack</p>",
             "created_at": "2026-09-24T10:05:00Z", "favourites_count": 3, "replies_count": 0, "spoiler_text": "racks",
             "media_attachments": [], "account": {"username": "bob", "uri": "https://other.test/users/bob"}}
    fedi.api["https://masto.test/api/v1/statuses/111/context"] = {"ancestors": [], "descendants": [reply]}
    b.open_threads([tid])
    return tid


def test_signing_in_on_your_servers_page(web, tagged, fedi):
    b, _ = tagged
    state = sign_in(web, fedi)
    assert fedi.home.apps[0]["redirect_uris"] == "https://bnc.test/accounts/mastodon/callback"
    web.cookies.clear()  # coming back from the Mastodon server, the session cookie isn't sent
    r = web.get("/accounts/mastodon/callback", params={"state": state, "code": "good"})
    assert r.status_code == 200 and "url=/accounts#mastodon" in r.text
    a = one(b, "SELECT * FROM accounts")
    assert (a["domain"], a["software"], a["username"], a["actor_ap_id"], a["is_default"]) == (
        HOME, "mastodon", "dave", ME["uri"], 0)
    assert TOKEN not in a["token_enc"]
    web.post("/login", data={"password": "pw"})
    page = web.get("/accounts").text
    assert "Signed in as <strong>@dave@home.test</strong>" in page and "Make default" not in page
    assert web.get("/accounts/mastodon/callback", params={"state": state, "code": "good"}).status_code == 400
    # Signing in again reuses the app registered on that server.
    web.get("/accounts/mastodon/callback", params={"state": sign_in(web, fedi), "code": "good"})
    assert len(fedi.home.apps) == 1 and one(b, "SELECT COUNT(*) AS n FROM accounts")["n"] == 1
    # Removing it revokes ThreadBNC's access on the server.
    web.post(f"/accounts/{a['id']}/remove")
    assert fedi.home.revoked == [TOKEN] and one(b, "SELECT COUNT(*) AS n FROM accounts")["n"] == 0


def test_a_declined_or_bad_sign_in_adds_nothing(web, tagged, fedi):
    b, _ = tagged
    r = web.get("/accounts/mastodon/callback", params={"state": sign_in(web, fedi), "error": "access_denied"})
    assert r.status_code == 400 and "You declined on home.test" in r.text
    r = web.get("/accounts/mastodon/callback", params={"state": sign_in(web, fedi), "code": "stale"})
    assert r.status_code == 400 and "home.test refused the sign-in" in r.text
    assert one(b, "SELECT COUNT(*) AS n FROM accounts")["n"] == 0


def test_liking_and_replying_to_posts_from_hashtags(web, tagged, fedi):
    b, _ = tagged
    tid = captured(b, web, fedi)
    web.get("/accounts/mastodon/callback", params={"state": sign_in(web, fedi), "code": "good"})
    post = one(b, "SELECT id FROM objects WHERE canonical_ap_id=?", NOTE)["id"]
    bob = one(b, "SELECT id FROM objects WHERE canonical_ap_id=?", BOB_REPLY)["id"]
    page = web.get(f"/t/{tid}").text
    assert 'title="Like as @dave@home.test"' in page and "Comment as @dave@home.test" in page
    assert "Downvote" not in page and "Sign in to Mastodon" not in page

    web.post(f"/o/{post}/vote", data={"score": "1"})
    web.post(f"/o/{bob}/vote", data={"score": "1"})
    assert fedi.home.favourites == ["+9001", "+9002"]
    assert one(b, "SELECT upvotes FROM objects WHERE id=?", bob)["upvotes"] == 4
    assert 'title="Remove like as @dave@home.test"' in web.get(f"/t/{tid}").text
    web.post(f"/o/{bob}/vote", data={"score": "0"})
    assert fedi.home.favourites[-1] == "-9002"
    assert "likes (favourites), not downvotes" in web.post(f"/o/{bob}/vote", data={"score": "-1"}).text

    # A comment on the post mentions its author; a reply to Bob mentions him and who he mentioned, with his warning.
    web.post(f"/t/{tid}/reply", data={"body": "Lovely rack"})
    web.post(f"/t/{tid}/reply", data={"body": "Agreed, @bob@other.test", "parent_id": str(bob)})
    first, second = fedi.home.posted
    assert (first["status"], first["in_reply_to_id"]) == ("@alice@masto.test Lovely rack", "9001")
    assert "visibility" not in first and "spoiler_text" not in first
    assert second["status"] == "@alice@masto.test Agreed, @bob@other.test"
    assert (second["in_reply_to_id"], second["spoiler_text"]) == ("9002", "racks")
    mine = one(b, "SELECT o.id, o.parent_id, r.body FROM objects o JOIN revisions r ON r.object_id=o.id "
                  "WHERE o.canonical_ap_id=?", f"https://{HOME}/users/dave/statuses/9102")
    assert mine["parent_id"] == bob and "Agreed" in mine["body"]
    assert "Delete on home.test" in web.get(f"/t/{tid}").text

    web.post(f"/o/{mine['id']}/delete", data={})
    assert fedi.home.deleted == ["9102"]


def comments_of(page):
    """What the feed opens under a post: the page's comments section."""
    return page[page.index('<section class="comments"'):]


def test_comments_opened_in_the_feed_can_be_added_to(web, tagged, fedi):
    b, _ = tagged
    tid = captured(b, web, fedi)
    part = comments_of(web.get(f"/t/{tid}?inline=1").text)
    assert 'href="/accounts#mastodon">Sign in to Mastodon' in part
    web.get("/accounts/mastodon/callback", params={"state": sign_in(web, fedi), "code": "good"})
    part = comments_of(web.get(f"/t/{tid}?inline=1").text)
    assert f'action="/t/{tid}/reply"' in part and "Comment as @dave@home.test" in part
    # Sent from there by app.js, it answers where it went instead of leaving the feed.
    r = web.post(f"/t/{tid}/reply", data={"body": "From the feed"}, headers={"X-ThreadBNC-Fetch": "1"})
    assert r.json()["ok"] and "#o" in r.json()["redirect"]
    assert fedi.home.posted[-1]["status"] == "@alice@masto.test From the feed"


def test_without_a_mastodon_account_it_says_how(web, tagged, fedi):
    b, _ = tagged
    tid = captured(b, web, fedi)
    page = web.get(f"/t/{tid}").text
    assert 'href="/accounts#mastodon">Sign in to Mastodon' in page and "Like as" not in page
    bob = one(b, "SELECT id FROM objects WHERE canonical_ap_id=?", BOB_REPLY)["id"]
    r = web.post(f"/o/{bob}/vote", data={"score": "1"})
    assert "Add an account on the Accounts page first." in r.text and fedi.home.favourites == []


def test_a_server_without_mastodons_api_is_refused(web, tagged, fedi):
    r = web.post("/accounts/mastodon", data={"server": "lemmy.test"})
    assert "doesn&#39;t look like a Mastodon server" in r.text or "doesn't look like a Mastodon server" in r.text


def test_the_composer_posts_on_your_mastodon_account(web, tagged, fedi):
    b, _ = tagged
    web.get("/accounts/mastodon/callback", params={"state": sign_in(web, fedi), "code": "good"})
    web.post("/login", data={"password": "pw"})
    me = one(b, "SELECT id FROM accounts WHERE software='mastodon'")["id"]
    follow(b, web.app.state.tags)
    tag = one(b, "SELECT id FROM communities WHERE canonical_ap_id='tag:selfhosted'")["id"]
    form = web.get("/post").text
    assert f'name="account_id" value="{me}" data-kind="Mastodon" checked' in form and "@dave@home.test" in form
    assert f'name="community_id" value="{tag}"' not in form  # hashtags aren't places to post any more
    r = web.post("/post", data={"title": "Racks", "body": "Mine is full.", "url": "https://blog.test/rack",
                                "account_id": str(me)}, follow_redirects=False)
    assert fedi.home.posted == [{"status": "Racks\n\nMine is full.\n\nhttps://blog.test/rack"}]
    uri = f"https://{HOME}/users/dave/statuses/9101"
    t = one(b, "SELECT t.id, t.retention, t.source_domain, c.canonical_ap_id, c.name FROM archived_threads t "
               "JOIN objects o ON o.id=t.root_object_id JOIN communities c ON c.id=t.community_id "
               "WHERE o.canonical_ap_id=?", uri)
    assert (t["retention"], t["source_domain"], t["canonical_ap_id"], t["name"]) == ("manual", "hashtag", ME["uri"], "dave")
    assert r.headers["location"] == f"/t/{t['id']}"
    page = web.get(f"/t/{t['id']}").text
    assert "Comment as @dave@home.test" in page and '>@dave@home.test (Mastodon)</a>' in page
    # Opening it reads it back from your server, as anyone could, with its replies.
    fedi.home.statuses["9101"]["favourites_count"] = 4
    fedi.home.context = [{"id": "301", "uri": "https://other.test/users/bob/statuses/301", "in_reply_to_id": "9101",
                          "content": "<p>Full indeed</p>", "created_at": "2026-09-25T10:00:00Z", "favourites_count": 0,
                          "replies_count": 0, "spoiler_text": "", "media_attachments": [],
                          "account": {"username": "bob", "uri": "https://other.test/users/bob"}}]
    b.sync_thread(t["id"], force=True)
    assert one(b, "SELECT upvotes FROM objects WHERE canonical_ap_id=?", uri)["upvotes"] == 4
    assert one(b, "SELECT COUNT(*) AS n FROM objects WHERE thread_id=?", t["id"])["n"] == 2
    assert one(b, "SELECT community_id FROM archived_threads WHERE id=?", t["id"])["community_id"] == \
        one(b, "SELECT id FROM communities WHERE canonical_ap_id=?", ME["uri"])["id"]
    # Your account's posts are a page of their own, but not one to follow or post in.
    cpage = web.get(f"/c/{one(b, 'SELECT community_id FROM archived_threads WHERE id=?', t['id'])['community_id']}").text
    assert "Racks" in cpage and f'name="community" value="{ME["uri"]}"' not in cpage and "Live on server" not in cpage

