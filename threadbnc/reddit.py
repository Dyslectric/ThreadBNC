"""Connecting to Reddit, and the client the Reddit adapter reads through.

Reddit's API needs an app registered at https://www.reddit.com/prefs/apps.
Two ways to connect:

* **Log in with Reddit** (a "web app" or "installed app"): you approve
  ThreadBNC on reddit.com and it keeps the refresh token Reddit gives back,
  encrypted like account tokens (see vault.py). Your Reddit password never
  passes through ThreadBNC. This can also list your subscriptions to follow,
  and vote, comment and post as you (the account shows up on the Accounts page
  and is used automatically for anything on Reddit; see accounts.Poster).
* **App only** (the app's id and secret, i.e. an API key): reads public
  subreddits without any Reddit account.
* **Browser cookie**: your reddit_session cookie, copied from a browser that's
  logged in to Reddit. No app needed (new apps need Reddit's approval since
  late 2025). Reads go through reddit.com's own .json pages and writes through
  the endpoints old.reddit uses, as your account. Reddit's terms don't provide
  for this, so it carries some risk to that account. ThreadBNC still says
  who it is in its User-Agent rather than passing itself off as a browser.

Being polite matters more here than with Lemmy: Reddit allows an app about
100 requests a minute and blocks clients that ignore that. So requests are
spaced out (THREADBNC_REDDIT_MIN_REQUEST_INTERVAL, 2 s by default), Reddit's
X-Ratelimit headers are obeyed, and when a sign-in stops working everything
waits for you to reconnect instead of retrying it.
"""

from __future__ import annotations

import hmac
import json
import logging
import secrets
import threading
import time
from datetime import timedelta
from typing import Any
from urllib.parse import urlencode, urljoin

import httpx

from . import __version__
from .adapters.base import RemoteNotFound, RemoteRejected, RemoteUnavailable
from .adapters.http import HostThrottle
from .db import Database, parse_ts, utcnow
from .vault import TokenVault, VaultError

log = logging.getLogger("threadbnc.reddit")

WWW = "https://www.reddit.com"
API = "https://oauth.reddit.com"
CALLBACK_PATH = "/reddit/callback"
SCOPES = "identity read mysubreddits vote submit edit privatemessages"
WRITE_SCOPES = {"vote", "submit", "edit"}  # sign-ins from before these were asked for can only read
INBOX_SCOPE = "privatemessages"  # replies, mentions and messages; older sign-ins lack it
SETTING = "reddit_connection"
PENDING_SETTING = "reddit_login_pending"
PENDING = timedelta(minutes=15)
LOW_REMAINING = 10  # requests left in Reddit's window below which we wait for it to reset
INSTALLED_GRANT = "https://oauth.reddit.com/grants/installed_client"


class RedditError(Exception):
    """Shown to the user as-is."""


def callback_url(base_url: str) -> str:
    return base_url.rstrip("/") + CALLBACK_PATH


