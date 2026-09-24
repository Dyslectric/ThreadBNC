from __future__ import annotations

import dataclasses

import pytest
from fastapi.testclient import TestClient

from threadbnc.accounts import AccountError, Poster
from threadbnc.adapters import RemoteRejected
from threadbnc.vault import TokenVault
from threadbnc.web import create_app

from .conftest import COMMUNITY, DOMAIN, FakeAdapter

HOME = "home.test"


def one(b, sql, *args):
    with b.db.connect() as conn:
        return conn.execute(sql, args).fetchone()


def col(b, sql, *args):
    with b.db.connect() as conn:
        return [r[0] for r in conn.execute(sql, args).fetchall()]


@pytest.fixture
def poster(bouncer, settings):
    return Poster(bouncer, TokenVault("test-key", settings.data_dir))


@pytest.fixture
def thread(server, bouncer):
    server.add_post("1", "Question", "body")
    server.add_comment("1", "10", "first comment")
    return bouncer.ingest_url(f"https://{DOMAIN}/post/1")


def test_login_stores_only_an_encrypted_token(poster, bouncer):
    account = poster.add(HOME, "dave", "hunter2")
    assert account.handle == f"dave@{HOME}" and account.is_default
    row = one(bouncer, "SELECT * FROM accounts")
    assert "jwt-dave" not in row["token_enc"] and "hunter2" not in str(dict(row))
    assert poster.vault.decrypt(row["token_enc"]).startswith("jwt-dave")


def test_wrong_password_is_rejected(poster, bouncer):
    with pytest.raises(AccountError, match="Login failed"):
        poster.add(HOME, "dave", "wrong")
    assert one(bouncer, "SELECT COUNT(*) FROM accounts")[0] == 0


def test_reply_is_archived_immediately_and_survives_federation_lag(server, bouncer, poster, thread):
    account = poster.add(HOME, "dave", "hunter2")
    parent = one(bouncer, "SELECT id FROM objects WHERE canonical_ap_id=?", f"https://{DOMAIN}/comment/10")[0]
    oid = poster.reply(account, thread, "my reply", parent)
    row = one(bouncer, "SELECT * FROM objects WHERE id=?", oid)
    assert row["parent_id"] == parent and row["canonical_ap_id"].startswith(f"https://{HOME}/comment/")
    # The community's server hasn't received it yet: that's not "missing".
    bouncer.sync_thread(thread, force=True)
    assert one(bouncer, "SELECT cur_missing FROM objects WHERE id=?", oid)[0] == 0
    assert col(bouncer, "SELECT event_type FROM state_events WHERE event_type='missing'") == []
    server.federate()
    bouncer.sync_thread(thread, force=True)
    assert one(bouncer, "SELECT COUNT(*) FROM objects WHERE canonical_ap_id=?", row["canonical_ap_id"])[0] == 1


def test_edit_records_revision_and_ignores_stale_copies(server, bouncer, poster, thread):
    account = poster.add(HOME, "dave", "hunter2")
    oid = poster.reply(account, thread, "original")
    server.federate()
    bouncer.sync_thread(thread, force=True)
    stale = dict(server.comments["1"])            # the community server lags behind...
    poster.edit(account, oid, "edited text")      # ...an edit made on the home server
    edited = dict(server.comments["1"])
    server.comments["1"] = stale
    bouncer.sync_thread(thread, force=True)
    assert col(bouncer, "SELECT body FROM revisions WHERE object_id=? ORDER BY seq", oid) == \
        ["original", "edited text"]               # no spurious revert to "original"
    server.comments["1"] = edited
    bouncer.sync_thread(thread, force=True)
    assert one(bouncer, "SELECT revision_count FROM objects WHERE id=?", oid)[0] == 2


def test_only_the_author_can_edit(poster, bouncer, thread):
    account = poster.add(HOME, "dave", "hunter2")
    theirs = one(bouncer, "SELECT id FROM objects WHERE canonical_ap_id=?", f"https://{DOMAIN}/comment/10")[0]
    with pytest.raises(AccountError, match="Only the author"):
        poster.edit(account, theirs, "hijack")


