"""piefront: the public PieFed frontend, against a made-up server."""

from __future__ import annotations

import re
from typing import Any

import pytest
from fastapi.testclient import TestClient

from piefront.api import tree
from piefront.config import Settings
from piefront.render import render_markdown
from piefront.web import create_app
from threadbnc.adapters import RemoteAuthError, RemoteNotFound, RemoteUnavailable
from threadbnc.adapters.http import HostThrottle

SERVER = "pie.test"
ORIGIN = {"origin": "http://testserver"}


def person(pid: int, name: str, host: str = SERVER) -> dict[str, Any]:
    return {"id": pid, "user_name": name, "title": name.capitalize(), "actor_id": f"https://{host}/u/{name}"}


def community(cid: int, name: str, host: str = SERVER, **extra: Any) -> dict[str, Any]:
    return {"id": cid, "name": name, "title": name.capitalize(), "actor_id": f"https://{host}/c/{name}",
            "nsfw": False, **extra}


ALICE, BOB = person(1, "alice"), person(2, "bob", "other.test")
MATH, CATS = community(10, "math"), community(11, "cats", "other.test")


class FakePieFed:
    """Answers /api/alpha as a PieFed server would, and notes what it was asked."""

    def __init__(self) -> None:
        self.throttle = HostThrottle(0)
        self.calls: list[tuple[str, str, dict[str, Any], str | None]] = []
        self.down = False
        self.subscribed = {10}
        self.votes: dict[tuple[str, int], int] = {}
        self.read: set[tuple[str, int]] = set()
        self.sent: list[dict[str, Any]] = []
        self.posts = [self.post(100, "Primes are odd", MATH, ALICE, body="Except **two**.", comments=3),
                      self.post(101, "A cat", CATS, BOB, url="https://other.test/cat.jpg", kind="Image",
                                thumb="https://media.pie.test/cat.webp", nsfw=True)]
        self.comments = [self.comment(500, "0.500", ALICE, "First"), self.comment(501, "0.500.501", BOB, "Second"),
                         self.comment(502, "0.502", BOB, "Third <script>alert(1)</script>")]

    @staticmethod
    def post(pid: int, title: str, where: dict[str, Any], who: dict[str, Any], body: str = "", url: str | None = None,
             kind: str = "Discussion", thumb: str | None = None, nsfw: bool = False, comments: int = 0) -> dict[str, Any]:
        return {"post": {"id": pid, "title": title, "body": body, "url": url, "post_type": kind, "nsfw": nsfw,
                         "ap_id": f"https://{SERVER}/post/{pid}", "published": "2026-10-01T10:00:00.000000Z",
                         "thumbnail_url": thumb, "small_thumbnail_url": thumb},
                "counts": {"comments": comments, "upvotes": 5, "downvotes": 1, "score": 4},
                "community": where, "creator": who, "my_vote": 0, "read": False}

    @staticmethod
    def comment(cid: int, path: str, who: dict[str, Any], body: str) -> dict[str, Any]:
        return {"comment": {"id": cid, "path": path, "body": body, "post_id": 100,
                            "ap_id": f"https://{SERVER}/comment/{cid}", "published": "2026-10-01T11:00:00.000000Z"},
                "counts": {"upvotes": 2, "downvotes": 0, "score": 2}, "creator": who, "my_vote": 0}

    def get_json(self, domain: str, path: str, params: dict[str, Any] | None = None, token: str | None = None) -> Any:
        return self.request_json("GET", domain, path, params=params, token=token)

    def request_json(self, method: str, domain: str, path: str, *, params: dict[str, Any] | None = None,
                     json: dict[str, Any] | None = None, token: str | None = None, throttle: bool = True) -> Any:
        assert domain == SERVER and path.startswith("/api/alpha/")
        params = {k: v for k, v in (params or {}).items() if v is not None}
        path = path.removeprefix("/api/alpha")
        self.calls.append((method, path, json if json is not None else params, token))
        if self.down:
            raise RemoteUnavailable("down")
        if token not in (None, "jwt-alice"):
            raise RemoteAuthError("not logged in", "not_logged_in")
        return getattr(self, method.lower() + path.replace("/", "_"))(params, json, token)

    def asked(self, method: str, path: str) -> list[dict[str, Any]]:
        return [body for m, p, body, _ in self.calls if (m, p) == (method, path)]

    def get_site(self, params, body, token):
        site = {"site": {"name": "Pie Test", "description": "A test", "enable_downvotes": True}}
        if token:
            site["my_user"] = {"local_user_view": {"person": ALICE},
                               "follows": [{"community": c} for c in (MATH, CATS) if c["id"] in self.subscribed]}
        return site

    def get_user_unread_count(self, params, body, token):
        return {"replies": 1, "mentions": 0, "private_messages": 1}

    def get_post_list(self, params, body, token):
        posts = [p for p in self.posts if "community_name" not in params
                 or p["community"]["name"] == params["community_name"].split("@")[0]]
        if params.get("type_") == "Subscribed":
            posts = [p for p in posts if p["community"]["id"] in self.subscribed]
        return {"posts": [{**p, "my_vote": self.votes.get(("post", p["post"]["id"]), 0)} for p in posts] if token else posts,
                "next_page": None}

    def get_post(self, params, body, token):
        found = [p for p in self.posts if p["post"]["id"] == int(params["id"])]
        if not found:
            raise RemoteNotFound("no such post")
        return {"post_view": found[0], "community_view": {"community": found[0]["community"]}}

    def get_comment_list(self, params, body, token):
        return {"comments": self.comments if int(params["post_id"]) == 100 else [], "next_page": None}

    def get_community(self, params, body, token):
        found = [c for c in (MATH, CATS) if c["name"] == str(params.get("name", "")).split("@")[0] or c["id"] == params.get("id")]
        if not found:
            raise RemoteNotFound("no such community")
        return {"community_view": {"community": found[0], "counts": {"total_subscriptions_count": 42, "post_count": 7},
                                   "subscribed": "Subscribed" if token and found[0]["id"] in self.subscribed else "NotSubscribed"},
                "moderators": [{"moderator": ALICE}]}

    def get_topic_list(self, params, body, token):
        return {"topics": [{"name": "science", "title": "Science", "communities": [],
                            "children": [{"name": "maths", "title": "Maths", "children": [], "communities": [MATH]}]},
                           {"name": "animals", "title": "Animals", "children": [], "communities": [CATS]}]}

    def get_feed_list(self, params, body, token):
        return {"feeds": [{"name": "cosy", "title": "Cosy", "actor_id": f"https://{SERVER}/f/cosy", "children": [],
                           "description": "Soft things", "communities": [CATS]}]}

    def post_user_login(self, params, body, token):
        if (body["username"], body["password"]) != ("alice", "sesame"):
            raise RemoteAuthError("incorrect_login", "incorrect_login")
        return {"jwt": "jwt-alice"}

    def post_user_logout(self, params, body, token):
        return {}

    def post_post_like(self, params, body, token):
        self.votes[("post", body["post_id"])] = body["score"]
        return {"post_view": self.posts[0]}

    def post_comment_like(self, params, body, token):
        self.votes[("comment", body["comment_id"])] = body["score"]
        return {"comment_view": self.comments[0]}

    def post_comment(self, params, body, token):
        made = self.comment(600, f"0.{body.get('parent_id') or ''}.600".replace("..", "."), ALICE, body["body"])
        self.comments.append(made)
        return {"comment_view": made}

    def post_community_follow(self, params, body, token):
        (self.subscribed.add if body["follow"] else self.subscribed.discard)(body["community_id"])
        return {"community_view": {"subscribed": "Subscribed" if body["follow"] else "NotSubscribed"}}

    def get_user_replies(self, params, body, token):
        return {"replies": [{**self.comments[1], "comment_reply": {"id": 70, "read": ("reply", 70) in self.read},
                             "post": self.posts[0]["post"], "community": MATH}]}

    def get_user_mentions(self, params, body, token):
        return {"replies": []}

    def get_private_message_list(self, params, body, token):
        return {"private_messages": [{"private_message": {"id": 80, "content": "Hello there", "read": ("message", 80) in self.read,
                                                           "published": "2026-10-02T09:00:00.000000Z"},
                                      "creator": BOB, "recipient": ALICE}]}

    def post_comment_mark_as_read(self, params, body, token):
        self.read.add(("reply", body["comment_reply_id"])) if body["read"] else self.read.discard(("reply", body["comment_reply_id"]))
        return {}

    def post_private_message_mark_as_read(self, params, body, token):
        self.read.add(("message", body["private_message_id"]))
        return {}

    def post_private_message(self, params, body, token):
        self.sent.append(body)
        return {}

    def get_resolve_object(self, params, body, token):
        if "bob" not in params["q"]:
            raise RemoteNotFound("nobody")
        return {"person": {"person": {**BOB, "name": "bob"}}}


