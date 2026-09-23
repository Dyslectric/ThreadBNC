"""Lemmy 1.0 (API v4) adapter, checked against real 1.0 responses.

The fixtures in fixtures/lemmy_v4 were captured from a Lemmy 1.0 nightly
server and anonymised: every key, id, count, flag and timestamp is as the server
sent it; names, text and hosts are placeholders.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from threadbnc.adapters.http import HostThrottle
from threadbnc.adapters import (
    CommunityRef,
    Lemmy1Adapter,
    LemmyAdapter,
    PieFedAdapter,
    RemoteNotFound,
    RemoteUnavailable,
    adapter_class,
)
from threadbnc import store
from threadbnc.bouncer import Bouncer
from threadbnc.db import fmt_ts, open_database, parse_ts, utcnow

FIXTURES = Path(__file__).parent / "fixtures" / "lemmy_v4"
DOMAIN = "lemmy1.test"


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


class ReplayHttp:
    """Stands in for HttpClient: answers from a table, records every call."""

    def __init__(self, routes: dict[tuple[str, str], Any]):
        self.routes = routes
        self.throttle = HostThrottle(0)
        self.calls: list[tuple[str, str, str, dict[str, Any], dict[str, Any] | None, str | None]] = []
        self.throttled: list[bool] = []

    def get_json(self, domain, path, params=None, token=None):
        return self.request_json("GET", domain, path, params=params, token=token)

    def request_json(self, method, domain, path, *, params=None, json=None, token=None, throttle=True):
        self.throttled.append(throttle)
        # Same None-stripping as the real client, so tests see what goes on the wire.
        params = {k: v for k, v in (params or {}).items() if v is not None}
        body = {k: v for k, v in json.items() if v is not None} if json is not None else None
        path = path.removeprefix(Lemmy1Adapter.api_base)
        self.calls.append((method, domain, path, params, body, token))
        answer = self.routes.get((method, path))
        if answer is None:
            raise RemoteNotFound(f"no route for {method} {path}")
        return answer(params, body) if callable(answer) else answer

    def called(self, method: str, path: str) -> list[tuple[dict[str, Any], dict[str, Any] | None]]:
        return [(p, b) for m, _, pa, p, b, _ in self.calls if (m, pa) == (method, path)]


def adapter(routes: dict[tuple[str, str], Any]) -> tuple[Lemmy1Adapter, ReplayHttp]:
    http = ReplayHttp(routes)
    return Lemmy1Adapter(DOMAIN, http), http  # type: ignore[arg-type]


def paged(*pages: Any):
    """A cursor-paged endpoint: page N is returned for cursor str(N-1)."""
    def answer(params, _body):
        n = int(params.get("page_cursor") or 0)
        items = pages[n] if n < len(pages) else []
        return {"items": items, **({"next_page": str(n + 1)} if n + 1 < len(pages) else {})}
    return answer


# ---- choosing the adapter --------------------------------------------------------

@pytest.mark.parametrize("software,version,cls", [
    ("lemmy", "0.19.20", LemmyAdapter),
    ("lemmy", "0.19.3-beta.1", LemmyAdapter),
    ("lemmy", "0.20.0-alpha.4", Lemmy1Adapter),
    ("lemmy", "1.0.0-nightly-2026-09-21", Lemmy1Adapter),
    ("lemmy", "1.2.3", Lemmy1Adapter),
    ("lemmy", None, LemmyAdapter),
    ("piefed", "1.2.0", PieFedAdapter),
    ("mastodon", "4.3.0", None),
])
def test_adapter_is_chosen_by_software_and_version(software, version, cls):
    assert adapter_class(software, version) is cls


def test_restarting_picks_up_an_upgrade_the_database_does_not_know_about(settings):
    """The stored software check is recent and says 0.19, but the server now runs
    1.0: a freshly started bouncer must ask the server, not trust the database."""
    routes = {
        ("GET", "/.well-known/nodeinfo"): {"links": [{"rel": "http://nodeinfo.diaspora.software/ns/schema/2.1",
                                                      "href": f"https://{DOMAIN}/nodeinfo/2.1"}]},
        ("GET", "/nodeinfo/2.1"): {"software": {"name": "lemmy", "version": "1.0.0-beta.2"}},
    }
    db = open_database(settings)
    with db.transaction() as conn:
        store.upsert_instance(conn, DOMAIN, utcnow(), software="lemmy", version="0.19.20")
    bouncer = Bouncer(db, settings, http=ReplayHttp(routes))  # type: ignore[arg-type]
    assert type(bouncer.adapter_for(DOMAIN)) is Lemmy1Adapter
    with db.connect() as conn:
        assert conn.execute("SELECT software_version FROM instances WHERE domain=?",
                            (DOMAIN,)).fetchone()[0] == "1.0.0-beta.2"


def test_unreachable_server_falls_back_to_what_was_seen_last(settings):
    db = open_database(settings)
    with db.transaction() as conn:
        store.upsert_instance(conn, DOMAIN, utcnow(), software="lemmy", version="1.0.0")
    bouncer = Bouncer(db, settings, http=ReplayHttp({}))  # type: ignore[arg-type]  # nodeinfo: not found
    assert type(bouncer.adapter_for(DOMAIN)) is Lemmy1Adapter


def test_bouncer_switches_to_v4_after_the_server_upgrades(settings):
    nodeinfo = {"version": "0.19.20"}
    routes = {
        ("GET", "/.well-known/nodeinfo"): {"links": [{"rel": "http://nodeinfo.diaspora.software/ns/schema/2.1",
                                                      "href": f"https://{DOMAIN}/nodeinfo/2.1"}]},
        ("GET", "/nodeinfo/2.1"): lambda _p, _b: {"software": {"name": "lemmy", "version": nodeinfo["version"]}},
    }
    bouncer = Bouncer(open_database(settings), settings, http=ReplayHttp(routes))  # type: ignore[arg-type]
    first = bouncer.adapter_for(DOMAIN)
    assert type(first) is LemmyAdapter and bouncer.adapter_for(DOMAIN) is first
    nodeinfo["version"] = "1.0.0"
    # A day later both the cached adapter and the stored software check are stale.
    stale = fmt_ts(parse_ts(utcnow()) - timedelta(days=2))
    bouncer._adapters[DOMAIN] = (first, stale)
    with bouncer.db.transaction() as conn:
        conn.execute("UPDATE instances SET software_checked_at=? WHERE domain=?", (stale, DOMAIN))
    assert type(bouncer.adapter_for(DOMAIN)) is Lemmy1Adapter


# ---- reading ---------------------------------------------------------------------

def test_fetch_post_reads_inline_counts_and_moderators():
    data = fixture("post")
    a, _ = adapter({("GET", "/post"): data})
    p = data["post_view"]["post"]
    post = a.fetch_post(str(p["id"]))
    assert post.local_id == str(p["id"]) and post.title == p["name"] and post.body == p["body"]
    assert (post.score, post.comment_count, post.upvotes, post.downvotes) == (
        p["score"], p["comments"], p["upvotes"], p["downvotes"])
    assert post.created_at and post.created_at.startswith(p["published_at"][:19])
    assert post.newest_comment_at and post.newest_comment_at.startswith(p["newest_comment_time_at"][:19])
    assert post.author.ap_id == data["post_view"]["creator"]["ap_id"]
    assert post.community.visibility == data["post_view"]["community"]["visibility"]
    assert post.community.moderator_ap_ids == [m["moderator"]["ap_id"] for m in data["moderators"]]


def test_fetch_comments_follows_cursors_and_threads_replies():
    items = fixture("comment_list")["items"]
    a, http = adapter({("GET", "/comment/list"): paged(items[:2], items[2:])})
    comments = {c.local_id: c for c in a.fetch_comments("1")}
    assert set(comments) == {str(i["comment"]["id"]) for i in items}
    assert len(http.called("GET", "/comment/list")) == 2
    assert http.called("GET", "/comment/list")[0][0] == {"post_id": "1", "type_": "all", "sort": "old",
                                                         "limit": 50}
    for i in items:
        c, parts = comments[str(i["comment"]["id"])], i["comment"]["path"].split(".")
        assert c.parent_local_id == (parts[-2] if len(parts) >= 3 else None)
        assert c.body == i["comment"]["content"] and c.reply_count == i["comment"]["child_count"]


def test_list_community_posts_maps_sorts_and_walks_to_the_page():
    items = fixture("post_list")["items"]
    a, http = adapter({("GET", "/post/list"): paged(items[:1], items[1:])})
    ref = CommunityRef(DOMAIN, "main", "remote1.test")
    assert [p.local_id for p in a.list_community_posts(ref, sort="TopDay", page=2, limit=1)] == [
        str(items[1]["post"]["id"])]
    first = http.called("GET", "/post/list")[0][0]
    assert first["sort"] == "top" and first["time_range_seconds"] == 86400
    assert first["community_name"] == "main@remote1.test" and first["type_"] == "all"
    assert a.list_community_posts(ref, sort="New", page=9) == []  # past the last page
    assert http.called("GET", "/post/list")[-1][0]["sort"] == "new"


def test_fetch_community_reads_sidebar_and_visibility():
    data = fixture("community")
    a, _ = adapter({("GET", "/community"): data})
    c = a.fetch_community(CommunityRef(DOMAIN, "main", "remote1.test"))
    raw = data["community_view"]["community"]
    assert c.ap_id == raw["ap_id"] and c.visibility == raw["visibility"] and c.local_id == str(raw["id"])
    assert c.moderator_ap_ids == [m["moderator"]["ap_id"] for m in data["moderators"]]


def test_modlog_restore_is_recorded_as_inactive():
    data = fixture("modlog_remove_post")  # a removal and its restore, newest first
    post_id = str(data["items"][0]["target_post"]["id"])
    entries = sorted((e for e in data["items"] if str(e["target_post"]["id"]) == post_id),
                     key=lambda e: e["modlog"]["published_at"])
    assert [e["modlog"]["is_revert"] for e in entries] == [False, True]
    a, _ = adapter({("GET", "/modlog"): data, ("GET", "/site"): fixture("site")})
    actions = a.fetch_moderation_state(post_local_id=post_id)
    assert [(x.kind, x.active) for x in actions] == [("remove_post", True), ("remove_post", False)]
    assert actions[-1].reason == entries[-1]["modlog"]["reason"]
    assert actions[-1].moderator is None and actions[-1].attribution == "unknown"  # hidden from anonymous readers
    assert a.fetch_moderation_state(comment_local_id="999999") == []


def test_resolve_object_is_one_tagged_view():
    data = fixture("resolve")
    a, _ = adapter({("GET", "/resolve_object"): data})
    assert a.resolve_as("tok", data["post"]["ap_id"]) == {"post": str(data["post"]["id"])}
    assert a.resolve_ap_id(data["post"]["ap_id"]) == str(data["post"]["id"])


def test_resolve_person_finds_the_actor_url_with_webfinger():
    actor = "https://remote1.test/users/someone"
    routes = {
        ("GET", "/.well-known/webfinger"): {"links": [
            {"rel": "http://webfinger.net/rel/profile-page", "type": "text/html", "href": "https://x/@someone"},
            {"rel": "self", "type": "application/activity+json", "href": actor}]},
        ("GET", "/resolve_object"): lambda p, _b: {
            "type_": "person", "person": {"id": 42, "name": "someone", "ap_id": p["q"]}},
    }
    a, http = adapter(routes)
    pid, person = a.resolve_person("tok", "@someone@remote1.test")
    assert pid == "42" and person.ap_id == actor
    assert http.calls[0][1] == "remote1.test"  # WebFinger asked the person's own server
    assert http.called("GET", "/resolve_object")[0][0] == {"q": actor}


# ---- acting as an account ---------------------------------------------------------

def test_login_and_whoami_use_the_account_endpoints():
    me = {"local_user_view": {"person": {"id": 1, "name": "dave", "ap_id": f"https://{DOMAIN}/u/dave"},
                              "local_user": {"admin": True}}}
    a, http = adapter({("POST", "/account/auth/login"): {"jwt": "J"}, ("GET", "/account"): me})
    assert a.login("dave", "pw", "123456") == "J"
    assert http.called("POST", "/account/auth/login")[0][1] == {
        "username_or_email": "dave", "password": "pw", "totp_2fa_token": "123456"}
    assert a.whoami("J").username == "dave" and a.my_roles("J") == {"admin": True}


@pytest.mark.parametrize("score,body", [(1, {"is_upvote": True}), (-1, {"is_upvote": False}), (0, {})])
def test_votes_use_is_upvote_and_omit_it_to_clear(score, body):
    data = fixture("post")
    a, http = adapter({("POST", "/post/like"): {"post_view": data["post_view"]}})
    a.vote_post("tok", "5", score)
    assert http.called("POST", "/post/like")[0][1] == {"post_id": 5, **body}


def test_deletes_use_http_delete():
    a, http = adapter({("DELETE", "/post"): {"post_view": fixture("post")["post_view"]},
                       ("DELETE", "/comment"): {"comment_view": fixture("comment_list")["items"][0]}})
    a.delete_post("tok", "5")
    a.delete_comment("tok", "6", deleted=False)
    assert http.called("DELETE", "/post")[0][1] == {"post_id": 5, "deleted": True}
    assert http.called("DELETE", "/comment")[0][1] == {"comment_id": 6, "deleted": False}


def test_moderation_calls_send_v4_bodies(monkeypatch):
    monkeypatch.setattr("threadbnc.adapters.lemmy1.time.time", lambda: 1_000_000)
    a, http = adapter({(m, p): {} for m, p in [("POST", "/community/ban_user"), ("POST", "/admin/ban"),
                                               ("POST", "/post/remove"), ("POST", "/post/lock"),
                                               ("POST", "/post/feature")]})
    a.ban_from_community("tok", "3", "4", True, None, days=2, remove_data=True)
    assert http.called("POST", "/community/ban_user")[0][1] == {
        "community_id": 3, "person_id": 4, "ban": True, "remove_or_restore_data": True, "reason": "",
        "expires_at": 1_000_000 + 2 * 86400}
    a.site_ban("tok", "4", False, "sorry")
    assert http.called("POST", "/admin/ban")[0][1] == {"person_id": 4, "ban": False,
                                                      "remove_or_restore_data": False, "reason": "sorry"}
    a.remove_post("tok", "9", True)
    a.lock_post("tok", "9", True)
    a.feature_post("tok", "9", True)
    assert http.called("POST", "/post/remove")[0][1] == {"post_id": 9, "removed": True, "reason": ""}
    assert http.called("POST", "/post/lock")[0][1] == {"post_id": 9, "locked": True, "reason": ""}
    assert http.called("POST", "/post/feature")[0][1]["feature_type"] == "community"


def test_admin_settings_and_blocklist_edits():
    site = fixture("site")
    blocked = paged([{"instance": {"domain": "spam.test"}}], [{"instance": {"domain": "worse.test"}}])
    a, http = adapter({("GET", "/site"): site, ("GET", "/federated_instances"): blocked,
                       ("POST", "/admin/instance/block"): {}, ("PUT", "/site"): {}})
    settings = a.admin_settings("tok")
    assert settings["registration_mode"] == "Open"  # the name the admin page uses
    assert settings["blocked_instances"] == ["spam.test", "worse.test"]
    a.update_site("tok", blocked_instances=["spam.test", "new.test"])
    assert sorted((b["instance"], b["block"]) for _, b in http.called("POST", "/admin/instance/block")) == [
        ("new.test", True), ("worse.test", False)]
    assert not http.called("PUT", "/site")  # nothing else to change
    a.update_site("tok", registration_mode="RequireApplication")
    assert http.called("PUT", "/site")[0][1] == {"registration_mode": "require_application"}


# ---- private communities -------------------------------------------------------------

def test_join_requests_and_answers():
    request = {"person": {"id": 7, "name": "alice", "ap_id": "https://remote1.test/u/alice"},
               "community": {"id": 3, "name": "club", "ap_id": f"https://{DOMAIN}/c/club"},
               "is_new_instance": False, "follow_state": "approval_required"}
    a, http = adapter({("GET", "/community/pending_follows/list"): paged([request]),
                       ("POST", "/community/pending_follows/approve"): {"success": True},
                       ("PUT", "/community"): fixture("community")})
    [req] = a.join_requests("tok")
    assert (req.person.ap_id, req.person_local_id, req.community_local_id, req.state) == (
        "https://remote1.test/u/alice", "7", "3", "approval_required")
    assert http.called("GET", "/community/pending_follows/list")[0][0] == {"unread_only": True, "limit": 50}
    a.answer_join_request("tok", "3", "7", approve=False)
    assert http.called("POST", "/community/pending_follows/approve")[0][1] == {
        "community_id": 3, "follower_id": 7, "approve": False}
    a.set_community_visibility("tok", "3", "private")
    assert http.called("PUT", "/community")[0][1] == {"community_id": 3, "visibility": "private"}


@pytest.mark.parametrize("cls", [LemmyAdapter, Lemmy1Adapter])
def test_reading_as_a_member_sends_the_session_on_reads_only(cls):
    http = ReplayHttp({})
    base = cls(DOMAIN, http)  # type: ignore[arg-type]
    member = base.reading_as("T")
    for adapter in (base, member):
        with pytest.raises(RemoteNotFound):
            adapter.fetch_post("1")
    tokens = [c[5] for c in http.calls]
    assert tokens == [None, "T"]
    assert base._read_token is None  # the shared adapter stays anonymous


@pytest.mark.parametrize("cls", [LemmyAdapter, Lemmy1Adapter])
def test_only_background_reads_wait_for_the_per_server_spacing(cls):
    http = ReplayHttp({})
    a = cls(DOMAIN, http)  # type: ignore[arg-type]
    for call in (lambda: a.fetch_post("1"),                      # archiving: anonymous
                 lambda: a.reading_as("T").fetch_post("1"),      # archiving a private community
                 lambda: a.community_moderators("T", "3")):      # you, waiting for the Mod tab
        with pytest.raises(RemoteNotFound):
            call()
    assert http.throttled == [True, True, False]


def test_resolved_ids_are_remembered():
    data = fixture("resolve")
    a, http = adapter({("GET", "/resolve_object"): data})
    ap = data["post"]["ap_id"]
    first = a.resolve_as("tok", ap)
    first["post"] = "changed by the caller"
    assert a.resolve_as("tok", ap) == {"post": str(data["post"]["id"])}  # callers can't spoil the cache
    assert len(http.called("GET", "/resolve_object")) == 1
    a, http = adapter({("GET", "/resolve_object"): {}})  # not found: not remembered
    assert a.resolve_as("tok", ap) == {} and a.resolve_as("tok", ap) == {}
    assert len(http.called("GET", "/resolve_object")) == 2


def test_known_community_is_found_by_name_not_resolve_object():
    """resolve_object may fetch a remote community again, and some crash the
    server doing it; one the server already has is a plain lookup."""
    a, http = adapter({("GET", "/community"): fixture("community")})
    assert a.resolve_as("tok", "https://remote2.test/c/user4") == {"community": "3"}
    assert http.called("GET", "/community")[0][0] == {"name": "user4@remote2.test"}
    assert not http.called("GET", "/resolve_object")


def test_resolve_object_server_errors_back_off(monkeypatch):
    import threadbnc.adapters.lemmy as lemmy
    now = [1000.0]
    monkeypatch.setattr(lemmy.time, "monotonic", lambda: now[0])

    def crash(_p, _b):
        raise RemoteUnavailable("HTTP 502")
    a, http = adapter({("GET", "/resolve_object"): crash})
    ap = "https://remote2.test/c/crashes"
    for wait in (lemmy.RESOLVE_RETRY_FIRST, 2 * lemmy.RESOLVE_RETRY_FIRST):
        with pytest.raises(RemoteUnavailable):
            a.resolve_as("tok", ap)
        with pytest.raises(RemoteUnavailable, match="trying again"):
            a.resolve_as("tok", ap)  # not asked again yet
        now[0] += wait
    assert len(http.called("GET", "/resolve_object")) == 2


def test_follow_state_is_read_without_following_again():
    views = iter([{"community_actions": {"follow_state": "Accepted"}},
                  {"community_actions": {"follow_state": "Pending"}},
                  {"subscribed": "Subscribed"},  # 0.19's shape
                  {}])
    a, http = adapter({("GET", "/community"): lambda p, b: {"community_view": next(views)}})
    assert [a.community_follow_state("tok", "77") for _ in range(4)] == \
        ["subscribed", "pending", "subscribed", "not_subscribed"]
    assert http.called("GET", "/community")[0][0] == {"id": 77}
    assert not http.called("POST", "/community/follow")
