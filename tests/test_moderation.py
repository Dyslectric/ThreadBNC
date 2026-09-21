from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from threadbnc.accounts import AccountError, Poster
from threadbnc.moderation import Moderation
from threadbnc.vault import TokenVault
from threadbnc.web import create_app

from .conftest import DOMAIN

HOME = "home.test"
DAVE = f"https://{HOME}/u/dave"


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
def mod(poster):
    return Moderation(poster)


@pytest.fixture
def thread(server, bouncer):
    server.add_post("1", "Question", "body")
    server.add_comment("1", "10", "rude comment")
    return bouncer.ingest_url(f"https://{DOMAIN}/post/1")


@pytest.fixture
def cid(bouncer, thread):
    return one(bouncer, "SELECT community_id FROM archived_threads WHERE id=?", thread)[0]


def make_dave_a_mod(server, bouncer, thread):
    server.posts["1"].community.moderator_ap_ids.append(DAVE)
    bouncer.sync_thread(thread, force=True)


def test_powers_follow_the_observed_moderator_list(server, bouncer, poster, mod, thread, cid):
    account = poster.add(HOME, "dave", "hunter2")
    assert not mod.powers(account, cid).any
    make_dave_a_mod(server, bouncer, thread)
    assert mod.powers(account, cid).moderator
    # the change itself is history
    assert "moderator_added" in col(bouncer, "SELECT event_type FROM state_events WHERE community_id=?", cid)


def test_non_moderators_are_refused(poster, mod, thread, bouncer, cid):
    account = poster.add(HOME, "dave", "hunter2")
    post_oid = one(bouncer, "SELECT root_object_id FROM archived_threads WHERE id=?", thread)[0]
    with pytest.raises(AccountError, match="isn't a moderator"):
        mod.remove(account, post_oid, "spam")
    with pytest.raises(AccountError, match="isn't a moderator"):
        mod.community_ban(account, cid, "bob@other.test", True, "spam")


def test_remove_post_is_requested_then_observed_with_reason(server, bouncer, poster, mod, thread):
    account = poster.add(HOME, "dave", "hunter2")
    make_dave_a_mod(server, bouncer, thread)
    post_oid = one(bouncer, "SELECT root_object_id FROM archived_threads WHERE id=?", thread)[0]
    with pytest.raises(AccountError, match="reason"):
        mod.remove(account, post_oid, "")
    mod.remove(account, post_oid, "off-topic")
    sql = "SELECT event_type FROM state_events WHERE object_id=? ORDER BY id"
    assert col(bouncer, sql, post_oid) == ["remove_requested"]
    bouncer.sync_thread(thread, force=True)
    assert col(bouncer, sql, post_oid) == ["remove_requested", "removed"]
    ev = one(bouncer, "SELECT * FROM state_events WHERE object_id=? AND event_type='removed'", post_oid)
    assert ev["reason"] == "off-topic" and ev["attribution"] == "moderator"
    # the archive still has the post body the server now withholds
    assert col(bouncer, "SELECT body FROM revisions WHERE object_id=? ORDER BY seq DESC", post_oid)[0] == "body"


def test_lock_and_pin_are_tracked(server, bouncer, poster, mod, thread):
    account = poster.add(HOME, "dave", "hunter2")
    make_dave_a_mod(server, bouncer, thread)
    post_oid = one(bouncer, "SELECT root_object_id FROM archived_threads WHERE id=?", thread)[0]
    mod.lock(account, post_oid)
    mod.pin(account, post_oid)
    bouncer.sync_thread(thread, force=True)
    row = one(bouncer, "SELECT cur_locked, cur_featured FROM objects WHERE id=?", post_oid)
    assert (row[0], row[1]) == (1, 1)
    comment_oid = one(bouncer, "SELECT id FROM objects WHERE object_type='comment'")[0]
    with pytest.raises(AccountError, match="Only posts"):
        mod.pin(account, comment_oid)


