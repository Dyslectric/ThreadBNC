"""Signed in to a booru: its comments, posting, moderating and administrating (booru.py)."""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient

from threadbnc import booru
from threadbnc.web import create_app

from .test_booru import HOST, OTHER, FakeHttp

HOME = "home.test"
BOT = "@booru_bot@booru.test"


def said(status, ok, message, **more):
    return httpx.Response(status, json={"ok": ok, "message": message, **more})


class FakeBooru(FakeHttp):
    """booru.test as a fedbooru that apps can sign in to: it takes `!login <code>` from
    whoever home.test's private messages (the fake server's) say sent it. shrine.test is
    an older one: it has no such addresses, and answers its own pages only as HTML."""

    def __init__(self, server):
        super().__init__()
        self.server = server
        self.started = {}  # secret -> code
        self.begun = 0
        self.sessions = {}  # token -> {"handle", "recent"}
        self.forms = []  # (path, fields) sent as someone
        self.seen = []  # every request made of booru.test: (method, path)
        self.files = []
        self.role = "admin"
        self.claim_as = None  # another account answering the sign-in
        self.comments = []
        self.says_how_long = True  # (a booru from before its admin page said for how long a sign-in is recent doesn't)

    def _claimed(self, secret):
        code = self.started.get(secret)
        for sender, _, body, _ in self.server.messages_sent:
            if code and body == f"!login {code}":
                return self.claim_as or sender
        return None

    def send(self, method, url, *, headers=None, content=None, throttle=True):
        where = urlparse(url)
        headers = headers or {}
        if where.netloc == HOST:
            self.seen.append((method, where.path))
        token = headers.get("Authorization", "").removeprefix("Bearer ") or None
        wants_json = headers.get("Accept") == "application/json"
        if where.netloc != HOST:
            if wants_json and (method == "POST" or where.path.startswith("/posts/")):
                assert throttle is False
                return httpx.Response(403 if method == "POST" else 200, text="<!doctype html><p>a page</p>")
            return super().send(method, url, headers=headers, content=content, throttle=throttle)
        if not wants_json:
            return super().send(method, url, headers=headers, content=content, throttle=throttle)
        assert throttle is False
        fields = {k: v[0] for k, v in parse_qs((content or b"").decode("latin-1"), keep_blank_values=True).items()}
        path = where.path
        if path == "/api/login":
            self.begun += 1
            secret = f"{self.begun:064x}"
            self.started[secret] = f"K7QD-{self.begun:04d}"
            return httpx.Response(200, json={"secret": secret, "code": self.started[secret], "bot": BOT,
                                             "bluesky_bot": None, "expires_in": 900})
        if path == "/api/login/state":
            who = self._claimed(fields["secret"])
            if fields["secret"] not in self.started:
                return httpx.Response(200, json={"state": "gone"})
            return httpx.Response(200, json={"state": "claimed", "handle": f"@{who}@{HOME}", "key": f"ap:https://{HOME}/u/{who}"}
                                  if who else {"state": "waiting"})
        if path == "/api/login/confirm":
            who = self._claimed(fields["secret"])
            if not who or fields["secret"] not in self.started:
                return said(410, False, "That sign-in has expired, or nobody has claimed it yet.")
            del self.started[fields["secret"]]
            session = f"{len(self.sessions) + 1:064d}"
            self.sessions[session] = {"handle": f"@{who}@{HOME}", "recent": True}
            return httpx.Response(200, json={"session": session, "expires_in": 2592000})
        mine = self.sessions.get(token) if token else None
        if token and mine is None:
            return said(401, False, "This session has ended. Sign in again.")
        staff, admin = self.role in ("moderator", "admin"), self.role == "admin"
        me = {"name": mine["handle"], "staff": staff, "admin": admin} if mine else {"name": None, "staff": False, "admin": False}
        if method == "POST":
            if mine is None:
                return said(403, False, "Writes must come from this site.")
            if path == "/logout":
                del self.sessions[token]
                return httpx.Response(303, headers={"location": "/posts"})
            if path.startswith("/admin/") and not mine["recent"]:
                return said(403, False, "Admin actions need a sign-in from the last 15 minutes. Sign in again to continue.",
                            stale=True, location="/login?next=%2Fadmin")
            if path == "/upload" and headers["Content-Type"].startswith("multipart/"):
                self.files.append((headers["Content-Type"].split(";")[0], content))
                return said(200, True, "Received 1 image. It's being processed.", location="/submissions")
            self.forms.append((path, fields))
            if path == "/posts/1/comment":
                if not fields.get("body", "").strip():
                    return said(422, False, "Write something first")
                self.comments.append({"id": len(self.comments) + 1, "body": fields["body"], "parent": fields.get("parent")})
                return said(200, True, "Posted your comment.", location=f"/posts/1#comment-{len(self.comments)}")
            if path == "/posts/1/remove" and not fields.get("reason"):
                return said(422, False, "Give a reason; it's shown in the public modlog")
            return said(200, True, f"Done: {path}.", location="/posts")
        if path == "/api/me":
            return httpx.Response(200, json={**me, "handle": mine["handle"], "recent": mine["recent"],
                                             "recent_for": 800 if mine["recent"] else 0})
        if path == "/posts/1":
            comments = [{"id": c["id"], "author": "@dave@home.test", "handle": None, "author_url": f"https://{HOME}/u/dave",
                         "account": None, "from": None, "link": None, "when": "2026-10-03T10:00:00Z", "date": "2026-10-03",
                         "body": c["body"], "gone": None, "colour": 1 if c["parent"] else 0,
                         "descendants": 0, "closes": 1, "mine": bool(mine), "can_delete": bool(mine), "pending": False,
                         "can_approve": False} for c in self.comments]
            if len(comments) == 2:  # the second answers the first: its box is inside the first's
                comments[0].update(descendants=1, closes=0)
                comments[1]["closes"] = 2
            return httpx.Response(200, json={
                "site_name": "Pictures", "me": me, "title": "Post #1", "id": 1, "status_note": None,
                "file_url": "/media/original/ab/cd/1.png", "width": 800, "height": 600, "file_size": "200 KB", "format": "png",
                "rating": "safe", "sensitive": False, "source": "https://src.test/a", "description": None,
                "groups": [["artist", [{"name": "mika", "category": "artist", "count": 42}]],
                           ["general", [{"name": "cat", "category": "general", "count": 95}]]],
                "pools": [{"id": 7, "name": "A set", "position": 1, "len": 2, "prev": None, "next": 2}],
                "parent": None, "children": [], "submitter": "@alice@lemmy.test", "origin": None, "home": None,
                "created": "2026-09-01", "score": 3, "favs": 1, "locked": False, "featured": False,
                "comments_locked": False, "can_comment": bool(mine), "comments_held": False, "shared_with": None,
                "discussion": {"community": "!booru@booru.test", "open": None, "address": "https://booru.test/objects/page/1"},
                "moderation": {"decide": None, "remove": True, "restore": False, "lock": True, "thread": 4} if mine and staff else None,
                "editable": bool(mine), "published": True, "my_vote": 1 if mine else 0, "my_fav": False, "withdraw": None,
                "comments": comments, "max_comment": 5000})
        if path.startswith("/posts/"):
            return said(404, False, "not found")
        if mine is None:
            return httpx.Response(303, headers={"location": "/login"})
        if path == "/queue":
            if not staff:
                return said(403, False, "the queue is for moderators and admins")
            return httpx.Response(200, json={"me": me, "items": [{
                "submission": 9, "title": "A set", "submitter": "@bob@lemmy.test", "origin": "https://lemmy.test/post/9",
                "images": [{"card": {"id": 5, "thumb_url": "/media/thumb/ab/cd/5.webp?exp=1&sig=ab", "sensitive": False, "alt": "secret"},
                            "index": 1, "rating": "safe", "file_url": "/media/original/ab/cd/5.png?exp=1&sig=ab"}]}],
                "comments": [{"id": 3, "post": 1, "author": "Quiet", "account": "@q@lemmy.test", "from": None,
                              "date": "2026-10-03", "body": "held <b>one</b>"}],
                "pager": {"page": 1, "prev": None, "next": None}})
        if path == "/reports":
            return httpx.Response(200, json={"me": me, "reports": [{
                "id": 2, "reporter": "@eve@lemmy.test", "what": "a comment", "link": "/posts/1#comment-3",
                "reason": "rude", "when": "2026-10-03", "others": 1, "origin": None}]})
        if path == "/moderate":
            return httpx.Response(200, json={"me": me, "categories": ["general", "artist", "place"], "can_manage_categories": False,
                                             "bans": [{"kind": "account", "target": "@spam@bad.test", "reason": "spam", "until": "permanent"}],
                                             "can_trust_posts": True, "can_trust_comments": False, "open_reports": 2, "held_comments": 1})
        if path == "/admin":
            if not admin:
                return said(403, False, "administration is for admins")
            return httpx.Response(200, json={"me": me, "recent": mine["recent"], "minutes": 15, "unrestricted": False,
                                             **({"recent_for": 800 if mine["recent"] else 0} if self.says_how_long else {}),
                                             "peers": [{"domain": OTHER, "name": "Shrine", "posts": 2, "state": "read 2026-10-03"}],
                                             "comment_approval": True, "comment_approval_in_file": False})
        if path == "/upload":
            return httpx.Response(200, json={"me": me, "slots": [1, 2, 3], "more": 6, "uploads": True, "links_stored": False,
                                             "max_images": 10, "max_bytes": 20971520, "accept": "image/png,image/jpeg",
                                             "allowed_ratings": ["safe", "questionable"], "default_rating": "questionable", "max_mb": 20})
        if path == "/submissions":
            return httpx.Response(200, json={"me": me, "uploads": [{"title": "Mine", "when": "2026-10-03", "status": "processing", "message": None}],
                                             "submissions": [{"id": 4, "title": "Set", "web": True, "from": None, "withdrawn": False,
                                                              "posts": [[1, "published", True], [6, "waiting for approval", False]]}]})
        if path == "/settings":
            return httpx.Response(200, json={"me": me, "handle": mine["handle"], "name": "Dave", "show_handle": True, "max_name": 40})
        return said(404, False, "not found")


