"""Signed in by a reverse proxy (Traefik + Authentik forward auth)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from threadbnc.web import PROXY_SECRET_HEADER, create_app

SECRET = "s" * 32


@pytest.fixture
def proxied(settings, bouncer):
    settings.proxy_auth_header = "X-authentik-username"
    settings.proxy_secret = SECRET
    return settings


def via_proxy(user="dave", secret=SECRET):
    return {"X-authentik-username": user, PROXY_SECRET_HEADER: secret}


def test_the_proxy_signs_people_in(proxied, bouncer):
    client = TestClient(create_app(proxied, bouncer))
    r = client.get("/communities", headers=via_proxy(), follow_redirects=False)
    assert r.status_code == 200 and "Log out" in r.text


@pytest.mark.parametrize("headers", [
    {"X-authentik-username": "dave"},                           # reached directly, no secret
    via_proxy(secret="wrong" * 8),                              # guessed secret
    {PROXY_SECRET_HEADER: SECRET},                              # secret but nobody signed in
    via_proxy(user="  "),
])
def test_the_header_alone_is_not_enough(proxied, bouncer, headers):
    client = TestClient(create_app(proxied, bouncer))
    r = client.get("/communities", headers=headers, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")


def test_only_listed_people_get_in(proxied, bouncer):
    proxied.proxy_allowed_users = ("dave",)
    client = TestClient(create_app(proxied, bouncer))
    assert client.get("/", headers=via_proxy("Dave")).status_code == 200
    r = TestClient(create_app(proxied, bouncer)).get("/", headers=via_proxy("mallory"))
    assert r.status_code == 403 and "mallory isn't allowed" in r.text


def test_a_proxy_session_needs_the_proxy_every_time(proxied, bouncer):
    client = TestClient(create_app(proxied, bouncer))
    assert client.get("/", headers=via_proxy()).status_code == 200
    # Same browser, same cookie, but not through the proxy (its session ended, or the app was reached directly).
    r = client.get("/communities", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    assert client.get("/", headers=via_proxy()).status_code == 200  # back through the proxy: signed in again


def test_password_sign_in_is_optional_with_a_proxy(proxied, bouncer):
    proxied.password = None
    client = TestClient(create_app(proxied, bouncer))
    page = client.get("/login").text
    assert "Password sign-in is turned off" in page and 'type="password"' not in page
    r = client.post("/login", data={"password": ""}, follow_redirects=False)
    assert r.status_code != 303  # nothing signs you in
    assert client.get("/", headers=via_proxy()).status_code == 200


def test_password_still_works_as_a_fallback(proxied, bouncer):
    client = TestClient(create_app(proxied, bouncer))
    assert "a fallback" in client.get("/login").text
    assert client.post("/login", data={"password": "pw"}, follow_redirects=False).headers["location"] == "/"
    assert client.get("/communities", follow_redirects=False).status_code == 200


def test_logging_out_ends_the_proxy_session_too(proxied, bouncer):
    proxied.proxy_logout_url = "/outpost.goauthentik.io/sign_out"
    client = TestClient(create_app(proxied, bouncer))
    client.get("/", headers=via_proxy())
    r = client.post("/logout", headers=via_proxy(), follow_redirects=False)
    assert r.headers["location"] == "/outpost.goauthentik.io/sign_out"
    pw = TestClient(create_app(proxied, bouncer))  # password sessions just go to /login
    pw.post("/login", data={"password": "pw"})
    assert pw.post("/logout", follow_redirects=False).headers["location"] == "/login"


def test_api_tokens_still_work_behind_the_proxy(proxied, bouncer):
    proxied.api_token = "tok"
    client = TestClient(create_app(proxied, bouncer))
    r = client.get("/api/jobs/1", headers={"Authorization": "Bearer tok"})
    assert r.status_code == 404  # authenticated; there's just no such job


@pytest.mark.parametrize("secret,password,message", [
    (None, "pw", "needs THREADBNC_PROXY_SECRET"),
    ("short", "pw", "needs THREADBNC_PROXY_SECRET"),
])
def test_misconfiguration_refuses_to_start(proxied, bouncer, secret, password, message):
    proxied.proxy_secret, proxied.password = secret, password
    with pytest.raises(SystemExit, match=message):
        create_app(proxied, bouncer)


def test_something_must_sign_people_in(settings, bouncer):
    settings.password = None
    with pytest.raises(SystemExit, match="never served unauthenticated"):
        create_app(settings, bouncer)
