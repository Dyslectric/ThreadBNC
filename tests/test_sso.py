"""Single sign-on: adding accounts by signing in with the server's identity provider."""

from __future__ import annotations

import re
import secrets
from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from threadbnc.db import fmt_ts, parse_ts, utcnow
from threadbnc.sso import SingleSignOn
from threadbnc.web import create_app

HOME = "home.test"
IDP = "https://auth.test/application/o/lemmy/authorize/"


def one(b, sql, *args):
    with b.db.connect() as conn:
        return conn.execute(sql, args).fetchone()


class IdentityProvider:
    """Plays Authentik: signs the person in and sends the browser back with a code."""

    def __init__(self, server):
        self.server = server

    def sign_in(self, authorize_url: str, identity: str) -> tuple[str, str]:
        q = {k: v[0] for k, v in parse_qs(urlparse(authorize_url).query).items()}
        assert authorize_url.startswith(IDP) and q["response_type"] == "code"
        assert q["redirect_uri"] == f"https://{HOME}/oauth/callback"
        code = secrets.token_hex(8)
        self.server.oauth_codes[code] = (identity, q.get("code_challenge"))
        return q["state"], code


@pytest.fixture
def app(settings, server, bouncer, monkeypatch):
    """ThreadBNC with home.test offering "Authentik" sign-in, whose return
    address forwards to ThreadBNC (unless a test says otherwise)."""
    settings.credentials_key = "test-key"
    server.oauth_providers.append({"id": 1, "display_name": "Authentik", "authorization_endpoint": IDP,
                                   "client_id": "lemmy", "scopes": "openid email profile", "use_pkce": True,
                                   "enabled": True, "issuer": "https://auth.test/application/o/lemmy/",
                                   "id_claim": "sub", "client_secret": "s3cret"})
    forwards = {"on": True}
    monkeypatch.setattr(SingleSignOn, "forwards_here", lambda self, domain, ours: forwards["on"])
    client = TestClient(create_app(settings, bouncer))
    client.post("/login", data={"password": "pw"})
    return client, forwards


def returning(client_app, state, code):
    """The browser coming back from the provider: a cross-site navigation, so
    ThreadBNC's (SameSite=strict) session cookie isn't sent."""
    stranger = TestClient(client_app)
    return stranger.get("/accounts/sso/callback", params={"state": state, "code": code}, follow_redirects=False)


def start(client, provider=1):
    r = client.get("/accounts/sso/start", params={"server": HOME, "provider": provider}, follow_redirects=False)
    assert r.status_code == 303
    return r.headers["location"]


def test_the_button_appears_only_when_the_return_address_leads_here(server, app):
    client, forwards = app
    opts = client.get("/accounts/sign-in-options", params={"server": HOME}).json()
    assert opts == {"domain": HOME, "ready": True, "note": None, "providers": [{"id": 1, "name": "Authentik"}]}
    page = client.get("/accounts", params={"server": HOME}).text
    assert "Sign in with Authentik" in page and "provider=1" in page
    forwards["on"] = False
    opts = client.get("/accounts/sign-in-options", params={"server": HOME}).json()
    assert not opts["ready"] and "sign you into that server's website" in opts["note"]
    server.v4 = False  # Lemmy 0.19: nothing offered
    assert client.get("/accounts/sign-in-options", params={"server": HOME}).json()["providers"] == []


def test_signing_in_to_a_linked_account(server, bouncer, app):
    client, _ = app
    server.oauth_links[(1, "dave-sub")] = "dave"
    url = start(client)
    q = parse_qs(urlparse(url).query)
    assert q["code_challenge_method"] == ["S256"] and len(q["state"][0]) >= 32
    state, code = IdentityProvider(server).sign_in(url, "dave-sub")
    r = returning(client.app, state, code)
    assert r.status_code == 200 and f'url=/accounts/sso/{state}' in r.text  # moves on by itself
    r = client.get(f"/accounts/sso/{state}")
    assert r.url.path == "/accounts" and "Signed in as dave@home.test" in r.text
    assert tuple(one(bouncer, "SELECT username, domain FROM accounts")) == ("dave", HOME)
    # The return page again (a reload): the spent code isn't reused, nothing changes.
    assert returning(client.app, state, code).status_code == 200
    assert one(bouncer, "SELECT COUNT(*) FROM accounts")[0] == 1


