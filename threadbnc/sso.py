"""Single sign-on: adding an account by signing in with the server's identity
provider (e.g. Authentik) instead of a password. Lemmy 1.0.

How it fits together. Lemmy only accepts one return address for SSO logins,
https://<server>/oauth/callback (normally a page of Lemmy's own web UI), and
checks it when the login is finished. So:

1. ThreadBNC sends you to the provider with that return address, a random
   `state` and a PKCE challenge, and remembers them (sso_logins).
2. The provider sends you back to https://<server>/oauth/callback, which the
   server's reverse proxy forwards to ThreadBNC's /accounts/sso/callback.
3. ThreadBNC hands the code to the server (/oauth/authenticate), which swaps it
   for your identity and returns a session, and keeps that session like a
   password login would.

ThreadBNC offers SSO for a server only when step 2 is in place: it checks
where the server's /oauth/callback redirects. Otherwise the provider would log
you into that server's own website instead.

The callback can't rely on the ThreadBNC session cookie (it's SameSite=strict
and the request comes from the provider's site), so it's authorised by the
unguessable, single-use state, and hands off to an ordinary page afterwards.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
from datetime import timedelta
from typing import Any
from urllib.parse import urlencode, urlparse

from .accounts import Account, AccountError, Poster, _domain_from
from .adapters import RemoteError
from .db import fmt_ts, parse_ts, utcnow

CALLBACK_PATH = "/accounts/sso/callback"
PENDING = timedelta(minutes=15)
FORWARD_CHECK_SECONDS = 600
DEFAULT_SCOPES = "openid email profile"

# What the server says when it needs more before it will finish a sign-in.
_NEEDS = {
    "registration_username_required": "need_username",
    "username_already_taken": "need_username",
    "registration_application_answer_required": "need_answer",
}
_FAILURES = {
    "oauth_registration_closed": "This sign-in isn't linked to an account on {domain}, and {domain} doesn't let "
                                 "new accounts sign up this way. To use an existing account, link it first "
                                 "(Admin → Single sign-on).",
    "email_already_taken": "An account on {domain} already uses this email but isn't linked to this sign-in. "
                           "Link it first (Admin → Single sign-on).",
    "oauth_authorization_invalid": "{domain} rejected the sign-in (the return address or provider settings "
                                   "don't match). Check Admin → Single sign-on.",
    "oauth_login_failed": "{domain} couldn't read your identity from the provider. Check the provider's "
                          "scopes and ID claim.",
    "registration_denied": "{domain} turned down the account application.",
    "registration_application_is_pending": "Your account on {domain} is waiting for an admin to approve it.",
    "email_not_verified": "Confirm your email address first (check your inbox), then sign in again.",
}


def callback_url(base_url: str) -> str:
    return base_url.rstrip("/") + CALLBACK_PATH


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:96]  # 43..128 unreserved characters
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


class SingleSignOn:
    def __init__(self, poster: Poster):
        self.poster = poster
        self.bouncer = poster.bouncer
        self.db = poster.db
        self._forwards: dict[str, tuple[bool, float]] = {}

    def _adapter(self, domain: str) -> Any:
        adapter = self.bouncer.adapter_for(domain)
        if not getattr(adapter, "supports_sso", False):
            raise AccountError(f"Single sign-on needs Lemmy 1.0 or later; {domain} isn't running it.")
        return adapter

    # -- discovery --------------------------------------------------------------
    def forwards_here(self, domain: str, our_callback: str) -> bool:
        """Does https://{domain}/oauth/callback redirect to our callback page?"""
        cached = self._forwards.get(domain)
        if cached and time.monotonic() - cached[1] < FORWARD_CHECK_SECONDS:
            return cached[0]
        try:
            target = self.bouncer.http.redirect_target(domain, "/oauth/callback?threadbnc_check=1")
        except RemoteError:
            target = None
        ours, there = urlparse(our_callback), urlparse(target or "")
        ok = bool(target) and (there.netloc, there.path) == (ours.netloc, ours.path)
        self._forwards[domain] = (ok, time.monotonic())
        return ok

    def options(self, server: str, our_callback: str) -> dict[str, Any]:
        """Sign-in providers to offer for `server` on the add-account form."""
        domain = _domain_from(server)
        try:
            adapter = self.bouncer.adapter_for(domain)
            providers = adapter.sign_in_options() if getattr(adapter, "supports_sso", False) else []
        except (RemoteError, AccountError):
            return {"domain": domain, "providers": [], "ready": False, "note": None}
        ready = bool(providers) and self.forwards_here(domain, our_callback)
        note = None
        if providers and not ready:
            note = (f"{domain} offers single sign-on, but its sign-in return page doesn't lead back to "
                    "ThreadBNC, so it would sign you into that server's website instead. Use your password.")
        return {"domain": domain, "ready": ready, "note": note,
                "providers": [{"id": p["id"], "name": p.get("display_name") or "single sign-on"}
                              for p in providers]}

    # -- signing in -------------------------------------------------------------------
    def start(self, server: str, provider_id: int, username: str | None = None, answer: str | None = None) -> str:
        """Remember a new sign-in and return the provider URL to send the browser to."""
        return self.pending(self.begin(server, provider_id, username, answer))["authorize_url"]

    def begin(self, server: str, provider_id: int, username: str | None = None, answer: str | None = None) -> str:
        """Remember a new sign-in; returns its state."""
        domain = _domain_from(server)
        adapter = self._adapter(domain)
        try:
            provider = next((p for p in adapter.sign_in_options() if p["id"] == provider_id), None)
        except RemoteError as exc:
            raise AccountError(f"Couldn't reach {domain}: {exc}") from exc
        if provider is None:
            raise AccountError(f"{domain} no longer offers that sign-in.")
        state = secrets.token_urlsafe(32)
        verifier, challenge = _pkce() if provider.get("use_pkce") else (None, None)
        params = {"response_type": "code", "client_id": provider["client_id"],
                  "redirect_uri": f"https://{domain}/oauth/callback",
                  "scope": provider.get("scopes") or DEFAULT_SCOPES, "state": state}
        if challenge:
            params.update(code_challenge=challenge, code_challenge_method="S256")
        endpoint = provider["authorization_endpoint"]
        url = endpoint + ("&" if "?" in endpoint else "?") + urlencode(params)
        now = utcnow()
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM sso_logins WHERE created_at<?",
                         (fmt_ts(parse_ts(now) - timedelta(days=1)),))  # type: ignore[operator]
            conn.execute(
                "INSERT INTO sso_logins(state, domain, provider_id, provider_name, verifier, username, answer, "
                "authorize_url, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (state, domain, provider_id, provider.get("display_name"), verifier,
                 (username or "").strip().lstrip("@") or None, (answer or "").strip() or None, url, now))
        return state

    def pending(self, state: str) -> Any:
        with self.db.connect() as conn:
            return conn.execute("SELECT * FROM sso_logins WHERE state=?", (state,)).fetchone()

    def finish(self, state: str, code: str | None, error: str | None = None) -> Any:
        """Handle the provider's return. Returns the updated row, or None if the
        state is unknown (then nothing is changed)."""
        row = self.pending(state)
        if row is None:
            return None
        if row["status"] != "started":
            return row  # a reload of the return page: already handled
        if parse_ts(utcnow()) - parse_ts(row["created_at"]) > PENDING:  # type: ignore[operator]
            return self._end(state, "failed", "That sign-in took too long; start again.")
        if error or not code:
            return self._end(state, "failed", f"The provider didn't sign you in ({error or 'no code returned'}).")
        domain = row["domain"]
        try:
            result = self._adapter(domain).oauth_authenticate(
                code, row["provider_id"], f"https://{domain}/oauth/callback", row["verifier"],
                row["username"], row["answer"])
        except AccountError as exc:
            return self._end(state, "failed", str(exc))
        except RemoteError as exc:
            code_name = getattr(exc, "code", "") or ""
            if code_name in _NEEDS:
                taken = code_name == "username_already_taken"
                return self._end(state, _NEEDS[code_name],
                                 f"{row['username']}@{domain} is taken; pick another name." if taken else None)
            message = _FAILURES.get(code_name, "{domain} refused the sign-in: " + str(exc))
            return self._end(state, "failed", message.format(domain=domain))
        if result.get("jwt"):
            try:
                account = self.poster.add_session(domain, str(result["jwt"]))
            except AccountError as exc:
                return self._end(state, "failed", str(exc))
            return self._end(state, "done", f"Signed in as {account.handle}.", account.id)
        if result.get("registration_created"):
            return self._end(state, "waiting", f"Account requested. It can be used once an admin of {domain} "
                                                "approves it; then sign in again.")
        if result.get("verify_email_sent"):
            return self._end(state, "waiting", f"{domain} sent you an email to confirm your address; do that, "
                                                "then sign in again.")
        return self._end(state, "failed", f"{domain} didn't return a session.")

    def _end(self, state: str, status: str, message: str | None, account_id: int | None = None) -> Any:
        with self.db.transaction() as conn:
            conn.execute("UPDATE sso_logins SET status=?, message=?, account_id=?, verifier=NULL WHERE state=?",
                         (status, message, account_id, state))
        return self.pending(state)

    def retry(self, state: str, username: str | None, answer: str | None) -> str:
        """Start over with the username/answer the server asked for (the first
        code is spent). Returns the new sign-in's state."""
        row = self.pending(state)
        if row is None or row["status"] not in ("need_username", "need_answer"):
            raise AccountError("That sign-in has ended; start again from the Accounts page.")
        return self.begin(row["domain"], row["provider_id"], username or row["username"],
                          answer or row["answer"])

    # -- admin: providers on a server you administer ------------------------------------
    def settings(self, account: Account, our_callback: str) -> dict[str, Any]:
        info = self.poster._run(account, lambda adapter, token: self._adapter(account.domain).sso_settings(token))
        info["return_address"] = f"https://{account.domain}/oauth/callback"
        info["forwards"] = self.forwards_here(account.domain, our_callback) if info["providers"] else None
        return info

    def discover(self, issuer: str) -> dict[str, str]:
        """Endpoints from the provider's OpenID configuration."""
        u = urlparse(issuer.strip())
        if u.scheme != "https" or not u.netloc:
            raise AccountError("The issuer must be an https:// URL, e.g. https://auth.example/application/o/lemmy/")
        path = u.path.rstrip("/") + "/.well-known/openid-configuration"
        try:
            conf = self.bouncer.http.get_json(u.netloc, path)
        except RemoteError as exc:
            raise AccountError(f"Couldn't read {u.netloc}{path}: {exc}") from exc
        missing = [k for k in ("issuer", "authorization_endpoint", "token_endpoint", "userinfo_endpoint")
                   if not conf.get(k)]
        if missing:
            raise AccountError(f"The provider's configuration is missing {', '.join(missing)}.")
        return {k: str(conf[k]) for k in ("issuer", "authorization_endpoint", "token_endpoint", "userinfo_endpoint")}

    def add_provider(self, account: Account, name: str, issuer: str, client_id: str, client_secret: str,
                     scopes: str = DEFAULT_SCOPES, id_claim: str = "sub", use_pkce: bool = True) -> dict[str, Any]:
        if not (name.strip() and client_id.strip() and client_secret.strip()):
            raise AccountError("Name, client ID and client secret are required.")
        endpoints = self.discover(issuer)
        fields = {"display_name": name.strip(), **endpoints, "id_claim": id_claim.strip() or "sub",
                  "client_id": client_id.strip(), "client_secret": client_secret.strip(),
                  "scopes": scopes.strip() or DEFAULT_SCOPES, "use_pkce": use_pkce, "enabled": True,
                  "auto_verify_email": True, "account_linking_enabled": False}
        return self.poster._run(account, lambda adapter, token: self._adapter(account.domain)
                                .create_oauth_provider(token, **fields))

    def set_provider(self, account: Account, provider_id: int, action: str) -> None:
        def act(adapter: Any, token: str) -> None:
            a = self._adapter(account.domain)
            if action == "delete":
                a.delete_oauth_provider(token, provider_id)
            else:
                a.edit_oauth_provider(token, provider_id, enabled=action == "enable")
        if action not in ("enable", "disable", "delete"):
            raise AccountError("Unknown action.")
        self.poster._run(account, act)

    def edit_provider(self, account: Account, provider_id: int, name: str, scopes: str, id_claim: str) -> None:
        """Change how a provider is shown and how people are identified. Lemmy
        reads `id_claim` from the provider's userinfo response, so it can be a
        custom claim (e.g. one an Authentik scope mapping adds)."""
        if not (name.strip() and id_claim.strip()):
            raise AccountError("Name and identity claim are required.")
        scopes = " ".join(scopes.split()) or DEFAULT_SCOPES
        if "openid" not in scopes.split():
            raise AccountError("Scopes must include openid.")
        self.poster._run(account, lambda adapter, token: self._adapter(account.domain).edit_oauth_provider(
            token, provider_id, display_name=name.strip(), scopes=scopes, id_claim=id_claim.strip()))

    def set_signups(self, account: Account, allowed: bool) -> None:
        self.poster._run(account, lambda adapter, token: adapter.update_site(token, oauth_registration=allowed))