class RedditConnection:
    def __init__(self, db: Database, vault: TokenVault, *, timeout: float = 20.0, min_interval: float = 2.0,
                 transport: httpx.BaseTransport | None = None):
        self.db = db
        self.vault = vault
        self._http = httpx.Client(timeout=timeout, transport=transport)
        self.throttle = HostThrottle(min_interval)
        self._lock = threading.Lock()
        self._token: tuple[str, float] | None = None  # access token, monotonic expiry
        self.paused_until: float = 0.0  # monotonic; Reddit asked us to wait

    # -- stored connection ------------------------------------------------------
    def config(self) -> dict[str, Any] | None:
        raw = self.db.get_setting(SETTING)
        return json.loads(raw) if raw else None

    def _save(self, key: str, value: dict[str, Any] | None) -> None:
        with self.db.transaction() as conn:
            if value is None:
                conn.execute("DELETE FROM app_settings WHERE key=?", (key,))
            else:
                conn.execute("INSERT INTO app_settings(key, value) VALUES (?, ?) "
                             "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value)))

    def _mark(self, status: str, error: str | None) -> None:
        cfg = self.config()
        if cfg:
            cfg.update(status=status, last_error=error)
            self._save(SETTING, cfg)

    def status(self) -> dict[str, Any] | None:
        """What the Reddit page shows; secrets left out."""
        cfg = self.config()
        if not cfg:
            return None
        wait = self.paused_until - time.monotonic()
        granted = set((cfg.get("scope") or "").replace(",", " ").split())
        can_write = cfg["mode"] == "cookie" or (cfg["mode"] == "user" and WRITE_SCOPES <= granted)
        can_inbox = cfg["mode"] == "cookie" or (cfg["mode"] == "user" and INBOX_SCOPE in granted)
        return {"mode": cfg["mode"], "client_id": cfg["client_id"], "username": cfg.get("username"),
                "status": cfg.get("status", "ok"), "last_error": cfg.get("last_error"),
                "connected_at": cfg.get("connected_at"), "paused_seconds": int(wait) if wait > 0 else 0,
                "can_write": can_write, "can_inbox": can_inbox,
                "has_account": cfg["mode"] in ("user", "cookie")}

    # -- connecting ---------------------------------------------------------------
    def connect_app(self, client_id: str, secret: str) -> dict[str, Any]:
        """Connect with an app's id and secret only (no Reddit account). Checked
        by getting a token straight away."""
        client_id, secret = client_id.strip(), secret.strip()
        if not client_id:
            raise RedditError("Enter the app's client id (under its name at reddit.com/prefs/apps).")
        cfg = {"mode": "app", "client_id": client_id, "secret_enc": self.vault.encrypt(secret) if secret else "",
               "status": "ok", "last_error": None, "connected_at": utcnow()}
        try:
            token, ttl = self._grant(cfg, self._app_grant(secret))
        except RemoteUnavailable as exc:
            raise RedditError(f"Reddit didn't accept that app: {exc}") from exc
        self._replace(cfg, token, ttl)
        return cfg

    def connect_cookie(self, text: str) -> dict[str, Any]:
        """Connect with a browser's Reddit session cookie. Accepts the
        reddit_session value on its own, or a whole Cookie header. Checked by
        asking Reddit who it belongs to."""
        cookie = _cookie_header(text)
        if not cookie:
            raise RedditError("Paste the value of your reddit_session cookie.")
        cfg = {"mode": "cookie", "client_id": "", "cookie_enc": self.vault.encrypt(cookie), "status": "ok",
               "last_error": None, "connected_at": utcnow()}
        try:
            me = self._whoami_cookie(cfg)
        except (RemoteUnavailable, RemoteNotFound, RemoteRejected) as exc:
            raise RedditError(f"Couldn't check that cookie with Reddit: {exc}") from exc
        if not me.get("name"):
            raise RedditError("Reddit didn't recognise that cookie: it may have expired, or be from a browser "
                              "that's logged out.")
        cfg.update(username=me["name"], modhash=me.get("modhash") or "")
        self._replace(cfg, None, 0)
        return cfg

    def _whoami_cookie(self, cfg: dict[str, Any]) -> dict[str, Any]:
        data = self._cookie_api("GET", "/api/me", {}, cfg=cfg, retry=False) or {}
        return data.get("data") or {} if isinstance(data, dict) else {}

    def start_login(self, client_id: str, secret: str, redirect_uri: str) -> str:
        """Begin "log in with Reddit". Returns the reddit.com page to approve on."""
        client_id = client_id.strip()
        if not client_id:
            raise RedditError("Enter the app's client id (under its name at reddit.com/prefs/apps).")
        state = secrets.token_urlsafe(24)
        url = f"{WWW}/api/v1/authorize?" + urlencode({
            "client_id": client_id, "response_type": "code", "state": state, "redirect_uri": redirect_uri,
            "duration": "permanent", "scope": SCOPES})
        self._save(PENDING_SETTING, {"state": state, "client_id": client_id, "redirect_uri": redirect_uri,
                                     "secret_enc": self.vault.encrypt(secret.strip()) if secret.strip() else "",
                                     "created_at": utcnow(), "authorize_url": url})
        return url

    def pending_url(self) -> str | None:
        """The reddit.com approval page of a login that's been started, if any."""
        raw = self.db.get_setting(PENDING_SETTING)
        pending = json.loads(raw) if raw else None
        started = parse_ts(pending["created_at"]) if pending else None
        if not started or parse_ts(utcnow()) - started > PENDING:  # type: ignore[operator]
            return None
        return pending["authorize_url"]

    def finish_login(self, state: str, code: str | None, error: str | None) -> tuple[bool, str]:
        """Reddit sent the browser back. Returns (connected, message)."""
        raw = self.db.get_setting(PENDING_SETTING)
        pending = json.loads(raw) if raw else None
        if not pending or not hmac.compare_digest(pending["state"], state or ""):
            return False, "That Reddit sign-in link is unknown or was already used. Start again."
        self._save(PENDING_SETTING, None)  # single use
        started = parse_ts(pending["created_at"])
        if started is None or parse_ts(utcnow()) - started > PENDING:  # type: ignore[operator]
            return False, "That Reddit sign-in took too long. Start again."
        if error or not code:
            return False, ("You declined on Reddit, so nothing was connected." if error == "access_denied"
                           else f"Reddit didn't finish the sign-in ({error or 'no code'}).")
        cfg = {"mode": "user", "client_id": pending["client_id"], "secret_enc": pending["secret_enc"],
               "status": "ok", "last_error": None, "connected_at": utcnow()}
        try:
            data = self._token_request(cfg, {"grant_type": "authorization_code", "code": code,
                                             "redirect_uri": pending["redirect_uri"]})
        except RemoteUnavailable as exc:
            return False, f"Reddit refused the sign-in: {exc}"
        if not data.get("refresh_token"):
            return False, "Reddit gave no lasting sign-in (refresh token); start again."
        cfg["refresh_enc"] = self.vault.encrypt(data["refresh_token"])
        cfg["scope"] = data.get("scope") or ""
        token, ttl = data["access_token"], float(data.get("expires_in") or 3600)
        try:
            me = self._api_get(token, "/api/v1/me", {}, cfg=cfg)
            cfg["username"] = (me or {}).get("name")
        except (RemoteUnavailable, RemoteNotFound, _Expired) as exc:
            log.info("couldn't learn the Reddit username: %s", exc)
        self._replace(cfg, token, ttl)
        who = f"u/{cfg['username']}" if cfg.get("username") else "your Reddit account"
        return True, f"Connected to Reddit as {who}."

    def _replace(self, cfg: dict[str, Any], token: str | None, ttl: float) -> None:
        old = self.config()
        self._save(SETTING, cfg)
        with self._lock:
            self._token = (token, time.monotonic() + ttl - 60) if token else None
            self.paused_until = 0.0
        if old and old.get("refresh_enc") and old.get("refresh_enc") != cfg.get("refresh_enc"):
            self._revoke(old)

    def disconnect(self) -> None:
        cfg = self.config()
        if cfg and cfg.get("refresh_enc"):
            self._revoke(cfg)
        self._save(SETTING, None)
        with self._lock:
            self._token = None

    def _revoke(self, cfg: dict[str, Any]) -> None:
        """End a lasting sign-in on Reddit's side too (best effort)."""
        try:
            self._http.post(f"{WWW}/api/v1/revoke_token", auth=self._basic(cfg), headers=self._headers(cfg),
                            data={"token": self.vault.decrypt(cfg["refresh_enc"]),
                                  "token_type_hint": "refresh_token"})
        except (httpx.HTTPError, VaultError) as exc:
            log.info("revoking the Reddit sign-in failed (forgetting it anyway): %s", exc)

    # -- tokens ---------------------------------------------------------------------
    def _headers(self, cfg: dict[str, Any] | None) -> dict[str, str]:
        # Reddit asks for "<platform>:<app id>:<version> (by /u/<name>)".
        by = f"; by /u/{cfg['username']}" if cfg and cfg.get("username") else ""
        return {"User-Agent": f"python:threadbnc:{__version__} (private archive{by})"}

    def _basic(self, cfg: dict[str, Any]) -> tuple[str, str]:
        secret = self.vault.decrypt(cfg["secret_enc"]) if cfg.get("secret_enc") else ""
        return cfg["client_id"], secret  # installed apps have no secret

    @staticmethod
    def _app_grant(secret: str) -> dict[str, str]:
        if secret:
            return {"grant_type": "client_credentials"}
        return {"grant_type": INSTALLED_GRANT, "device_id": "DO_NOT_TRACK_THIS_DEVICE"}

    def _token_request(self, cfg: dict[str, Any], form: dict[str, str]) -> dict[str, Any]:
        self.throttle.wait("www.reddit.com")
        try:
            resp = self._http.post(f"{WWW}/api/v1/access_token", auth=self._basic(cfg), data=form,
                                   headers=self._headers(cfg))
        except (httpx.HTTPError, VaultError) as exc:
            raise RemoteUnavailable(f"Reddit: {type(exc).__name__}: {exc}") from exc
        try:
            data = resp.json()
        except ValueError:
            data = {}
        if resp.status_code == 429 or resp.status_code >= 500:
            raise RemoteUnavailable(f"Reddit: HTTP {resp.status_code}")
        if resp.status_code >= 400 or not data.get("access_token"):
            err = data.get("error") or data.get("message") or f"HTTP {resp.status_code}"
            raise _Refused(f"Reddit sign-in refused ({err})")
        return data

    def _grant(self, cfg: dict[str, Any], form: dict[str, str]) -> tuple[str, float]:
        data = self._token_request(cfg, form)
        return data["access_token"], float(data.get("expires_in") or 3600)

    def _access_token(self, cfg: dict[str, Any]) -> str:
        with self._lock:
            if self._token and self._token[1] > time.monotonic():
                return self._token[0]
        try:
            if cfg["mode"] == "user":
                form = {"grant_type": "refresh_token", "refresh_token": self.vault.decrypt(cfg["refresh_enc"])}
            else:
                secret = self.vault.decrypt(cfg["secret_enc"]) if cfg.get("secret_enc") else ""
                form = self._app_grant(secret)
            token, ttl = self._grant(cfg, form)
        except VaultError as exc:
            self._mark("needs_login", str(exc))
            raise RemoteUnavailable("Reddit: the stored sign-in can't be decrypted; connect again.") from exc
        except _Refused as exc:  # not a network problem: don't keep asking until you reconnect
            self._mark("needs_login", str(exc))
            raise
        with self._lock:
            self._token = (token, time.monotonic() + ttl - 60)
        return token

    # -- reading ----------------------------------------------------------------------
    def _ready(self) -> dict[str, Any]:
        cfg = self.config()
        if not cfg:
            raise RemoteUnavailable("Reddit isn't connected. Connect it on the Reddit page first.")
        if cfg.get("status") == "needs_login":
            raise RemoteUnavailable("Reddit needs you to connect again (Reddit page): "
                                    + (cfg.get("last_error") or "the sign-in stopped working"))
        wait = self.paused_until - time.monotonic()
        if wait > 0:
            raise RemoteUnavailable(f"Reddit rate limit reached; waiting {int(wait) + 1} s before asking again")
        return cfg

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """One API read, as the connected app/account."""
        return self._call("GET", path, params or {})

    def post(self, path: str, form: dict[str, Any]) -> Any:
        """One API write, as the connected account. Reddit's own complaints
        (archived thread, rate limit, ...) come back as RemoteRejected."""
        data = self._call("POST", path, {"api_type": "json", **form})
        errors = ((data or {}).get("json") or {}).get("errors") if isinstance(data, dict) else None
        if errors:
            code, message = (list(errors[0]) + ["", ""])[:2]
            if str(code) == "USER_REQUIRED":  # the cookie's session has ended
                self._mark("needs_login", "Reddit says you're logged out")
                raise RemoteUnavailable("Reddit says you're logged out; paste a fresh cookie on the Reddit page.")
            raise RemoteRejected(f"Reddit refused: {message or code}", str(code))
        return data

    def _call(self, method: str, path: str, params: dict[str, Any]) -> Any:
        cfg = self._ready()
        if cfg["mode"] == "cookie":
            return self._cookie_api(method, path, params, cfg=cfg)
        try:
            return self._api(method, self._access_token(cfg), path, params, cfg=cfg)
        except _Expired:
            with self._lock:
                self._token = None
        try:  # the token was revoked or expired early: get a fresh one, once
            return self._api(method, self._access_token(cfg), path, params, cfg=cfg)
        except _Expired as exc:
            self._mark("needs_login", "Reddit stopped accepting the sign-in")
            raise RemoteUnavailable("Reddit stopped accepting the sign-in; connect again.") from exc

    def _api_get(self, token: str, path: str, params: dict[str, Any], *, cfg: dict[str, Any]) -> Any:
        return self._api("GET", token, path, params, cfg=cfg)

    def _api(self, method: str, token: str, path: str, params: dict[str, Any], *, cfg: dict[str, Any]) -> Any:
        self.throttle.wait("oauth.reddit.com")
        clean = {"raw_json": 1, **{k: v for k, v in params.items() if v is not None}}
        headers = {**self._headers(cfg), "Authorization": f"bearer {token}"}
        try:
            if method == "GET":
                resp = self._http.get(API + path, params=clean, headers=headers, follow_redirects=True)
            else:
                resp = self._http.post(API + path, data=clean, headers=headers)
        except httpx.HTTPError as exc:
            raise RemoteUnavailable(f"Reddit: {type(exc).__name__}: {exc}") from exc
        if resp.status_code == 401:
            self._note_limits(resp)
            raise _Expired()
        return self._check(resp, method, path)

    def _cookie_api(self, method: str, path: str, params: dict[str, Any], *, cfg: dict[str, Any],
                    retry: bool = True) -> Any:
        """A request as the browser session: reads from www.reddit.com's .json
        pages, writes to its /api endpoints with the session's modhash (Reddit's
        guard against cross-site requests)."""
        self.throttle.wait("www.reddit.com")
        clean = {"raw_json": 1, **{k: v for k, v in params.items() if v is not None}}
        try:
            headers = {**self._headers(cfg), "Cookie": self.vault.decrypt(cfg["cookie_enc"])}
        except VaultError as exc:
            self._mark("needs_login", str(exc))
            raise RemoteUnavailable("Reddit: the stored cookie can't be decrypted; paste it again.") from exc
        try:
            if method == "GET":
                resp = self._http.get(f"{WWW}{path}.json", params=clean, headers=headers, follow_redirects=True)
            else:
                resp = self._http.post(WWW + path, data=clean, headers={**headers, "X-Modhash": cfg.get("modhash", "")})
        except httpx.HTTPError as exc:
            raise RemoteUnavailable(f"Reddit: {type(exc).__name__}: {exc}") from exc
        if resp.status_code == 401 or "/login" in resp.url.path:
            self._note_limits(resp)
            self._mark("needs_login", "Reddit stopped accepting the cookie")
            raise RemoteUnavailable("Reddit stopped accepting the cookie; paste a fresh one on the Reddit page.")
        if resp.status_code == 403 and method != "GET" and retry:
            # A stale modhash looks like this: fetch the current one and try once more.
            me = self._whoami_cookie(cfg)
            if not me.get("name"):
                self._mark("needs_login", "Reddit says you're logged out")
                raise RemoteUnavailable("Reddit says you're logged out; paste a fresh cookie on the Reddit page.")
            cfg["modhash"] = me.get("modhash") or ""
            self._save(SETTING, cfg)
            return self._cookie_api(method, path, params, cfg=cfg, retry=False)
        return self._check(resp, method, path)

    def _check(self, resp: httpx.Response, method: str, path: str) -> Any:
        """Turn Reddit's answer into data, or the error the caller expects."""
        self._note_limits(resp)
        if resp.status_code == 404:
            raise RemoteNotFound(f"Reddit {path}: 404")
        if resp.status_code == 403 and method != "GET":
            raise RemoteRejected("Reddit refused: this sign-in isn't allowed to do that (connect Reddit again to "
                                 "allow voting, commenting and posting), or you're banned there.", "forbidden")
        if resp.status_code == 403:
            # Private, quarantined or banned subreddits; never taken as deletion.
            raise RemoteUnavailable(f"Reddit {path}: forbidden (private, quarantined or banned?)")
        if resp.status_code >= 400 and method != "GET" and resp.status_code != 429 and resp.status_code < 500:
            raise RemoteRejected(f"Reddit refused: HTTP {resp.status_code}", str(resp.status_code))
        if resp.status_code >= 400:
            raise RemoteUnavailable(f"Reddit {path}: HTTP {resp.status_code}")
        try:
            return resp.json()
        except ValueError as exc:
            raise RemoteUnavailable(f"Reddit {path}: sent a web page instead of data (it may be refusing "
                                    "automated requests right now)") from exc

    def _note_limits(self, resp: httpx.Response) -> None:
        """Obey Reddit's rate-limit headers: when the window is nearly used up
        (or we got a 429), stop until it resets."""
        try:
            remaining = float(resp.headers.get("x-ratelimit-remaining", "inf"))
            reset = float(resp.headers.get("x-ratelimit-reset", "60"))
        except ValueError:
            return
        if resp.status_code == 429 or remaining < LOW_REMAINING:
            self.paused_until = time.monotonic() + max(reset, 1.0)
            log.warning("Reddit rate limit: pausing %.0f s (%s requests left)", reset, remaining)

    def resolve_share(self, path: str) -> str | None:
        """Where an app share link (/r/name/s/code) leads, without following it further."""
        self.throttle.wait("www.reddit.com")
        try:
            resp = self._http.get(WWW + path, headers=self._headers(self.config()), follow_redirects=False)
        except httpx.HTTPError as exc:
            raise RemoteUnavailable(f"Reddit: {type(exc).__name__}: {exc}") from exc
        location = resp.headers.get("location")
        return urljoin(WWW, location) if resp.is_redirect and location else None

    def close(self) -> None:
        self._http.close()


def _cookie_header(text: str) -> str:
    """A Cookie header from what was pasted: a bare reddit_session value, or
    `reddit_session=...; other=...` (with or without a leading "Cookie:")."""
    text = text.strip()
    if text.lower().startswith("cookie:"):
        text = text[7:].strip()
    if not text:
        return ""
    return text if "=" in text else f"reddit_session={text}"


class _Refused(RemoteUnavailable):
    """Reddit's token endpoint said no (bad app credentials, revoked sign-in)."""


class _Expired(Exception):
    """The API said 401: the access token is no longer valid."""