def test_a_new_person_picks_a_username(server, bouncer, app):
    client, _ = app
    server.site["oauth_registration"] = True
    idp = IdentityProvider(server)
    state, code = idp.sign_in(start(client), "erin-sub")
    returning(client.app, state, code)
    page = client.get(f"/accounts/sso/{state}").text
    assert "New account on home.test" in page and 'name="username"' in page
    server.users["taken"] = "pw"
    r = client.post(f"/accounts/sso/{state}/retry", data={"username": "taken"}, follow_redirects=False)
    go = r.headers["location"]
    assert go.startswith("/accounts/sso/go/")
    page = client.get(go).text
    url = re.search(r'content="0;url=([^"]+)"', page).group(1).replace("&amp;", "&")
    state2, code2 = idp.sign_in(url, "erin-sub")
    returning(client.app, state2, code2)
    page = client.get(f"/accounts/sso/{state2}").text
    assert "taken@home.test is taken" in page  # asked again
    r = client.post(f"/accounts/sso/{state2}/retry", data={"username": "erin"}, follow_redirects=False)
    url = re.search(r'content="0;url=([^"]+)"', client.get(r.headers["location"]).text).group(1).replace("&amp;", "&")
    state3, code3 = idp.sign_in(url, "erin-sub")
    returning(client.app, state3, code3)
    assert "Signed in as erin@home.test" in client.get(f"/accounts/sso/{state3}").text
    assert server.oauth_links[(1, "erin-sub")] == "erin"


def test_unlinked_sign_in_when_sign_ups_are_off(server, bouncer, app):
    client, _ = app
    state, code = IdentityProvider(server).sign_in(start(client), "stranger-sub")
    returning(client.app, state, code)
    r = client.get(f"/accounts/sso/{state}")
    assert "doesn&#39;t let new accounts sign up this way" in r.text or "doesn't let new accounts" in r.text
    assert one(bouncer, "SELECT COUNT(*) FROM accounts")[0] == 0


def test_the_return_page_needs_a_known_fresh_state(server, bouncer, app):
    client, _ = app
    r = returning(client.app, "made-up", "code")
    assert r.status_code == 400 and "unknown or was already used" in r.text
    server.oauth_links[(1, "dave-sub")] = "dave"
    state, code = IdentityProvider(server).sign_in(start(client), "dave-sub")
    with bouncer.db.transaction() as conn:  # 20 minutes pass
        conn.execute("UPDATE sso_logins SET created_at=? WHERE state=?",
                     (fmt_ts(parse_ts(utcnow()) - timedelta(minutes=20)), state))
    returning(client.app, state, code)
    assert "took too long" in client.get(f"/accounts/sso/{state}").text
    assert one(bouncer, "SELECT COUNT(*) FROM accounts")[0] == 0


def test_the_provider_refusing(server, bouncer, app):
    client, _ = app
    state, _ = IdentityProvider(server).sign_in(start(client), "x")
    TestClient(client.app).get("/accounts/sso/callback", params={"state": state, "error": "access_denied"})
    assert "access_denied" in client.get(f"/accounts/sso/{state}").text


def test_the_pkce_verifier_must_match(server, bouncer, app):
    client, _ = app
    server.oauth_links[(1, "dave-sub")] = "dave"
    state, code = IdentityProvider(server).sign_in(start(client), "dave-sub")
    with bouncer.db.transaction() as conn:
        conn.execute("UPDATE sso_logins SET verifier='wrong-verifier-wrong-verifier-wrong-verifier' WHERE state=?",
                     (state,))
    returning(client.app, state, code)
    assert "rejected the sign-in" in client.get(f"/accounts/sso/{state}").text


def test_only_the_return_page_skips_the_threadbnc_login(settings, server, bouncer, app):
    stranger = TestClient(app[0].app)
    assert stranger.get("/accounts/sso/start", params={"server": HOME, "provider": 1},
                        follow_redirects=False).headers["location"] == "/login?next=%2Faccounts%2Fsso%2Fstart%3Fserver%3Dhome.test%26provider%3D1"
    assert stranger.get("/accounts/sso/somestate", follow_redirects=False).headers["location"].startswith("/login")


# ---- the admin side ---------------------------------------------------------------------

