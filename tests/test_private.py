"""Private communities: the approved list, join requests and revoking access."""

from __future__ import annotations

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from threadbnc.accounts import AccountError
from threadbnc.private import handle_of
from threadbnc.web import create_app

HOME = "home.test"
BOB = "https://other.test/u/bob"
CAROL = "https://home.test/u/carol"


def one(b, sql, *args):
    with b.db.connect() as conn:
        return conn.execute(sql, args).fetchone()


@pytest.fixture
def club(settings, server, bouncer):
    """Dave, an admin on home.test, starts !club there and manages it from ThreadBNC."""
    settings.credentials_key = "test-key"
    server.admins.add("dave")
    client = TestClient(create_app(settings, bouncer))
    client.post("/login", data={"password": "pw"})
    client.post("/accounts", data={"server": HOME, "username": "dave", "password": "hunter2"})
    aid = one(bouncer, "SELECT id FROM accounts")[0]
    r = client.post("/communities/new", data={"account_id": aid, "name": "club", "title": "Club"},
                    follow_redirects=False)
    cid = int(r.headers["location"].rsplit("/", 1)[1])
    private = bouncer.hooks[0].__self__  # the PrivateCommunities the app hooked into the bouncer
    return client, cid, f"https://{HOME}/c/club", private


def post(client, path, **data):
    return client.post(path, data=data)  # no Referer: lands on each action's fallback page


def status(bouncer, cid, handle):
    row = one(bouncer, "SELECT status, decided_by FROM join_requests WHERE community_id=? AND handle=?", cid, handle)
    return tuple(row) if row else None


@pytest.mark.parametrize("text,handle", [
    ("bob@other.test", "bob@other.test"), ("@Bob@Other.Test", "bob@other.test"),
    ("https://other.test/u/bob", "bob@other.test"), ("https://masto.test/@bob", "bob@masto.test"),
    (" carol@home.test:8443 ", "carol@home.test:8443"),
])
def test_usernames_are_normalised(text, handle):
    assert handle_of(text) == handle


@pytest.mark.parametrize("text", ["bob", "bob@", "@other.test", "bob@localhost", "b ob@other.test"])
def test_bad_usernames_are_refused(text):
    with pytest.raises(AccountError):
        handle_of(text)


def test_making_it_private_and_letting_listed_people_in(server, bouncer, club):
    client, cid, ap, private = club
    page = client.get(f"/c/{cid}?tab=mod").text
    assert "Membership" in page and 'value="public" selected' in page
    r = client.post(f"/c/{cid}/visibility", data={"visibility": "private"})
    assert "Now private" in r.text and server.visibility["77"] == "private"
    assert 'value="private" selected' in r.text

    # Bob is on the list; Carol isn't.
    r = post(client, f"/c/{cid}/members", who="@Bob@other.test", action="add")
    assert "bob@other.test is on the list" in r.text
    server.ask_to_join(BOB, ap)
    server.ask_to_join(CAROL, ap)
    assert private.sweep(force=True) == 1
    assert server.follow_state(BOB) == "accepted" and server.follow_state(CAROL) == "approval_required"
    assert status(bouncer, cid, "bob@other.test") == ("approved", "list")
    assert status(bouncer, cid, "carol@home.test") == ("waiting", None)
    page = client.get(f"/c/{cid}?tab=mod").text
    assert "Waiting to join" in page and "carol@home.test" in page and "Approve and add to list" in page

    # A second sweep inside a minute does nothing; a forced one finds nothing new.
    lists = server.join_request_lists
    assert private.sweep() == 0 and server.join_request_lists == lists
    assert private.sweep(force=True) == 0


def test_adding_someone_who_is_already_waiting_lets_them_in_at_once(server, bouncer, club):
    client, cid, ap, private = club
    client.post(f"/c/{cid}/visibility", data={"visibility": "private"})
    server.ask_to_join(CAROL, ap)
    private.sweep(force=True)
    assert status(bouncer, cid, "carol@home.test") == ("waiting", None)
    post(client, f"/c/{cid}/members", who="carol@home.test", action="add")
    assert server.follow_state(CAROL) == "accepted"
    assert status(bouncer, cid, "carol@home.test") == ("approved", "list")


@pytest.mark.parametrize("action,state,listed", [("approve_add", "accepted", True), ("approve", "accepted", False),
                                                 ("deny", "denied", False)])
def test_answering_by_hand(server, bouncer, club, action, state, listed):
    client, cid, ap, private = club
    client.post(f"/c/{cid}/visibility", data={"visibility": "private"})
    server.ask_to_join(CAROL, ap)
    private.sweep(force=True)
    rid = one(bouncer, "SELECT id FROM join_requests WHERE handle='carol@home.test'")[0]
    r = post(client, f"/c/{cid}/join-requests/{rid}", action=action)
    assert r.url.path == f"/c/{cid}" and "tab=mod" in str(r.url)  # back to the Mod tab, not the start page
    assert server.follow_state(CAROL) == state
    assert status(bouncer, cid, "carol@home.test") == ("denied" if action == "deny" else "approved", "you")
    assert bool(one(bouncer, "SELECT 1 FROM private_members WHERE handle='carol@home.test'")) is listed
    r = post(client, f"/c/{cid}/join-requests/{rid}", action=action)
    assert "already been answered" in r.text