@pytest.fixture
def signed(settings, bouncer, server):
    """A client with one account (dave@home.test) and booru.test added; its bot can be messaged."""
    bouncer.http = FakeBooru(server)
    booru._index.clear()
    server.people[f"https://{HOST}/u/booru_bot"] = "777"
    client = TestClient(create_app(settings, bouncer))
    client.app.state.booru_spawn = lambda start: start()  # a sign-in is started at once, not in the background
    client.post("/login", data={"password": "pw"})
    client.post("/accounts", data={"server": HOME, "username": "dave", "password": "hunter2"})
    client.post("/booru/servers", data={"address": HOST})
    return client, bouncer.http


def sign_in(client):
    account = client.get("/booru").text.split('name="account"', 1)[1].split('value="', 1)[1].split('"', 1)[0]
    r = client.post("/booru/1/signin", data={"account": account, "next": "/booru/1/posts/1"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/booru/1/signin?next=%2Fbooru%2F1%2Fposts%2F1"
    return client.get(r.headers["location"], follow_redirects=False)


def test_signing_in_as_one_of_your_accounts(signed, server):
    client, fake = signed
    page = client.get("/booru/1/posts/1").text
    assert "Sign in" in page and 'action="/booru/1/signin"' in page and "dave@home.test</option>" in page
    assert "<h2>0 comments</h2>" in page and "Sign in to Pictures of booru.test from the menu above to comment" in page
    assert 'action="/booru/1/do/' not in page, "nothing to do there until you're signed in"

    r = sign_in(client)
    # the code went to the booru's bot as a private message from that account, and the booru has it
    assert server.messages_sent == [("dave", "777", "!login K7QD-0001", None)]
    assert r.status_code == 303 and r.headers["location"] == "/booru/1/posts/1"
    page = client.get("/booru/1/posts/1").text
    assert "Signed in to Pictures of booru.test as @dave@home.test." in page
    assert "Signed in as <strong>@dave@home.test</strong> · admin" in page
    for key in ("upload", "submissions", "queue", "reports", "moderate", "admin"):
        assert f'href="/booru/1/{key}"' in page
    # the token is kept encrypted, and sent as a bearer token
    with client.app.state.db.connect() as conn:
        kept = conn.execute("SELECT token_enc, pending_enc FROM booru_sessions").fetchone()
    assert kept["pending_enc"] is None and list(fake.sessions)[0] not in kept["token_enc"]

    r = client.post("/booru/1/signout", follow_redirects=False)
    assert r.status_code == 303 and not fake.sessions
    assert 'action="/booru/1/signin"' in client.get("/booru").text


def test_waiting_for_the_message_and_someone_elses(signed, server):
    client, fake = signed
    server.people.pop(f"https://{HOST}/u/booru_bot")  # home.test can't find the bot: nothing is sent
    account = client.get("/booru").text.split('name="account"', 1)[1].split('value="', 1)[1].split('"', 1)[0]
    r = client.post("/booru/1/signin", data={"account": account}, follow_redirects=True)
    assert "Couldn&#39;t send the sign-in message as dave@home.test" in r.text and 'action="/booru/1/signin"' in r.text

    # sent, but not there yet: the page waits, looking again by itself
    server.people[f"https://{HOST}/u/booru_bot"] = "777"
    assert server.messages_sent == []
    real = fake._claimed
    fake._claimed = lambda secret: None
    r = sign_in(client)
    assert r.status_code == 200 and "Signing in to Pictures of booru.test" in r.text
    assert 'data-booru-wait="/booru/1/signin/state"' in r.text and "dave@home.test" in r.text
    assert "Signing in…" in client.get("/booru").text
    # the code answered by another account signs nobody in
    fake._claimed, fake.claim_as = real, "mallory"
    page = client.get("/booru/1/signin", follow_redirects=True).text
    assert "Couldn&#39;t sign in to Pictures of booru.test: @mallory@home.test answered it, not dave@home.test." in page
    assert not fake.sessions and 'action="/booru/1/signin"' in page

    # a booru from before apps could sign in says so
    client.post("/booru/servers", data={"address": OTHER})
    r = client.post("/booru/2/signin", data={"account": account}, follow_redirects=True)
    assert "shrine.test can&#39;t be signed in to from here yet: it runs an older fedbooru." in r.text
    assert "Information" in client.get("/booru/2/posts/12").text, "its posts are still read through the public API"


def test_commenting_voting_and_editing(signed):
    client, fake = signed
    sign_in(client)
    page = client.get("/booru/1/posts/1").text
    # the post as its own page has it: tags with their counts, its pool, who submitted it
    assert f'src="https://{HOST}/media/original/ab/cd/1.png"' in page and "800×600 · png · 200 KB" in page
    assert '<li class="tag-artist"><a href="/booru?tags=mika">mika</a> <span class="booru-count">42</span>' in page
    assert 'href="/booru?tags=pool%3A7&amp;on=1"' in page and 'href="/booru/1/posts/2" rel="next"' in page
    assert "@alice@lemmy.test" in page and "!booru@booru.test" in page
    assert 'aria-pressed="true">▲</button>' in page and 'name="value" value="0"' in page  # your vote, taken back by the same button
    assert "Comment as @dave@home.test" in page and "<h2>Moderate</h2>" in page

    r = client.post("/booru/1/do/posts/1/comment", data={"body": "Lovely", "anchor": "comments"},
                    headers={"referer": "http://testserver/booru/1/posts/1"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/booru/1/posts/1#comments"
    assert fake.forms[-1] == ("/posts/1/comment", {"body": "Lovely"})
    client.post("/booru/1/do/posts/1/comment", data={"body": "Isn't it <i>just</i>", "parent": "1"}, follow_redirects=False)
    page = client.get("/booru/1/posts/1").text
    assert "Posted your comment." in page and "<h2>2 comments</h2>" in page
    assert "Isn&#39;t it &lt;i&gt;just&lt;/i&gt;" in page  # comments are text
    thread = page.split('id="comments"', 1)[1]
    assert thread.index('id="comment-1"') < thread.index('id="comment-2"') < thread.index("</div></div></details>")
    assert thread.count("</div></div></details>") == 2 and 'class="comment d1"' in thread

    # what the booru refuses is said as it says it
    r = client.post("/booru/1/do/posts/1/remove", data={"reason": ""}, follow_redirects=True)
    assert "Give a reason; it&#39;s shown in the public modlog" in r.text
    for path, fields in [("posts/1/vote", {"value": "-1"}), ("posts/1/favourite", {}),
                         ("posts/1/edit", {"tags": "+dog", "rating": "q", "source": "", "parent": ""}),
                         ("posts/1/report", {"reason": "wrong"}), ("posts/1/pool", {"pool": "pool#7"}),
                         ("posts/1/comments/2/delete", {}), ("submissions/4/lock-comments", {"reason": "heated"})]:
        assert client.post(f"/booru/1/do/{path}", data=fields, follow_redirects=False).status_code == 303
        assert fake.forms[-1] == (f"/{path}", fields)
    # only the booru's own forms can be sent
    for path in ("logout", "api/login", "posts/1", "admin/role/../../logout", "login/confirm"):
        assert client.post(f"/booru/1/do/{path}", data={}).status_code == 404
    assert client.post("/booru/9/do/posts/1/vote", data={"value": "1"}).status_code == 404


def test_posting(signed):
    client, fake = signed
    assert "Sign in to Pictures of booru.test to open that" in client.get("/booru/1/upload").text
    sign_in(client)
    page = client.get("/booru/1/upload").text
    assert 'type="file" name="files" multiple' in page and "questionable (its default)" in page
    assert '<option value="s">safe</option>' in page and "explicit" not in page
    r = client.post("/booru/1/upload", data={"title": "Cats", "tags": "cat"},
                    files=[("files", ("a.png", b"\x89PNG-one", "image/png")), ("files", ("b.png", b"\x89PNG-two", "image/png"))],
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/booru/1/submissions"
    kind, body = fake.files[-1]
    assert kind == "multipart/form-data"
    assert b'name="file1"; filename="a.png"' in body and b'name="file2"; filename="b.png"' in body and b"PNG-two" in body
    assert b'name="title"\r\n\r\nCats' in body and b'name="tags"\r\n\r\ncat' in body and b'name="mode"' not in body
    page = client.get(r.headers["location"]).text
    assert "Received 1 image." in page and "Mine" in page and "processing" in page
    assert 'href="/booru/1/posts/1">#1</a>' in page and "#6 <span" in page and 'href="/booru/1/posts/6"' not in page
    assert 'action="/booru/1/do/submissions/4/withdraw"' in page

    # by address instead: one a line
    client.post("/booru/1/upload", data={"links": "https://x.test/a.png\n\n https://x.test/b.png ", "rating": "s"})
    assert fake.forms[-1] == ("/upload", {"rating": "s", "mode": "link", "link1": "https://x.test/a.png",
                                         "link2": "https://x.test/b.png"})
    r = client.post("/booru/1/upload", data={"title": "Nothing"}, follow_redirects=True)
    assert "Choose pictures to upload, or give their addresses." in r.text


def test_moderating(signed):
    client, fake = signed
    sign_in(client)
    page = client.get("/booru/1/queue").text
    # pending pictures come by the signed links the booru gives for them
    assert f'src="https://{HOST}/media/thumb/ab/cd/5.webp?exp=1&amp;sig=ab"' in page and "by @bob@lemmy.test" in page
    assert 'action="/booru/1/do/queue/9/approve"' in page and 'action="/booru/1/do/queue/9/reject"' in page
    assert "held &lt;b&gt;one&lt;/b&gt;" in page and "(@q@lemmy.test)" in page
    assert 'action="/booru/1/do/posts/1/comments/3/approve"' in page
    client.post("/booru/1/do/queue/9/reject", data={"text": "not ours"})
    assert fake.forms[-1] == ("/queue/9/reject", {"text": "not ours"})

    page = client.get("/booru/1/reports").text
    assert 'href="/booru/1/posts/1#comment-3">A comment</a>' in page and "and 1 other" in page and "rude" in page
    client.post("/booru/1/do/reports/2/resolve", data={"note": "dealt with"})
    assert fake.forms[-1] == ("/reports/2/resolve", {"note": "dealt with"})

    page = client.get("/booru/1/moderate").text
    assert "<option>place</option>" in page and "@spam@bad.test" in page
    assert '<option value="posts">with submissions</option>' in page and 'value="comments"' not in page
    client.post("/booru/1/do/moderate/ban", data={"kind": "instance", "target": "bad.test", "duration": "7d", "reason": "spam"})
    assert fake.forms[-1][0] == "/moderate/ban" and fake.forms[-1][1]["duration"] == "7d"
    client.post("/booru/1/do/moderate/tags", data={"op": "alias", "from": "kitty", "to": "cat"})
    assert fake.forms[-1] == ("/moderate/tags", {"op": "alias", "from": "kitty", "to": "cat"})

    # a member gets neither the pages nor their links
    fake.role = "member"
    page = client.get("/booru/1/queue").text
    assert "The queue is for moderators and admins." in page and "Approval queue" not in page
    page = client.get("/booru/1/posts/1").text
    assert 'href="/booru/1/upload"' in page and 'href="/booru/1/queue"' not in page and "<h2>Moderate</h2>" not in page


def test_administrating_needs_a_recent_sign_in(signed, server):
    client, fake = signed
    sign_in(client)
    page = client.get("/booru/1/admin").text
    assert "Shrine" in page and "held for a moderator&#39;s approval" in page and "its config file says otherwise" in page
    assert len(server.messages_sent) == 1, "the sign-in is fresh: it isn't renewed"
    client.post("/booru/1/do/admin/role", data={"handle": "@dan@lemmy.test", "role": "moderator", "state": "on"})
    assert fake.forms[-1] == ("/admin/role", {"handle": "@dan@lemmy.test", "role": "moderator", "state": "on"})

    # an older sign-in: the booru refuses the form, so it's held, you're signed in again, and it's sent
    old = list(fake.sessions)[0]
    fake.sessions[old]["recent"] = False
    r = client.post("/booru/1/do/admin/instance", data={"domain": "spam.test", "policy": "blocked"},
                    headers={"referer": "http://testserver/booru/1/admin"}, follow_redirects=False)
    assert r.headers["location"] == "/booru/1/signin?next=%2Fbooru%2F1%2Fadmin"
    assert server.messages_sent[-1][2] == "!login K7QD-0002" and fake.forms[-1][0] == "/admin/role"
    r = client.get(r.headers["location"], follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/booru/1/admin"
    assert fake.forms[-1] == ("/admin/instance", {"domain": "spam.test", "policy": "blocked"})
    assert len(fake.sessions) == 2 and "Done: /admin/instance." in client.get("/booru/1/admin").text

    # opening the page with a sign-in that's nearly too old renews it by itself, meanwhile
    for session in fake.sessions.values():
        session["recent"] = False
    before = len(server.messages_sent)
    page = client.get("/booru/1/admin").text
    assert len(server.messages_sent) == before + 1
    assert len(fake.sessions) == 2, "the page didn't wait for it"
    assert fake.seen[-1] == ("POST", "/api/login") or fake.seen[-2:] == [("GET", "/admin"), ("POST", "/api/login")]
    assert fake.seen.count(("GET", "/api/me")) == 2, "asked at each sign-in, not by the page: it says how old the sign-in is"
    client.post("/booru/1/do/admin/peer", data={"domain": OTHER, "state": "off"})  # by now the booru has the message
    assert fake.forms[-1] == ("/admin/peer", {"domain": OTHER, "state": "off"}) and len(fake.sessions) == 3
    # a booru whose page only says whether the sign-in is recent: renewed once it isn't
    fake.says_how_long = False
    client.get("/booru/1/admin")
    assert len(server.messages_sent) == before + 1
    for session in fake.sessions.values():
        session["recent"] = False
    client.get("/booru/1/admin")
    assert len(server.messages_sent) == before + 2
    client.get("/booru/1/admin")

    # a session that has ended there is forgotten here
    fake.sessions.clear()
    page = client.get("/booru/1/admin").text
    assert "Your session on Pictures of booru.test has ended." in page and 'action="/booru/1/signin"' in page


def test_whose_account_answered():
    dave = "https://home.test/u/dave"
    assert booru.is_account({"key": "ap:https://home.test/u/dave", "handle": "@dave@home.test"}, dave, "dave")
    assert booru.is_account({"key": "ap:https://HOME.test/u/dave/"}, dave, "dave")
    assert not booru.is_account({"key": "ap:https://home.test/u/dave2", "handle": "@dave@home.test"}, dave, "dave")
    assert booru.is_account({"key": "at:did:plc:abc123"}, "at://did:plc:abc123/app.bsky.actor.profile/self", "dave.bsky.social")
    assert not booru.is_account({"key": "at:did:plc:other"}, "https://bsky.app/profile/did:plc:abc123", "dave.bsky.social")
    assert booru.is_account({"key": "at:did:plc:abc123", "handle": "@Dave.bsky.social"}, "", "dave.bsky.social")
    assert not booru.is_account({"key": "", "handle": "@dave@home.test"}, dave, "dave")


def test_settings_and_links(signed):
    client, fake = signed
    sign_in(client)
    page = client.get("/booru/1/settings").text
    assert 'name="name" value="Dave"' in page and 'name="show_handle" value="on" checked' in page
    client.post("/booru/1/do/settings", data={"name": "D"})
    assert fake.forms[-1] == ("/settings", {"name": "D"})
    assert booru.here(1, "/posts/5#comment-3") == "/booru/1/posts/5#comment-3" and booru.here(1, "/pools/2") is None
    assert booru.absolute(HOST, "//evil.test/x.png") is None and booru.absolute(HOST, "https://cdn.test/x.png")
    assert json.dumps(booru.upload_fields("", "cat", "", "", "")) == '{"tags": "cat"}'


def test_the_click_doesnt_wait(signed, server):
    client, fake = signed
    held = []
    client.app.state.booru_spawn = held.append  # the background work, kept to run when the test says
    account = client.get("/booru").text.split('name="account"', 1)[1].split('value="', 1)[1].split('"', 1)[0]
    asked = len(fake.seen)
    r = client.post("/booru/1/signin", data={"account": account, "next": "/booru/1/posts/1"}, follow_redirects=False)
    assert r.status_code == 303 and len(fake.seen) == asked and not server.messages_sent, "nothing was waited for"
    page = client.get(r.headers["location"]).text
    assert "is being sent from <strong>dave@home.test</strong>" in page and 'data-booru-wait="/booru/1/signin/state"' in page
    assert '<noscript><meta http-equiv="refresh" content="3"></noscript>' in page
    assert client.get("/booru/1/signin/state").json() == {"waiting": True}
    client.post("/booru/1/signin", data={"account": account})  # pressed again: it's still the one sign-in
    assert len(held) == 1

    # sent, and not there yet; then there: the page's script is told to stop waiting, and loading it again goes on
    real, fake._claimed = fake._claimed, lambda secret: None
    held.pop()()
    assert server.messages_sent[-1][2] == "!login K7QD-0001"
    assert client.get("/booru/1/signin/state").json() == {"waiting": True}
    assert "was sent from" in client.get("/booru/1/signin").text
    fake._claimed = real
    assert client.get("/booru/1/signin/state").json() == {"waiting": False} and not fake.sessions
    r = client.get("/booru/1/signin?next=/booru/1/posts/1", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/booru/1/posts/1" and len(fake.sessions) == 1
    assert client.get("/booru/1/signin/state").json() == {"waiting": False}

    # one that can't be started says so on the page that was waiting
    client.post("/booru/1/signout")
    server.people.pop(f"https://{HOST}/u/booru_bot")
    client.post("/booru/1/signin", data={"account": account}, follow_redirects=False)
    held.pop()()
    assert client.get("/booru/1/signin/state").json() == {"waiting": False}
    assert "Couldn&#39;t send the sign-in message as dave@home.test" in client.get("/booru/1/signin", follow_redirects=True).text


def test_votes_and_favourites_without_leaving_the_page(signed):
    client, fake = signed
    sign_in(client)
    page = client.get("/booru/1/posts/1").text
    assert 'data-booru-marks data-vote="1" data-fav="0"' in page and '<span data-booru-score>3</span>' in page
    assert 'data-booru-mark="fav"' in page and 'data-off="/booru/1/do/posts/1/unfavourite"' in page
    asked = len(fake.seen)
    r = client.post("/booru/1/do/posts/1/vote", data={"value": "0"}, headers={"accept": "application/json"})
    assert r.json() == {"ok": True, "message": "Done: /posts/1/vote.", "go": None}
    assert fake.seen[asked:] == [("POST", "/posts/1/vote")], "one request to the booru, and no page read again"
    assert "Done: /posts/1/vote." not in client.get("/booru").text, "the script shows it: nothing is left for the next page"
    r = client.post("/booru/1/do/posts/1/remove", data={"reason": ""}, headers={"accept": "application/json"})
    assert r.json() == {"ok": False, "message": "Give a reason; it's shown in the public modlog", "go": None}
    fake.sessions.clear()
    r = client.post("/booru/1/do/posts/1/favourite", data={}, headers={"accept": "application/json"})
    assert r.json()["ok"] is False and "has ended" in r.json()["message"]