def test_delete_is_recorded_as_a_request_until_observed(server, bouncer, poster, thread):
    account = poster.add(HOME, "dave", "hunter2")
    oid = poster.reply(account, thread, "regret")
    server.federate()
    bouncer.sync_thread(thread, force=True)
    poster.delete(account, oid)
    sql = "SELECT event_type FROM state_events WHERE object_id=? AND event_type!='discovered' ORDER BY id"
    assert col(bouncer, sql, oid) == ["delete_requested"]
    bouncer.sync_thread(thread, force=True)       # server still shows it: no "restored"
    assert col(bouncer, sql, oid) == ["delete_requested"]
    lid = one(bouncer, "SELECT local_id FROM object_local_ids WHERE object_id=? AND domain=?", oid, DOMAIN)[0]
    server.edit_comment("1", lid, deleted=True, body="")
    bouncer.sync_thread(thread, force=True)
    assert col(bouncer, sql, oid) == ["delete_requested", "author_deleted"]
    assert col(bouncer, "SELECT body FROM revisions WHERE object_id=? ORDER BY seq DESC", oid)[0] == "regret"


def test_vote_and_unvote(server, bouncer, poster, thread):
    account = poster.add(HOME, "dave", "hunter2")
    post_oid = one(bouncer, "SELECT root_object_id FROM archived_threads WHERE id=?", thread)[0]
    ap = f"https://{DOMAIN}/post/1"
    poster.vote(account, post_oid, 1)
    assert poster.my_votes(account, [ap]) == {ap: 1} and server.votes[("dave", "post:1")] == 1
    poster.vote(account, post_oid, 0)
    assert poster.my_votes(account, [ap]) == {}


def test_vote_from_another_server_keeps_the_source_counts(server, bouncer, poster, thread, monkeypatch):
    """dave's server only just fetched the post, so it knows of 1 upvote (his);
    that must not replace the 500 the thread's own server reports."""
    server.posts["1"] = dataclasses.replace(server.posts["1"], upvotes=500, downvotes=20, score=480)
    bouncer.sync_thread(thread, force=True)
    real = FakeAdapter.vote_post
    monkeypatch.setattr(FakeAdapter, "vote_post", lambda self, token, pid, score: dataclasses.replace(
        real(self, token, pid, score), upvotes=1, downvotes=0, score=1))
    account = poster.add(HOME, "dave", "hunter2")
    oid = one(bouncer, "SELECT root_object_id FROM archived_threads WHERE id=?", thread)[0]
    counts = lambda: tuple(one(bouncer, "SELECT upvotes, downvotes, score FROM objects WHERE id=?", oid))
    poster.vote(account, oid, 1)
    assert counts() == (501, 20, 481)  # the vote counted in, until the next sync
    poster.vote(account, oid, -1)
    assert counts() == (500, 21, 479)
    poster.vote(account, oid, 0)
    assert counts() == (500, 20, 480)


def test_vote_on_the_source_server_takes_its_counts(server, bouncer, poster, thread, monkeypatch):
    real = FakeAdapter.vote_post
    monkeypatch.setattr(FakeAdapter, "vote_post", lambda self, token, pid, score: dataclasses.replace(
        real(self, token, pid, score), upvotes=42, downvotes=3, score=39))
    account = poster.add(DOMAIN, "dave", "hunter2")
    oid = one(bouncer, "SELECT root_object_id FROM archived_threads WHERE id=?", thread)[0]
    poster.vote(account, oid, 1)
    assert tuple(one(bouncer, "SELECT upvotes, downvotes, score FROM objects WHERE id=?", oid)) == (42, 3, 39)


def test_submit_creates_a_kept_thread(server, bouncer, poster, thread):
    account = poster.add(HOME, "dave", "hunter2")
    cid = one(bouncer, "SELECT community_id FROM archived_threads WHERE id=?", thread)[0]
    tid = poster.submit(account, cid, "My new post", "hello", "")
    t = one(bouncer, "SELECT * FROM archived_threads WHERE id=?", tid)
    assert t["retention"] == "manual"
    assert one(bouncer, "SELECT r.title FROM revisions r WHERE r.object_id=?", t["root_object_id"])[0] == \
        "My new post"


def two_communities(settings, server, bouncer, thread):
    """A logged-in client, and two communities to post in: !math and !news."""
    server.communities["news"] = dataclasses.replace(COMMUNITY, ap_id=f"https://{DOMAIN}/c/news", name="news")
    math = one(bouncer, "SELECT community_id FROM archived_threads WHERE id=?", thread)[0]
    news = bouncer.follow_community(f"!news@{DOMAIN}")
    settings.credentials_key = "test-key"
    client = TestClient(create_app(settings, bouncer))
    client.post("/login", data={"password": "pw"})
    client.post("/accounts", data={"server": HOME, "username": "dave", "password": "hunter2"})
    return client, math, news