def test_requests_answered_in_lemmy_itself_are_noticed(server, bouncer, club):
    client, cid, ap, private = club
    client.post(f"/c/{cid}/visibility", data={"visibility": "private"})
    server.ask_to_join(CAROL, ap)
    private.sweep(force=True)
    server.follows[0][2] = "accepted"  # a moderator approved it on the Lemmy website
    private.sweep(force=True)
    assert status(bouncer, cid, "carol@home.test")[0] == "elsewhere"


def test_taking_someone_off_the_list(server, bouncer, club):
    client, cid, ap, private = club
    client.post(f"/c/{cid}/visibility", data={"visibility": "private"})
    post(client, f"/c/{cid}/members", who="bob@other.test", action="add")
    server.ask_to_join(BOB, ap)
    private.sweep(force=True)

    # Without revoking: off the list, still a member, not banned.
    r = post(client, f"/c/{cid}/members", who="bob@other.test", action="remove")
    assert "They keep access" in r.text and server.community_bans == []
    # Revoking bans them from the community.
    post(client, f"/c/{cid}/members", who="bob@other.test", action="add")
    r = post(client, f"/c/{cid}/members", who="bob@other.test", action="remove", revoke="1")
    assert "access is revoked" in r.text and server.community_bans == [("77", BOB)]
    assert status(bouncer, cid, "bob@other.test") == ("revoked", "you")
    assert one(bouncer, "SELECT reason FROM mod_actions WHERE action='community_ban'")[0].startswith("Removed")
    # Putting them back lifts our ban; they must ask again.
    post(client, f"/c/{cid}/members", who="bob@other.test", action="add")
    assert server.community_bans == []
    assert status(bouncer, cid, "bob@other.test") == ("unbanned", "you")
    server.ask_to_join(BOB, ap)
    private.sweep(force=True)
    assert server.follow_state(BOB) == "accepted"


def test_public_communities_are_not_swept(server, bouncer, club):
    client, cid, ap, private = club
    client.post(f"/c/{cid}/visibility", data={"visibility": "private"})
    client.post(f"/c/{cid}/visibility", data={"visibility": "public"})
    lists = server.join_request_lists
    post(client, f"/c/{cid}/members", who="bob@other.test", action="add")  # list kept for later
    assert private.sweep(force=True) == 0 and server.join_request_lists == lists


def test_not_offered_before_lemmy_1(server, bouncer, club):
    client, cid, ap, private = club
    server.v4 = False
    assert "Membership" not in client.get(f"/c/{cid}?tab=mod").text
    r = post(client, f"/c/{cid}/visibility", visibility="private")
    assert "need Lemmy 1.0" in r.text and "77" not in server.visibility


def test_needs_a_moderator_account_on_the_communitys_own_server(server, bouncer, club):
    client, cid, ap, private = club
    server.users["dave2"] = "pw2"
    client.post("/accounts", data={"server": "elsewhere.test", "username": "dave2", "password": "pw2"})
    aid = one(bouncer, "SELECT id FROM accounts WHERE domain='elsewhere.test'")[0]
    with bouncer.db.transaction() as conn:  # make the remote account a moderator too
        conn.execute("UPDATE communities SET moderators_json=? WHERE id=?",
                     (f'["https://{HOME}/u/dave", "https://elsewhere.test/u/dave2"]', cid))
    client.post("/accounts/act-as", data={"account_id": aid})
    r = post(client, f"/c/{cid}/visibility", visibility="private")
    assert f"Join requests are handled on {HOME}" in r.text and "77" not in server.visibility


def test_a_broken_account_is_reported_on_the_mod_tab(server, bouncer, club):
    client, cid, ap, private = club
    client.post(f"/c/{cid}/visibility", data={"visibility": "private"})
    server.revoked = True  # the server logged the account out
    private.sweep(force=True)
    err = one(bouncer, "SELECT last_error FROM private_communities WHERE community_id=?", cid)[0]
    assert "log in again" in err


# ---- archiving as a member ---------------------------------------------------------------

def test_private_communities_are_archived_as_a_member(server, bouncer, club):
    client, cid, ap, private = club
    post = server.add_post("1", "Members only", "secret")
    server.edit_post("1", community=replace(post.community, ap_id=ap, name="club", domain=HOME))
    server.members_only = True
    assert bouncer.poll_follow(cid) == 0  # anonymous: nothing visible
    client.post(f"/c/{cid}/visibility", data={"visibility": "private"})
    assert bouncer.poll_follow(cid) == 1  # as dave, a moderator and so a member
    tid = one(bouncer, "SELECT id FROM archived_threads")[0]
    server.add_comment("1", "c1", "members chatting")
    bouncer.sync_thread(tid, force=True)
    assert "members chatting" in client.get(f"/t/{tid}").text
    assert "Members only" in client.get(f"/c/{cid}?tab=live").text


def test_member_reads_are_only_for_managed_private_communities(server, bouncer, club):
    client, cid, ap, private = club
    assert private.read_token(HOME, cid) is None  # not managed yet
    client.post(f"/c/{cid}/visibility", data={"visibility": "private"})
    assert private.read_token(HOME, cid) and private.read_token("other.test", cid) is None
    client.post(f"/c/{cid}/visibility", data={"visibility": "public"})
    assert private.read_token(HOME, cid) is None  # public again: back to anonymous reads