def test_community_ban_and_unban_are_logged(server, bouncer, poster, mod, thread, cid):
    account = poster.add(HOME, "dave", "hunter2")
    make_dave_a_mod(server, bouncer, thread)
    comment_oid = one(bouncer, "SELECT id FROM objects WHERE object_type='comment'")[0]
    person = mod.ban_author(account, comment_oid, "rude", days=7)
    assert person.ap_id == "https://other.test/u/bob" and server.community_bans
    server_list, ours = mod.community_bans(account, cid)
    assert server_list is None and [r["target"] for r in ours] == ["bob@other.test"]
    mod.community_ban(account, cid, "https://other.test/u/bob", ban=False)
    assert not server.community_bans and mod.community_bans(account, cid)[1] == []


def test_add_and_remove_moderator(server, bouncer, poster, mod, thread, cid):
    account = poster.add(HOME, "dave", "hunter2")
    make_dave_a_mod(server, bouncer, thread)
    mods = mod.set_moderator(account, cid, "carol@home.test", True)
    assert "https://home.test/u/carol" in [m.ap_id for m in mods]
    mods = mod.set_moderator(account, cid, "https://home.test/u/carol", False)
    assert "https://home.test/u/carol" not in [m.ap_id for m in mods]
    events = col(bouncer, "SELECT event_type FROM state_events WHERE community_id=? ORDER BY id", cid)
    assert events.count("moderator_added") >= 2 and "moderator_removed" in events


def test_admin_tools(server, poster, mod):
    account = poster.add(HOME, "dave", "hunter2")
    with pytest.raises(AccountError, match="isn't an admin"):
        mod.site_settings(account)
    server.admins.add("dave")
    account = poster.refresh_roles(account)
    mod.site_ban(account, "bob@other.test", True, "spam")
    assert [p.ap_id for p in mod.site_settings(account)["banned"]] == ["https://other.test/u/bob"]
    with pytest.raises(AccountError, match="That's you"):
        mod.site_ban(account, "dave@home.test", True, "oops")
    assert mod.block_instance(account, "https://Bad.Example/", True) == ["bad.example"]
    with pytest.raises(AccountError, match="own server"):
        mod.block_instance(account, HOME, True)
    assert mod.block_link_domain(account, "spam.example", True) == ["https://spam.example/"]
    assert mod.block_link_domain(account, "spam.example", False) == []
    mod.set_registration_mode(account, "Closed")
    assert server.site["registration_mode"] == "Closed"
    actions = [r["action"] for r in mod.recent_actions(account_id=account.id)]
    assert {"site_ban", "block_instance", "registration_mode"} <= set(actions)


def test_moderation_web(settings, server, bouncer, thread, cid):
    settings.credentials_key = "test-key"
    client = TestClient(create_app(settings, bouncer))
    client.post("/login", data={"password": "pw"})
    client.post("/accounts", data={"server": HOME, "username": "dave", "password": "hunter2"})
    assert "Mod</summary>" not in client.get(f"/t/{thread}").text
    make_dave_a_mod(server, bouncer, thread)
    page = client.get(f"/t/{thread}").text
    assert "Mod</summary>" in page and "Lock comments" in page and "Ban bob@other.test" in page
    tab = client.get(f"/c/{cid}?tab=mod").text
    assert "Moderators" in tab and "Add moderator" in tab and "Ban from community" in tab
    post_oid = one(bouncer, "SELECT root_object_id FROM archived_threads WHERE id=?", thread)[0]
    r = client.post(f"/o/{post_oid}/mod", data={"action": "remove", "reason": "spam"},
                    headers={"referer": f"http://testserver/t/{thread}"}, follow_redirects=False)
    assert r.status_code == 303 and server.posts["1"].removed
    server.admins.add("dave")
    client.post(f"/accounts/{one(bouncer, 'SELECT id FROM accounts')[0]}/refresh")
    admin = client.get("/admin").text
    assert "Blocked instances" in admin and "Sign-ups" in admin