def test_submit_to_several_communities(settings, server, bouncer, thread):
    client, math, news = two_communities(settings, server, bouncer, thread)
    form = client.get(f"/c/{math}/submit").text
    assert "Also post in other communities" in form and f'name="community_id" value="{news}"' in form
    assert f'name="community_id" value="{math}"' not in form  # the community it's for isn't offered again
    r = client.post(f"/c/{math}/submit", data={"title": "Big news", "body": "hello", "community_id": [str(news)]},
                    follow_redirects=False)
    assert r.status_code == 303
    first, second = [p for p in server.posts.values() if p.title == "Big news"]
    assert first.body == "hello"
    assert second.body == f"cross-posted from: {first.ap_id}\n\nhello"  # the copy points at the first
    tid = one(bouncer, "SELECT t.id FROM archived_threads t JOIN objects o ON o.id=t.root_object_id "
                       "WHERE o.canonical_ap_id=?", first.ap_id)[0]
    assert r.headers["location"] == f"/t/{tid}"  # opens the first
    assert "Posted in 2 communities" in client.get(r.headers["location"]).text


def test_repost_to_several_keeps_the_original_crosspost_line(settings, server, bouncer, thread):
    client, math, news = two_communities(settings, server, bouncer, thread)
    body = f"cross-posted from: https://{DOMAIN}/post/1\n\n> body"
    r = client.post(f"/t/{thread}/repost", data={"title": "Question", "body": body,
                                                 "community_id": [str(math), str(news)]}, follow_redirects=False)
    assert r.status_code == 303
    copies = [p for p in server.posts.values() if p.ap_id.startswith(f"https://{HOME}/")]
    assert [p.body for p in copies] == [body, body]
    assert len(col(bouncer, "SELECT id FROM state_events WHERE thread_id=? AND event_type='reposted'", thread)) == 2
    # Both are ticked next time.
    form = client.get(f"/t/{thread}/repost").text
    assert f'value="{math}" checked' in form and f'value="{news}" checked' in form


def test_one_community_failing_doesnt_stop_the_rest(settings, server, bouncer, thread, monkeypatch):
    client, math, news = two_communities(settings, server, bouncer, thread)
    real = FakeAdapter.create_post
    calls = []

    def create_post(self, token, community_id, title, body=None, url=None):
        calls.append(title)
        if len(calls) == 1:
            raise RemoteRejected("home.test refused: banned_from_community", "banned_from_community")
        return real(self, token, community_id, title, body, url)
    monkeypatch.setattr(FakeAdapter, "create_post", create_post)
    r = client.post(f"/c/{math}/submit", data={"title": "Hi", "body": "text", "community_id": [str(news)]},
                    follow_redirects=False)
    [made] = [p for p in server.posts.values() if p.title == "Hi"]
    assert made.body == "text"  # the first to succeed is the original: no crosspost line
    page = client.get(r.headers["location"]).text
    assert "Posted in 1 of 2 communities. Failed: !math@lemmy.test" in page


def test_a_community_that_cant_be_found_posts_nothing(settings, server, bouncer, thread):
    client, math, _news = two_communities(settings, server, bouncer, thread)
    before = len(server.posts)
    client.post(f"/c/{math}/submit", data={"title": "Hi", "community": "not a community"})
    assert len(server.posts) == before


def test_revoked_session_asks_to_log_in_again(server, bouncer, poster, thread):
    account = poster.add(HOME, "dave", "hunter2")
    server.revoked = True
    with pytest.raises(AccountError, match="log in again"):
        poster.reply(account, thread, "hello")
    assert poster.get(account.id).status == "needs_login"
    server.revoked = False
    with pytest.raises(AccountError, match="log in again"):
        poster.reply(poster.get(account.id), thread, "hello")
    poster.add(HOME, "dave", "hunter2")           # logging in again restores it
    assert poster.get(account.id).status == "ok"


def test_changed_key_can_not_decrypt(bouncer, settings, thread):
    Poster(bouncer, TokenVault("key-one", settings.data_dir)).add(HOME, "dave", "hunter2")
    other = Poster(bouncer, TokenVault("key-two", settings.data_dir))
    with pytest.raises(AccountError, match="decrypt"):
        other.reply(other.default(), thread, "hello")


