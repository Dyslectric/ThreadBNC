"""Reddit: connecting, following subreddits politely, and reposting to your own communities."""

from __future__ import annotations

import base64
import time
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient

from threadbnc.accounts import AccountError, Poster
from threadbnc.adapters import RemoteUnavailable, parse_community_ref, parse_thread_url
from threadbnc.reddit import RedditConnection
from threadbnc.vault import TokenVault
from threadbnc.web import create_app

from .conftest import DOMAIN

HOME = "home.test"


class FakeReddit:
    """Just enough of reddit.com's OAuth endpoints and oauth.reddit.com's API."""

    def __init__(self) -> None:
        self.apps = {"app1": "s3cret", "installed1": ""}
        self.codes = {"good-code"}
        self.refresh_ok = True
        self.requests: list[str] = []
        self.remaining = 500.0
        now = time.time()
        self.posts = {
            "p1": self._post("p1", "Link post", url="https://example.org/story", created=now - 3600),
            "p2": self._post("p2", "Text post", selftext="Some **text**\n\nsecond para", created=now - 1800),
        }
        self.comments = {
            "p1": [self._comment("c1", "p1", "top comment"),
                   self._comment("c2", "p1", "a reply", parent="t1_c1")],
            "p2": [],
        }
        self.more = False  # "load more comments" stub under p1
        self.subscribed = ["pics", "technology"]
        self.granted = "identity read mysubreddits vote submit edit"  # what a login is given
        self.votes: dict[str, int] = {}
        self.archived: set[str] = set()  # posts too old to reply to
        self.next_id = 0

    @staticmethod
    def _post(pid, title, url=None, selftext="", created=0.0):
        return {"id": pid, "name": f"t3_{pid}", "subreddit": "pics", "subreddit_id": "t5_pics", "title": title,
                "selftext": selftext, "is_self": url is None,
                "url": url or f"https://www.reddit.com/r/pics/comments/{pid}/x/",
                "author": "alice", "created_utc": created, "edited": False, "score": 42, "num_comments": 2,
                "locked": False, "stickied": False, "over_18": False, "removed_by_category": None,
                "permalink": f"/r/pics/comments/{pid}/x/"}

    @staticmethod
    def _comment(cid, pid, body, parent=None):
        return {"id": cid, "parent_id": parent or f"t3_{pid}", "body": body, "author": "bob",
                "created_utc": time.time() - 600, "edited": False, "score": 3, "replies": ""}

    def _tree(self, pid):
        by_parent: dict[str, list] = {}
        for c in self.comments[pid]:
            by_parent.setdefault(c["parent_id"], []).append(c)

        def children(parent):
            out = []
            for c in by_parent.get(parent, []):
                kids = children(f"t1_{c['id']}")
                out.append({"kind": "t1", "data": {**c, "replies": {"kind": "Listing", "data": {"children": kids}}
                                                   if kids else ""}})
            return out

        top = children(f"t3_{pid}")
        if self.more:
            top.append({"kind": "more", "data": {"count": 12, "children": ["zz1", "zz2"]}})
        return top

    def handle(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        self.requests.append(f"{request.method} {url.host}{url.path}")
        headers = {"x-ratelimit-remaining": str(self.remaining), "x-ratelimit-reset": "120"}
        if url.host == "www.reddit.com" and url.path == "/api/v1/access_token":
            cid, _, secret = base64.b64decode(request.headers["authorization"].split()[1]).decode().partition(":")
            if self.apps.get(cid) != secret:
                return httpx.Response(401, json={"message": "Unauthorized", "error": 401})
            form = parse_qs(request.content.decode())
            grant = form["grant_type"][0]
            if grant == "authorization_code":
                if form["code"][0] not in self.codes:
                    return httpx.Response(400, json={"error": "invalid_grant"})
                return httpx.Response(200, json={"access_token": "user-tok", "refresh_token": "refresh-xyz",
                                                 "expires_in": 3600, "scope": self.granted})
            if grant == "refresh_token":
                if not self.refresh_ok:
                    return httpx.Response(400, json={"error": "invalid_grant"})
                return httpx.Response(200, json={"access_token": "user-tok", "expires_in": 3600})
            return httpx.Response(200, json={"access_token": "app-tok", "expires_in": 3600})
        if url.host == "www.reddit.com" and url.path == "/api/v1/revoke_token":
            return httpx.Response(204)
        if url.host == "www.reddit.com" and url.path.startswith("/r/pics/s/"):
            return httpx.Response(301, headers={"location": "https://www.reddit.com/r/pics/comments/p2/x/"})
        assert url.host == "oauth.reddit.com", url
        assert request.headers["authorization"].startswith("bearer ")
        path, q = url.path, parse_qs(url.query.decode())
        if request.method == "POST":
            return self.write(path, {k: v[0] for k, v in parse_qs(request.content.decode()).items()}, headers)
        if path == "/api/v1/me":
            return httpx.Response(200, json={"name": "dave"}, headers=headers)
        if path == "/r/pics/about":
            return httpx.Response(200, json={"kind": "t5", "data": {"display_name": "pics", "title": "Pictures",
                                                                    "name": "t5_pics"}}, headers=headers)
        if path == "/r/nope/about":
            return httpx.Response(404, json={"error": 404}, headers=headers)
        if path == "/r/pics/new":
            posts = sorted(self.posts.values(), key=lambda p: -p["created_utc"])
            return httpx.Response(200, json={"kind": "Listing", "data": {
                "after": None, "children": [{"kind": "t3", "data": p} for p in posts]}}, headers=headers)
        if path == "/api/info":
            pid = q["id"][0][3:]
            kids = [{"kind": "t3", "data": self.posts[pid]}] if pid in self.posts else []
            return httpx.Response(200, json={"kind": "Listing", "data": {"children": kids}}, headers=headers)
        if path.startswith("/comments/"):
            pid = path.split("/")[2]
            return httpx.Response(200, json=[
                {"kind": "Listing", "data": {"children": [{"kind": "t3", "data": self.posts[pid]}]}},
                {"kind": "Listing", "data": {"children": self._tree(pid)}}], headers=headers)
        if path == "/subreddits/mine/subscriber":
            return httpx.Response(200, json={"kind": "Listing", "data": {"after": None, "children": [
                {"kind": "t5", "data": {"display_name": n, "title": n.title(), "name": f"t5_{n}"}}
                for n in self.subscribed]}}, headers=headers)
        return httpx.Response(404, json={"error": 404}, headers=headers)

    def _post_of(self, thing: str) -> str:
        if thing.startswith("t3_"):
            return thing[3:]
        return next(pid for pid, cs in self.comments.items() for c in cs if c["id"] == thing[3:])

    def write(self, path, form, headers):
        assert form["api_type"] == "json"
        if "vote" not in self.granted.split():
            return httpx.Response(403, json={"message": "Forbidden", "error": 403}, headers=headers)

        def things(kind, data):
            return httpx.Response(200, json={"json": {"errors": [], "data": {"things": [{"kind": kind, "data": data}]}}},
                                  headers=headers)
        if path == "/api/vote":
            self.votes[form["id"]] = int(form["dir"])
            return httpx.Response(200, json={}, headers=headers)
        if path == "/api/comment":
            pid = self._post_of(form["thing_id"])
            if pid in self.archived:
                return httpx.Response(200, json={"json": {"errors": [
                    ["TOO_OLD", "that's a piece of history now; it's too late to reply to it", "parent"]]}},
                    headers=headers)
            self.next_id += 1
            c = {**self._comment(f"n{self.next_id}", pid, form["text"], parent=form["thing_id"]), "author": "dave"}
            self.comments[pid].append(c)
            return things("t1", {**c, "subreddit": "pics", "link_id": f"t3_{pid}"})
        if path == "/api/editusertext":
            kind, tid = form["thing_id"][:2], form["thing_id"][3:]
            if kind == "t3":
                self.posts[tid].update(selftext=form["text"], edited=time.time())
                return things("t3", self.posts[tid])
            pid = self._post_of(form["thing_id"])
            c = next(c for c in self.comments[pid] if c["id"] == tid)
            c.update(body=form["text"], edited=time.time())
            return things("t1", {**c, "subreddit": "pics", "link_id": f"t3_{pid}"})
        if path == "/api/del":
            kind, tid = form["id"][:2], form["id"][3:]
            if kind == "t3":
                self.posts[tid].update(selftext="[deleted]", author="[deleted]", removed_by_category="deleted")
            else:
                pid = self._post_of(form["id"])
                next(c for c in self.comments[pid] if c["id"] == tid).update(body="[deleted]", author="[deleted]")
            return httpx.Response(200, json={}, headers=headers)
        if path == "/api/submit":
            self.next_id += 1
            pid = f"s{self.next_id}"
            self.posts[pid] = {**self._post(pid, form["title"], url=form.get("url"), selftext=form.get("text", ""),
                                            created=time.time()), "author": "dave", "subreddit": form["sr"],
                               "num_comments": 0}
            self.comments[pid] = []
            return httpx.Response(200, json={"json": {"errors": [], "data": {"id": pid, "name": f"t3_{pid}"}}},
                                  headers=headers)
        return httpx.Response(404, json={"error": 404}, headers=headers)


@pytest.fixture
def reddit(bouncer, settings):
    fake = FakeReddit()
    conn = RedditConnection(bouncer.db, TokenVault("test-key", settings.data_dir), min_interval=0,
                            transport=httpx.MockTransport(fake.handle))
    bouncer.reddit = conn
    bouncer.reddit_adapter.reader = conn
    return fake


def one(b, sql, *args):
    with b.db.connect() as conn:
        return conn.execute(sql, args).fetchone()


def col(b, sql, *args):
    with b.db.connect() as conn:
        return [r[0] for r in conn.execute(sql, args).fetchall()]


def logged_in(settings, bouncer):
    client = TestClient(create_app(settings, bouncer))
    client.post("/login", data={"password": "pw"})
    return client


# -- parsing -------------------------------------------------------------------------------

@pytest.mark.parametrize("url,local", [
    ("https://www.reddit.com/r/pics/comments/abc123/some_title/", "abc123"),
    ("https://old.reddit.com/r/pics/comments/abc123/some_title/def456/", "abc123"),
    ("reddit.com/comments/abc123", "abc123"),
    ("https://redd.it/abc123", "abc123"),
    ("https://www.reddit.com/gallery/abc123", "abc123"),
])
def test_reddit_post_urls(url, local):
    ref = parse_thread_url(url)
    assert (ref.domain, ref.kind, ref.local_id) == ("reddit.com", "post", local)


def test_subreddit_refs():
    for text in ("r/pics", "/r/pics", "https://www.reddit.com/r/pics/", "old.reddit.com/r/pics/top", "pics@reddit.com"):
        ref = parse_community_ref(text)
        assert (ref.domain, ref.name, ref.qualified) == ("reddit.com", "pics", "pics"), text
    with pytest.raises(ValueError):
        parse_community_ref("https://www.reddit.com/user/someone")


# -- connecting -----------------------------------------------------------------------------

def test_following_a_subreddit_needs_a_connection(bouncer, reddit):
    with pytest.raises(RemoteUnavailable, match="isn't connected"):
        bouncer.follow_community("r/pics")
    assert reddit.requests == []


def test_app_only_connection_stores_the_secret_encrypted(bouncer, reddit):
    bouncer.reddit.connect_app("app1", "s3cret")
    raw = one(bouncer, "SELECT value FROM app_settings WHERE key='reddit_connection'")[0]
    assert "s3cret" not in raw and bouncer.reddit.status()["mode"] == "app"


def test_wrong_app_secret_is_refused(bouncer, reddit):
    from threadbnc.reddit import RedditError
    with pytest.raises(RedditError, match="didn't accept"):
        bouncer.reddit.connect_app("app1", "wrong")
    assert bouncer.reddit.status() is None


def test_installed_app_without_secret(bouncer, reddit):
    bouncer.reddit.connect_app("installed1", "")
    assert bouncer.follow_community("r/pics")


def test_log_in_with_reddit(settings, bouncer, reddit):
    client = logged_in(settings, bouncer)
    r = client.post("/reddit/login", data={"client_id": "app1", "client_secret": "s3cret"}, follow_redirects=False)
    assert r.headers["location"] == "/reddit/go"
    page = client.get("/reddit/go").text
    authorize = next(line for line in page.splitlines() if "api/v1/authorize" in line)
    state = parse_qs(urlparse(authorize.split('url=')[1].split('"')[0].replace("&amp;", "&")).query)["state"][0]
    # Reddit sends the browser back without our (SameSite=strict) cookie.
    anon = TestClient(client.app)
    assert anon.get(f"/reddit/callback?state=nope&code=good-code").status_code == 400
    r = anon.get(f"/reddit/callback?state={state}&code=good-code")
    assert r.status_code == 200
    status = bouncer.reddit.status()
    assert status["mode"] == "user" and status["username"] == "dave"
    raw = one(bouncer, "SELECT value FROM app_settings WHERE key='reddit_connection'")[0]
    assert "refresh-xyz" not in raw
    # The state works once.
    assert anon.get(f"/reddit/callback?state={state}&code=good-code").status_code == 400


def test_revoked_sign_in_stops_asking_until_reconnected(bouncer, reddit):
    bouncer.reddit.start_login("app1", "s3cret", "http://x/reddit/callback")
    state = bouncer.reddit.pending_url().split("state=")[1].split("&")[0]
    assert bouncer.reddit.finish_login(state, "good-code", None)[0]
    bouncer.reddit._token = None  # access token expired
    reddit.refresh_ok = False
    with pytest.raises(RemoteUnavailable):
        bouncer.follow_community("r/pics")
    assert bouncer.reddit.status()["status"] == "needs_login"
    before = len(reddit.requests)
    with pytest.raises(RemoteUnavailable, match="connect again"):
        bouncer.follow_community("r/pics")
    assert len(reddit.requests) == before  # no more token requests


def test_rate_limit_headers_pause_requests(bouncer, reddit):
    bouncer.reddit.connect_app("app1", "s3cret")
    reddit.remaining = 3
    bouncer.reddit_adapter.fetch_post("p1")
    before = len(reddit.requests)
    with pytest.raises(RemoteUnavailable, match="rate limit"):
        bouncer.reddit_adapter.fetch_post("p1")
    assert len(reddit.requests) == before


# -- following -----------------------------------------------------------------------------

def test_follow_a_subreddit_conservatively(bouncer, reddit):
    bouncer.reddit.connect_app("app1", "s3cret")
    cid = bouncer.follow_community("r/pics", None, 30, backfill=True)
    f = one(bouncer, "SELECT * FROM community_follows WHERE community_id=?", cid)
    assert f["poll_interval_minutes"] == 60 and f["source_domain"] == "reddit.com"
    assert bouncer.poll_follow(cid) == 2
    assert col(bouncer, "SELECT canonical_ap_id FROM communities") == ["https://www.reddit.com/r/pics"]
    link = one(bouncer, "SELECT o.*, r.url, r.body FROM objects o JOIN revisions r ON r.object_id=o.id "
                        "WHERE canonical_ap_id=?", "https://www.reddit.com/r/pics/comments/p1/")
    assert link["url"] == "https://example.org/story" and link["body"] is None and link["score"] == 42
    text = one(bouncer, "SELECT r.url, r.body FROM objects o JOIN revisions r ON r.object_id=o.id "
                        "WHERE canonical_ap_id=?", "https://www.reddit.com/r/pics/comments/p2/")
    assert text["url"] is None and text["body"].startswith("Some **text**")
    reply = one(bouncer, "SELECT parent_id FROM objects WHERE canonical_ap_id LIKE '%/_/c2/'")[0]
    assert reply == one(bouncer, "SELECT id FROM objects WHERE canonical_ap_id LIKE '%/_/c1/'")[0]
    # New threads are re-checked no more often than the subreddit.
    nxt = col(bouncer, "SELECT next_check_at FROM archived_threads")
    from threadbnc.db import parse_ts, utcnow
    assert all((parse_ts(n) - parse_ts(utcnow())).total_seconds() > 55 * 60 for n in nxt)
    # Can't be set faster than every 10 minutes.
    bouncer.update_follow(cid, 1, 30)
    assert one(bouncer, "SELECT poll_interval_minutes FROM community_follows")[0] == 10


def test_unchanged_thread_costs_one_request(bouncer, reddit):
    bouncer.reddit.connect_app("app1", "s3cret")
    tid = bouncer.ingest_url("https://www.reddit.com/r/pics/comments/p1/x/")
    before = len(reddit.requests)
    bouncer.sync_thread(tid)
    assert reddit.requests[before:] == ["GET oauth.reddit.com/api/info"]


def test_comments_hidden_behind_load_more_are_not_missing(bouncer, reddit):
    bouncer.reddit.connect_app("app1", "s3cret")
    tid = bouncer.ingest_url("https://www.reddit.com/r/pics/comments/p1/x/")
    reddit.comments["p1"] = reddit.comments["p1"][:1]  # c2 dropped out of the first 200...
    reddit.more = True                                 # ...behind "load more"
    bouncer.sync_thread(tid, force=True)
    assert col(bouncer, "SELECT event_type FROM state_events WHERE event_type='missing'") == []
    reddit.more = False  # a complete tree without it: now it's gone
    bouncer.sync_thread(tid, force=True)
    assert col(bouncer, "SELECT event_type FROM state_events WHERE event_type='missing'") == ["missing"]


def test_share_links_are_followed_to_the_post(bouncer, reddit):
    bouncer.reddit.connect_app("app1", "s3cret")
    tid = bouncer.ingest_url("https://www.reddit.com/r/pics/s/AbC123")
    root = one(bouncer, "SELECT o.canonical_ap_id FROM archived_threads t JOIN objects o ON o.id=t.root_object_id "
                        "WHERE t.id=?", tid)[0]
    assert root == "https://www.reddit.com/r/pics/comments/p2/"


def test_follow_subscriptions_from_the_reddit_page(settings, bouncer, reddit):
    bouncer.reddit.start_login("app1", "s3cret", "http://x/reddit/callback")
    state = bouncer.reddit.pending_url().split("state=")[1].split("&")[0]
    bouncer.reddit.finish_login(state, "good-code", None)
    client = logged_in(settings, bouncer)
    page = client.get("/reddit?subs=1").text
    assert "r/pics" in page and "r/technology" in page
    client.post("/reddit/follow", data={"names": ["pics"], "retention_days": "7"})
    assert col(bouncer, "SELECT c.name FROM community_follows f JOIN communities c ON c.id=f.community_id") == ["pics"]
    assert "following" in client.get("/reddit?subs=1").text


def test_subreddits_show_as_r_name(settings, bouncer, reddit):
    bouncer.reddit.connect_app("app1", "s3cret")
    cid = bouncer.follow_community("r/pics", None, 30, backfill=True)
    bouncer.poll_follow(cid)
    client = logged_in(settings, bouncer)
    home = client.get("/").text
    assert "r/pics" in home and "!pics" not in home and "↻ Repost" in home
    assert "✎ New post" not in client.get(f"/c/{cid}").text


# -- reposting -----------------------------------------------------------------------------

@pytest.fixture
def poster(bouncer, settings):
    return Poster(bouncer, TokenVault(settings.credentials_key, settings.data_dir))  # the same key as the app


def test_repost_a_reddit_post_to_your_community(settings, bouncer, reddit, server, poster):
    bouncer.reddit.connect_app("app1", "s3cret")
    src = bouncer.ingest_url("https://www.reddit.com/r/pics/comments/p2/x/")
    mine = bouncer.follow_community(f"!math@{DOMAIN}")
    account = poster.add(HOME, "dave", "hunter2")
    client = logged_in(settings, bouncer)
    form = client.get(f"/t/{src}/repost").text
    assert "cross-posted from: https://www.reddit.com/r/pics/comments/p2/" in form and "&gt; Some **text**" in form
    draft = poster.repost_draft(src)
    r = client.post(f"/t/{src}/repost", data={"community_id": str(mine), "title": draft["title"],
                                              "url": draft["url"], "body": draft["body"]}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/t/")
    new = int(r.headers["location"].split("/")[2])
    row = one(bouncer, "SELECT r.title, r.body, o.canonical_ap_id, o.dupe_key FROM archived_threads t "
                       "JOIN objects o ON o.id=t.root_object_id JOIN revisions r ON r.object_id=o.id "
                       "AND r.seq=o.revision_count WHERE t.id=?", new)
    assert row["title"] == "Text post" and row["canonical_ap_id"].startswith(f"https://{HOME}/post/")
    assert row["body"].splitlines()[0] == "cross-posted from: https://www.reddit.com/r/pics/comments/p2/"
    # Shown together with the original, which records the repost.
    orig_key = one(bouncer, "SELECT o.dupe_key FROM archived_threads t JOIN objects o ON o.id=t.root_object_id "
                            "WHERE t.id=?", src)[0]
    assert row["dupe_key"] == orig_key
    assert col(bouncer, "SELECT event_type FROM state_events WHERE thread_id=? AND event_type='reposted'", src)
    page = client.get(f"/t/{src}").text
    assert "Posted 2 times" in page and "You reposted this to !math@lemmy.test" in page
    # The next repost remembers where the last one went.
    assert f'value="{mine}" selected' in client.get(f"/t/{src}/repost").text
    assert account


def test_link_repost_keeps_the_link(bouncer, reddit, poster):
    bouncer.reddit.connect_app("app1", "s3cret")
    src = bouncer.ingest_url("https://www.reddit.com/r/pics/comments/p1/x/")
    draft = poster.repost_draft(src)
    assert draft["url"] == "https://example.org/story"
    assert draft["body"] == "cross-posted from: https://www.reddit.com/r/pics/comments/p1/"


def login_with_reddit(bouncer, poster):
    bouncer.reddit.start_login("app1", "s3cret", "http://x/reddit/callback")
    state = bouncer.reddit.pending_url().split("state=")[1].split("&")[0]
    assert bouncer.reddit.finish_login(state, "good-code", None)[0]
    poster.sync_reddit_account()
    return poster.reddit_account()


def obj(bouncer, ap_id):
    return one(bouncer, "SELECT * FROM objects WHERE canonical_ap_id=?", ap_id)


P1 = "https://www.reddit.com/r/pics/comments/p1/"


def test_reddit_account_is_used_for_reddit_only(settings, bouncer, reddit, poster):
    lemmy = poster.add(HOME, "dave", "hunter2")
    me = login_with_reddit(bouncer, poster)
    assert me.handle == "u/dave" and me.actor_ap_id == "https://www.reddit.com/user/dave"
    assert poster.default().id == lemmy.id  # never the default, never in the switcher
    client = logged_in(settings, bouncer)
    client.post("/accounts/act-as", data={"account_id": str(me.id)})
    page = client.get("/accounts").text
    assert "u/dave" in page and "Used automatically for anything on Reddit" in page
    switcher = page.split('aria-label="Posting as"')[1].split("</select>")[0]
    assert "u/dave" not in switcher and "dave@home.test" in switcher


def test_votes_and_replies_on_reddit_go_as_the_reddit_account(bouncer, reddit, poster):
    lemmy = poster.add(HOME, "dave", "hunter2")
    me = login_with_reddit(bouncer, poster)
    tid = bouncer.ingest_url(P1)
    poster.vote(lemmy, obj(bouncer, P1)["id"], 1)  # acting as the Lemmy account: switched automatically
    assert reddit.votes == {"t3_p1": 1}
    assert col(bouncer, "SELECT account_id FROM my_votes") == [me.id]
    parent = obj(bouncer, P1 + "_/c1/")["id"]
    oid = poster.reply(lemmy, tid, "hello from ThreadBNC", parent)
    row = one(bouncer, "SELECT * FROM objects WHERE id=?", oid)
    assert row["canonical_ap_id"] == P1 + "_/n1/" and row["parent_id"] == parent
    bouncer.sync_thread(tid, force=True)  # the next check sees the same comment, not a new one
    assert col(bouncer, "SELECT id FROM objects WHERE canonical_ap_id LIKE '%/_/n1/'") == [oid]
    assert col(bouncer, "SELECT event_type FROM state_events WHERE event_type='missing'") == []


def test_edit_and_delete_your_reddit_comment(bouncer, reddit, poster):
    login_with_reddit(bouncer, poster)
    lemmy = poster.add(HOME, "dave", "hunter2")
    tid = bouncer.ingest_url(P1)
    oid = poster.reply(lemmy, tid, "first try")
    poster.edit(lemmy, oid, "second try")
    assert [r for r in col(bouncer, "SELECT body FROM revisions WHERE object_id=? ORDER BY seq", oid)] == \
        ["first try", "second try"]
    poster.delete(lemmy, oid)
    assert "delete_requested" in col(bouncer, "SELECT event_type FROM state_events WHERE object_id=?", oid)
    with pytest.raises(AccountError, match="can't bring back"):
        poster.delete(lemmy, oid, deleted=False)
    theirs = obj(bouncer, P1 + "_/c1/")["id"]
    with pytest.raises(AccountError, match="Only the author"):
        poster.edit(lemmy, theirs, "not mine")


def test_post_to_a_subreddit_and_reddit_titles_stay_fixed(bouncer, reddit, poster):
    me = login_with_reddit(bouncer, poster)
    cid = bouncer.follow_community("r/pics")
    tid = poster.submit(me, cid, "My photo", "look at this")
    root = one(bouncer, "SELECT o.* FROM archived_threads t JOIN objects o ON o.id=t.root_object_id WHERE t.id=?", tid)
    assert root["canonical_ap_id"] == "https://www.reddit.com/r/pics/comments/s1/"
    poster.edit(me, root["id"], "better text", title="My photo")
    assert reddit.posts["s1"]["selftext"] == "better text"
    with pytest.raises(AccountError, match="title or link"):
        poster.edit(me, root["id"], "better text", title="A new title")


def test_repost_from_your_server_to_a_subreddit(settings, bouncer, reddit, server, poster):
    lemmy = poster.add(HOME, "dave", "hunter2")
    login_with_reddit(bouncer, poster)
    sub = bouncer.follow_community("r/pics")
    server.add_post("1", "Local news", "what happened here")
    src = bouncer.ingest_url(f"https://{DOMAIN}/post/1")
    client = logged_in(settings, bouncer)
    form = client.get(f"/t/{src}/repost").text
    assert "Subreddits (as u/dave)" in form and f'<option value="{sub}"' in form
    draft = poster.repost_draft(src)
    new = poster.repost(lemmy, src, sub, draft["title"], draft["body"], draft["url"])
    posted = next(p for p in reddit.posts.values() if p["author"] == "dave")
    assert posted["subreddit"] == "pics" and posted["selftext"].startswith(f"cross-posted from: https://{DOMAIN}/post/1")
    assert new != src
    assert "You reposted this to r/pics as u/dave" in client.get(f"/t/{src}").text


def test_a_group_with_a_reddit_copy_votes_on_both(settings, bouncer, reddit, server, poster):
    lemmy = poster.add(HOME, "dave", "hunter2")
    login_with_reddit(bouncer, poster)
    bouncer.ingest_url(P1)  # links https://example.org/story
    server.add_post("1", "Link post", "")
    server.edit_post("1", url="https://example.org/story")
    lemmy_tid = bouncer.ingest_url(f"https://{DOMAIN}/post/1")
    client = logged_in(settings, bouncer)
    page = client.get(f"/t/{lemmy_tid}").text
    assert "Posted 2 times" in page and "Upvote as dave@home.test and u/dave on all 2 copies" in page
    lemmy_post = one(bouncer, "SELECT root_object_id FROM archived_threads WHERE id=?", lemmy_tid)[0]
    client.post(f"/o/{lemmy_post}/vote", data={"score": "1", "also": [str(obj(bouncer, P1)["id"])]})
    assert reddit.votes == {"t3_p1": 1} and server.votes == {("dave", "post:1"): 1}
    assert lemmy


def test_reddit_thread_page_offers_voting_and_commenting(settings, bouncer, reddit, poster):
    login_with_reddit(bouncer, poster)
    tid = bouncer.ingest_url(P1)
    page = logged_in(settings, bouncer).get(f"/t/{tid}").text
    assert "Comment as u/dave" in page and "Upvote as u/dave" in page


def test_archived_reddit_thread_says_why(bouncer, reddit, poster):
    me = login_with_reddit(bouncer, poster)
    tid = bouncer.ingest_url(P1)
    reddit.archived.add("p1")
    with pytest.raises(AccountError, match="archived"):
        poster.reply(me, tid, "too late")


def test_read_only_sign_in_asks_to_connect_again(settings, bouncer, reddit, poster):
    reddit.granted = "identity read mysubreddits"  # a login from before writing was asked for
    lemmy = poster.add(HOME, "dave", "hunter2")
    assert login_with_reddit(bouncer, poster) is None
    tid = bouncer.ingest_url(P1)
    with pytest.raises(AccountError, match="Connect Reddit again"):
        poster.vote(lemmy, obj(bouncer, P1)["id"], 1)
    page = logged_in(settings, bouncer).get(f"/t/{tid}").text
    assert "Upvote as" not in page and "Log in with Reddit" in page
    assert reddit.votes == {}


def test_app_only_connection_cannot_write(bouncer, reddit, poster):
    bouncer.reddit.connect_app("app1", "s3cret")
    lemmy = poster.add(HOME, "dave", "hunter2")
    tid = bouncer.ingest_url(P1)
    with pytest.raises(AccountError, match="log in with Reddit"):
        poster.reply(lemmy, tid, "hi")
    with pytest.raises(AccountError, match="log in with Reddit"):
        poster.submit(lemmy, one(bouncer, "SELECT community_id FROM archived_threads WHERE id=?", tid)[0], "t")


def test_disconnecting_removes_the_reddit_account(settings, bouncer, reddit, poster):
    me = login_with_reddit(bouncer, poster)
    bouncer.ingest_url(P1)
    poster.vote(me, obj(bouncer, P1)["id"], 1)
    client = logged_in(settings, bouncer)
    client.post("/reddit/disconnect")
    assert col(bouncer, "SELECT username FROM accounts") == [] and col(bouncer, "SELECT * FROM my_votes") == []
    login_with_reddit(bouncer, poster)
    poster.remove(poster.reddit_account().id)  # removing it on the Accounts page disconnects too
    assert bouncer.reddit.status() is None and col(bouncer, "SELECT username FROM accounts") == []