@pytest.fixture
def server() -> FakePieFed:
    return FakePieFed()


@pytest.fixture
def client(server: FakePieFed) -> TestClient:
    settings = Settings(server=SERVER, secret_key="k" * 32, https_only_cookies=False)
    return TestClient(create_app(settings, server), headers=ORIGIN)  # type: ignore[arg-type]


def sign_in(client: TestClient) -> None:
    r = client.post("/login", data={"username": "alice", "password": "sesame", "next": "/"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"


def test_anyone_reads_the_feed_and_the_answer_is_shared(client, server):
    r = client.get("/")
    assert r.status_code == 200
    assert "Primes are odd" in r.text and "A cat" in r.text
    assert 'href="/c/math"' in r.text and 'href="/c/cats@other.test"' in r.text
    assert "Sign in" in r.text and "data-vote" not in r.text  # votes are shown, not offered
    assert server.asked("GET", "/post/list")[0]["type_"] == "Popular"
    client.get("/")
    assert len(server.asked("GET", "/post/list")) == 1  # read once for everyone not signed in
    assert r.headers["x-robots-tag"].startswith("noindex")
    assert "img-src 'self' https:" in r.headers["content-security-policy"]


def test_sorts_and_views(client, server):
    r = client.get("/?feed=local&sort=top&t=week&view=tiles")
    asked = server.asked("GET", "/post/list")[-1]
    assert (asked["type_"], asked["sort"]) == ("Local", "TopWeek")
    assert 'class="tiles"' in r.text and 'src="https://media.pie.test/cat.webp"' in r.text
    assert 'data-veil="NSFW"' in r.text
    assert 'class="tiles"' in client.get("/?feed=local").text  # the view is remembered
    assert 'class="timeline"' in client.get("/?view=timeline").text
    assert "<strong>two</strong>" in client.get("/?view=timeline").text  # whole posts
    assert "view=loops" not in r.text


def test_community_page(client, server):
    r = client.get("/c/cats@other.test")
    assert r.status_code == 200 and "A cat" in r.text and "Primes" not in r.text
    assert server.asked("GET", "/post/list")[-1]["community_name"] == "cats@other.test"
    assert "<strong>42</strong> subscribers" in r.text
    assert client.get("/c/nothing").status_code == 404


def test_thread_and_comment_tree(client, server):
    r = client.get("/t/100")
    assert r.status_code == 200
    assert r.text.index('id="o500"') < r.text.index('id="o501"') < r.text.index('id="o502"')
    assert "1 reply hidden" in r.text  # 501 is under 500
    assert "<script>alert" not in r.text
    assert 'data-thread="100"' in r.text and "Sign in</a> to comment" in r.text
    assert client.get("/t/999").status_code == 404
    assert client.post("/t/100/comments/check").json() == {"jobs": []}


def test_comment_tree_keeps_orphans_at_the_top():
    top = tree([{"id": 2, "parent": 1, "children": []}, {"id": 3, "parent": 9, "children": []},
                {"id": 1, "parent": None, "children": []}])
    assert [c["id"] for c in top] == [3, 1] and top[1]["children"][0]["id"] == 2 and top[1]["descendants"] == 1


def test_sign_in_shows_subscriptions_and_unread(client, server):
    r = client.post("/login", data={"username": "alice", "password": "wrong"})
    assert r.status_code == 401 and "didn&#39;t accept" in r.text
    sign_in(client)
    r = client.get("/")
    assert server.asked("GET", "/post/list")[-1]["type_"] == "Subscribed"
    assert "Primes are odd" in r.text and "A cat" not in r.text
    sidebar = r.text[r.text.index('id="feed-sidebar"'):]
    assert 'href="/c/math"' in sidebar and "cats" not in sidebar
    assert re.search(r'class="count"[^>]*>2<', r.text)  # a reply and a message
    assert "data-vote" in r.text
    r = client.post("/logout", follow_redirects=False)
    assert r.status_code == 303 and "data-vote" not in client.get("/").text


def test_a_session_the_server_dropped_signs_you_out(client, server):
    sign_in(client)
    server.request_json, real = lambda *a, **k: (_ for _ in ()).throw(RemoteAuthError("x", "not_logged_in")) \
        if k.get("token") else real(*a, **k), server.request_json
    client.app.state.api.forget("jwt-alice")
    r = client.get("/?feed=popular")
    assert r.status_code == 200 and "has ended" in r.text and "data-vote" not in r.text


def test_voting_commenting_and_subscribing(client, server):
    assert client.post("/p/100/vote", data={"score": 1}, follow_redirects=False).headers["location"].startswith("/login")
    sign_in(client)
    client.get("/")  # (shows "Signed in as")
    r = client.post("/p/100/vote", data={"score": 1}, headers={"X-ThreadBNC-Fetch": "1", "referer": "http://testserver/?feed=all"})
    assert r.json() == {"ok": True, "redirect": "/?feed=all", "messages": []}
    assert server.votes[("post", 100)] == 1
    client.post("/k/500/vote", data={"score": -1})
    assert server.votes[("comment", 500)] == -1
    assert client.post("/p/100/vote", data={"score": 5}).status_code == 400

    r = client.post("/t/100/reply", data={"body": "Mine", "parent_id": "500"}, follow_redirects=False)
    assert r.headers["location"] == "/t/100#o600"
    assert server.asked("POST", "/comment")[-1] == {"body": "Mine", "post_id": 100, "parent_id": 500}

    r = client.post("/follow", data={"community_id": 11, "follow": "1"})
    assert 11 in server.subscribed and "cats" in client.get("/").text[client.get("/").text.index('id="feed-sidebar"'):]
    client.post("/follow", data={"community_id": 11, "follow": "0"})
    assert 11 not in server.subscribed


def test_forms_from_other_sites_are_refused(client, server):
    sign_in(client)
    assert client.post("/p/100/vote", data={"score": 1}, headers={"origin": "https://evil.test"}).status_code == 403
    assert ("post", 100) not in server.votes


def test_forum_directory(client, server):
    r = client.get("/forums")
    assert r.status_code == 200
    assert 'href="/forums/topics/science"' in r.text and 'href="/forums/topics/science/maths"' in r.text
    assert 'href="/forums/topics/animals"' in r.text
    r = client.get("/forums/topics/science/maths")
    assert 'href="/c/math"' in r.text and "Subscribe" in r.text and "forum-head" in r.text
    part = client.get("/forums/topics/animals?part=1&depth=1")
    assert part.text.lstrip().startswith("{#") is False and 'class="forum-body"' in part.text and "<html" not in part.text
    assert 'href="/c/cats@other.test"' in part.text
    r = client.get("/forums/feeds")
    assert "Cosy" in r.text and "Soft things" in r.text
    assert client.get("/forums/topics/nowhere").status_code == 404
    assert len(server.asked("GET", "/topic/list")) == 1  # kept: it's a big list

    sign_in(client)
    r = client.get("/forums/topics/science/maths")
    assert "Subscribed" in r.text and 'name="community_id" value="10"' in r.text and "data-forum-follow" in r.text


def test_inbox_and_messages(client, server):
    assert client.get("/inbox", follow_redirects=False).headers["location"].startswith("/login")
    sign_in(client)
    r = client.get("/inbox")
    assert "Hello there" in r.text and "Second" in r.text and 'href="/t/100#o501"' in r.text
    assert r.text.index("Hello there") < r.text.index("Second")  # newest first

    client.post("/inbox/reply/70/read", data={"read": "1"})
    assert ("reply", 70) in server.read
    r = client.get("/inbox")
    assert "Second" not in r.text and "Second" in client.get("/inbox?show=all").text
    assert "Second" not in client.get("/inbox?show=all&kind=message").text

    client.post("/inbox/message/80/reply", data={"body": "Hi Bob", "author_id": "2"})
    assert server.sent[-1] == {"content": "Hi Bob", "recipient_id": 2} and ("message", 80) in server.read
    client.post("/inbox/reply/70/reply", data={"body": "Answer", "post_id": "100", "comment_id": "501"})
    assert server.asked("POST", "/comment")[-1] == {"body": "Answer", "post_id": 100, "parent_id": 501}

    r = client.post("/inbox/message", data={"to": "bob@other.test", "body": "New"}, follow_redirects=False)
    assert r.status_code == 303 and server.sent[-1] == {"content": "New", "recipient_id": 2}
    r = client.post("/inbox/message", data={"to": "carol@other.test", "body": "New"})
    assert "couldn&#39;t find" in r.text and len(server.sent) == 2


def test_the_server_being_down_is_said(client, server):
    server.down = True
    r = client.get("/")
    assert r.status_code == 502 and "couldn&#39;t be reached" in r.text


def test_pictures_in_text_are_shown_from_where_they_are():
    out = render_markdown("![a cat](https://other.test/cat.png) ![x](http://plain.test/x.png) <b>raw</b>")
    assert '<img class="media" src="https://other.test/cat.png"' in out
    assert "<img" not in out.split("cat.png")[-1] and "<b>" not in out