def test_remove_account_logs_out(server, poster):
    account = poster.add(HOME, "dave", "hunter2")
    assert server.sessions
    poster.remove(account.id)
    assert not server.sessions and poster.list() == []


def test_web_flow(settings, server, bouncer, thread):
    settings.credentials_key = "test-key"
    client = TestClient(create_app(settings, bouncer))
    client.post("/login", data={"password": "pw"})
    assert "Add an account" in client.get(f"/t/{thread}").text
    r = client.post("/accounts", data={"server": HOME, "username": "dave", "password": "hunter2"})
    assert r.status_code == 200 and f"dave@{HOME}" in r.text and "hunter2" not in r.text
    page = client.get(f"/t/{thread}").text
    assert f"Comment as dave@{HOME}" in page and "▲" in page
    r = client.post(f"/t/{thread}/reply", data={"body": "hello from the web"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith(f"/t/{thread}#o")
    page = client.get(f"/t/{thread}").text
    assert "hello from the web" in page and ">Edit<" in page


def test_vote_returns_to_the_voted_object(settings, server, bouncer, thread):
    settings.credentials_key = "test-key"
    client = TestClient(create_app(settings, bouncer))
    client.post("/login", data={"password": "pw"})
    client.post("/accounts", data={"server": HOME, "username": "dave", "password": "hunter2"})
    post_oid = one(bouncer, "SELECT root_object_id FROM archived_threads WHERE id=?", thread)[0]
    # From the thread page: back to it, at the post.
    r = client.post(f"/o/{post_oid}/vote", data={"score": "1"},
                    headers={"referer": f"http://testserver/t/{thread}?sort=new"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/t/{thread}?sort=new#o{post_oid}"
    # No Referer (privacy extensions strip it): still the thread, never the start page.
    r = client.post(f"/o/{post_oid}/vote", data={"score": "0"}, follow_redirects=False)
    assert r.headers["location"] == f"/t/{thread}#o{post_oid}"


# ---- starting a community -----------------------------------------------------

def test_admin_status_is_recorded_at_login(server, poster):
    assert poster.add(HOME, "dave", "hunter2").is_admin is False
    server.admins.add("dave")
    account = poster.refresh_roles(poster.default())
    assert account.is_admin is True


def test_admin_creates_community_and_it_is_followed_without_expiry(server, bouncer, poster):
    server.admins.add("dave")
    account = poster.add(HOME, "dave", "hunter2")
    cid = poster.create_community(account, "retrohacks", "Retro Hacks", "old stuff")
    c = one(bouncer, "SELECT * FROM communities WHERE id=?", cid)
    assert c["canonical_ap_id"] == f"https://{HOME}/c/retrohacks"
    f = one(bouncer, "SELECT * FROM community_follows WHERE community_id=?", cid)
    assert f["active"] == 1 and f["retention_days"] is None and f["source_domain"] == HOME
    assert "community_created" in col(bouncer, "SELECT event_type FROM state_events WHERE community_id=?", cid)
    # ...and you can post in it straight away
    tid = poster.submit(account, cid, "Welcome", "first post")
    assert one(bouncer, "SELECT retention FROM archived_threads WHERE id=?", tid)[0] == "manual"


def test_non_admin_and_bad_names_are_refused(server, poster):
    account = poster.add(HOME, "dave", "hunter2")
    with pytest.raises(AccountError, match="Only admins"):
        poster.create_community(account, "retrohacks", "Retro Hacks")
    server.admins.add("dave")
    with pytest.raises(AccountError, match="lowercase"):
        poster.create_community(account, "Bad Name!", "x")
    poster.create_community(account, "retrohacks", "Retro Hacks")
    with pytest.raises(AccountError, match="already exists"):
        poster.create_community(account, "retrohacks", "Again")


def test_start_community_web_flow(settings, server, bouncer):
    settings.credentials_key = "test-key"
    server.admins.add("dave")
    client = TestClient(create_app(settings, bouncer))
    client.post("/login", data={"password": "pw"})
    client.post("/accounts", data={"server": HOME, "username": "dave", "password": "hunter2"})
    assert "Start a community" in client.get("/communities").text
    form = client.get("/communities/new").text
    assert f"{HOME} (as dave)" in form
    aid = one(bouncer, "SELECT id FROM accounts")[0]
    r = client.post("/communities/new", data={"account_id": aid, "name": "retrohacks", "title": "Retro Hacks"},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/c/")
    page = client.get(r.headers["location"]).text
    assert "!retrohacks" in page and "New post" in page and "Following" in page