@pytest.fixture
def admin(settings, server, bouncer, monkeypatch):
    settings.credentials_key = "test-key"
    server.admins.add("dave")
    monkeypatch.setattr(SingleSignOn, "forwards_here", lambda self, domain, ours: False)
    monkeypatch.setattr(SingleSignOn, "discover", lambda self, issuer: {
        "issuer": issuer, "authorization_endpoint": IDP, "token_endpoint": "https://auth.test/application/o/token/",
        "userinfo_endpoint": "https://auth.test/application/o/userinfo/"})
    client = TestClient(create_app(settings, bouncer))
    client.post("/login", data={"password": "pw"})
    client.post("/accounts", data={"server": HOME, "username": "dave", "password": "hunter2"})
    return client, one(bouncer, "SELECT id FROM accounts")[0]


def test_adding_and_managing_a_provider(server, admin):
    client, aid = admin
    page = client.get("/admin").text
    assert "Single sign-on" in page and f"https://{HOME}/oauth/callback" in page and "No sign-in providers" in page
    r = client.post(f"/admin/{aid}/sso/providers", data={
        "name": "Authentik", "issuer": "https://auth.test/application/o/lemmy/", "client_id": "lemmy",
        "client_secret": "s3cret", "scopes": "openid email profile", "id_claim": "sub", "use_pkce": "1"})
    assert "Added Authentik" in r.text
    [p] = server.oauth_providers
    assert p["authorization_endpoint"] == IDP and p["client_secret"] == "s3cret" and p["use_pkce"] is True
    assert "s3cret" not in r.text  # never shown back
    assert "doesn&#39;t lead back to ThreadBNC yet" in r.text or "doesn't lead back to ThreadBNC yet" in r.text
    assert "INSERT INTO oauth_account" in r.text and "SELECT lu.id, 1, 'PROVIDER_USERNAME'" in r.text
    assert "WHERE p.local AND p.name = 'dave';" in r.text
    client.post(f"/admin/{aid}/sso/providers/1", data={"action": "disable"})
    assert server.oauth_providers[0]["enabled"] is False
    client.post(f"/admin/{aid}/sso/signups", data={"allowed": "1"})
    assert server.site["oauth_registration"] is True
    client.post(f"/admin/{aid}/sso/signups", data={})
    assert server.site["oauth_registration"] is False
    client.post(f"/admin/{aid}/sso/providers/1", data={"action": "delete"})
    assert server.oauth_providers == []


def test_identifying_people_by_a_custom_claim(server, admin):
    """E.g. an Authentik scope mapping that sends dyslectric_dot_dev_username."""
    client, aid = admin
    client.post(f"/admin/{aid}/sso/providers", data={
        "name": "Authentik", "issuer": "https://auth.test/application/o/lemmy/", "client_id": "lemmy",
        "client_secret": "s3cret", "use_pkce": "1"})
    r = client.post(f"/admin/{aid}/sso/providers/1/edit", data={
        "name": "Authentik", "scopes": "openid  email profile dyslectric", "id_claim": "dyslectric_dot_dev_username"})
    assert "identified by their dyslectric_dot_dev_username claim" in r.text
    p = server.oauth_providers[0]
    assert (p["id_claim"], p["scopes"]) == ("dyslectric_dot_dev_username", "openid email profile dyslectric")
    assert "p.name FROM local_user" in r.text and "ON CONFLICT DO NOTHING" in r.text  # link everyone by name
    assert "PROVIDER_USERNAME" not in r.text
    r = client.post(f"/admin/{aid}/sso/providers/1/edit", data={"name": "A", "scopes": "email", "id_claim": "x"})
    assert "must include openid" in r.text and server.oauth_providers[0]["id_claim"] == "dyslectric_dot_dev_username"


def test_the_forwarding_check(settings, bouncer):
    from threadbnc.accounts import Poster
    from threadbnc.vault import TokenVault

    answers = {}

    class Http:
        def redirect_target(self, domain, path):
            assert path.startswith("/oauth/callback")
            return answers.get(domain)

    bouncer.http = Http()
    sso = SingleSignOn(Poster(bouncer, TokenVault("k", settings.data_dir)))
    ours = "https://threadbnc.example/accounts/sso/callback"
    answers["a.test"] = "https://threadbnc.example/accounts/sso/callback?threadbnc_check=1"
    answers["b.test"] = "https://elsewhere.example/accounts/sso/callback?x=1"
    assert sso.forwards_here("a.test", ours) is True
    assert sso.forwards_here("b.test", ours) is False
    assert sso.forwards_here("c.test", ours) is False  # no redirect at all
