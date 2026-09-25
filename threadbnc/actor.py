"""ThreadBNC's own ActivityPub identity, for following what no community
carries: hashtags, through relays (tags.py).

It's one actor, https://{THREADBNC_ACTOR_DOMAIN}/threadbnc/actor, known as
threadbnc@{that domain}. It never posts. It follows, it's sent what it
follows, and it reads posts from their own servers the way any server would:
every request it makes is signed with its key (HTTP Signatures, as Mastodon
does them), so servers that only answer signed requests answer it too, and
every delivery to its inbox has to be signed by whoever sent it.

The domain can be one a Lemmy server already runs on (dyslectric.dev): the
actor lives under /threadbnc/, which Lemmy doesn't use, and the reverse proxy
sends just those paths, and WebFinger lookups for threadbnc@, here. See the
README, "Hashtags"."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from email.utils import formatdate, parsedate_to_datetime
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa
from starlette.concurrency import run_in_threadpool

from .adapters import RemoteAuthError, RemoteError, RemoteNotFound, RemotePaused, RemoteRejected, RemoteUnavailable
from .adapters.http import HttpClient
from .db import Database
from .traffic import delivered
from .vault import TokenVault

log = logging.getLogger(__name__)

ACTOR_PATH = "/threadbnc/actor"
INBOX_PATH = "/threadbnc/inbox"
OUTBOX_PATH = "/threadbnc/outbox"
WEBFINGER_PATH = "/.well-known/webfinger"
USERNAME = "threadbnc"
KEY_SETTING = "actor_private_key"
AP_JSON = "application/activity+json"
AP_ACCEPT = 'application/activity+json, application/ld+json; profile="https://www.w3.org/ns/activitystreams"'
CONTEXT = ["https://www.w3.org/ns/activitystreams", "https://w3id.org/security/v1"]
PUBLIC = {"https://www.w3.org/ns/activitystreams#Public", "as:Public", "Public"}
MAX_ACTIVITY_BYTES = 2_000_000
MAX_CLOCK_SKEW = timedelta(hours=12)  # as Mastodon allows
KEY_CACHE_SECONDS = 86400.0  # a sender's key is fetched again after this, or when it stops verifying


class SignatureError(Exception):
    """A delivery whose signature is missing or doesn't check out."""


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def is_public(obj: dict[str, Any]) -> bool:
    """Addressed to everyone (public or unlisted), not just followers or mentions."""
    to: list[Any] = []
    for field in ("to", "cc"):
        value = obj.get(field)
        to += value if isinstance(value, list) else [value]
    return any(isinstance(v, str) and v in PUBLIC for v in to)


def parse_signature(value: str | None) -> dict[str, str]:
    """keyId="…",algorithm="…",headers="…",signature="…" -> its parameters."""
    if not value:
        raise SignatureError("unsigned")
    params = dict(re.findall(r'(\w+)="([^"]*)"', value))
    params.update((k, v) for k, v in re.findall(r'(\w+)=(\d+)(?:,|$)', value))  # (created) and (expires) are bare
    if not params.get("keyId") or not params.get("signature"):
        raise SignatureError("the Signature header has no keyId or signature")
    return params


class Actor:
    def __init__(self, domain: str, db: Database, vault: TokenVault, http: HttpClient):
        self.domain = domain
        self.db, self.vault, self.http = db, vault, http
        self.id = f"https://{domain}{ACTOR_PATH}"
        self.inbox = f"https://{domain}{INBOX_PATH}"
        self.outbox = f"https://{domain}{OUTBOX_PATH}"
        self.key_id = f"{self.id}#main-key"
        self.handle = f"{USERNAME}@{domain}"
        self._key: rsa.RSAPrivateKey | None = None
        self._lock = threading.Lock()
        self._keys: dict[str, tuple[Any, str, float]] = {}  # key id -> (public key, owner, fetched at)

    # -- the key -------------------------------------------------------------------
    def private_key(self) -> rsa.RSAPrivateKey:
        """Made on first use and kept in the database, encrypted like account
        tokens (vault.py)."""
        with self._lock:
            if self._key is None:
                stored = self.db.get_setting(KEY_SETTING)
                if stored is None:
                    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
                    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                            serialization.NoEncryption()).decode()
                    with self.db.transaction() as conn:  # a second process may have made one first: keep that
                        conn.execute("INSERT INTO app_settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO NOTHING",
                                     (KEY_SETTING, self.vault.encrypt(pem)))
                    stored = self.db.get_setting(KEY_SETTING)
                pem = self.vault.decrypt(stored)  # type: ignore[arg-type]
                self._key = serialization.load_pem_private_key(pem.encode(), password=None)  # type: ignore[assignment]
            return self._key  # type: ignore[return-value]

    def public_pem(self) -> str:
        return self.private_key().public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()

    # -- what it serves ------------------------------------------------------------
    def document(self) -> dict[str, Any]:
        return {
            "@context": CONTEXT,
            "id": self.id,
            "type": "Application",
            "preferredUsername": USERNAME,
            "name": "ThreadBNC",
            "summary": "<p>A private reader that follows hashtags. It doesn't post, and doesn't take followers.</p>",
            "url": self.id,
            "inbox": self.inbox,
            "outbox": self.outbox,
            "endpoints": {"sharedInbox": self.inbox},
            "manuallyApprovesFollowers": True,
            "discoverable": False,
            "indexable": False,
            "publicKey": {"id": self.key_id, "owner": self.id, "publicKeyPem": self.public_pem()},
        }

    def outbox_document(self) -> dict[str, Any]:
        return {"@context": CONTEXT[0], "id": self.outbox, "type": "OrderedCollection", "totalItems": 0,
                "orderedItems": []}

    def webfinger(self, resource: str) -> dict[str, Any] | None:
        """Servers look the actor up as threadbnc@domain before trusting its key."""
        if resource.lower() not in (f"acct:{self.handle}", self.handle, self.id):
            return None
        return {"subject": f"acct:{self.handle}", "aliases": [self.id],
                "links": [{"rel": "self", "type": AP_JSON, "href": self.id}]}

    # -- signed requests -----------------------------------------------------------
    def signed_headers(self, method: str, url: str, body: bytes | None = None) -> dict[str, str]:
        u = httpx.URL(url)
        headers = {"Host": u.netloc.decode(), "Date": formatdate(usegmt=True)}
        names = ["(request-target)", "host", "date"]
        if body is not None:
            headers["Digest"] = "SHA-256=" + _b64(hashlib.sha256(body).digest())
            names.append("digest")
        lines = [f"(request-target): {method.lower()} {u.raw_path.decode()}"]
        lines += [f"{n}: {headers[n.title()]}" for n in names[1:]]
        signature = self.private_key().sign("\n".join(lines).encode(), padding.PKCS1v15(), hashes.SHA256())
        headers["Signature"] = (f'keyId="{self.key_id}",algorithm="rsa-sha256",headers="{" ".join(names)}",'
                                f'signature="{_b64(signature)}"')
        return headers

    def fetch(self, url: str) -> dict[str, Any]:
        """An ActivityPub object from where it lives, asked for with a signature."""
        if urlparse(url).scheme != "https":
            raise RemoteNotFound(f"{url}: not an https address")
        headers = self.signed_headers("GET", url)
        headers["Accept"] = AP_ACCEPT
        resp = self.http.send("GET", url, headers=headers)
        host = urlparse(url).hostname
        if resp.status_code in (404, 410):
            raise RemoteNotFound(f"{url}: HTTP {resp.status_code}")
        if resp.status_code in (401, 403):
            raise RemoteAuthError(f"{host} refused to show it (HTTP {resp.status_code})")
        if resp.status_code >= 400:
            raise RemoteUnavailable(f"{url}: HTTP {resp.status_code}")
        try:
            data = resp.json()
        except ValueError as exc:
            raise RemoteUnavailable(f"{url}: not ActivityPub (HTTP {resp.status_code})") from exc
        if not isinstance(data, dict):
            raise RemoteUnavailable(f"{url}: not an ActivityPub object")
        return data

    def deliver(self, inbox: str, activity: dict[str, Any]) -> None:
        """Send an activity to an inbox, signed."""
        body = json.dumps(activity).encode()
        headers = self.signed_headers("POST", inbox, body)
        headers["Content-Type"] = AP_JSON
        resp = self.http.send("POST", inbox, headers=headers, content=body, throttle=False)
        if resp.status_code >= 300:
            answer = resp.text[:300].replace("\x00", "")
            raise RemoteRejected(f"{urlparse(inbox).hostname} answered HTTP {resp.status_code}: {answer}".strip())

    def inbox_of(self, actor_url: str) -> str:
        doc = self.fetch(actor_url)
        inbox = doc.get("inbox")
        if not isinstance(inbox, str) or not inbox.startswith("https://"):
            raise RemoteNotFound(f"{actor_url} has no inbox")
        return inbox

    # -- deliveries to it ----------------------------------------------------------
    def _public_key(self, key_id: str, fresh: bool = False) -> tuple[Any, str]:
        cached = self._keys.get(key_id)
        if cached and not fresh and time.monotonic() - cached[2] < KEY_CACHE_SECONDS:
            return cached[0], cached[1]
        doc = self.fetch(key_id.split("#")[0])
        if "publicKeyPem" in doc:  # the key on its own (GoToSocial serves it so)
            pem, owner = doc.get("publicKeyPem"), doc.get("owner")
        else:
            keys = doc.get("publicKey")
            keys = keys if isinstance(keys, list) else [keys]
            entry = next((k for k in keys if isinstance(k, dict) and k.get("id") == key_id),
                         next((k for k in keys if isinstance(k, dict)), {}))
            pem, owner = entry.get("publicKeyPem"), entry.get("owner") or doc.get("id")
        if not isinstance(pem, str) or not isinstance(owner, str):
            raise SignatureError(f"no public key at {key_id}")
        if urlparse(owner).hostname != urlparse(key_id).hostname:
            raise SignatureError(f"{key_id} says it belongs to {owner}, on another server")
        key = serialization.load_pem_public_key(pem.encode())
        self._keys[key_id] = (key, owner, time.monotonic())
        return key, owner

    def verify(self, method: str, target: str, headers: dict[str, str], body: bytes) -> str:
        """Check a delivery's signature (and that its body is what was signed).
        `target` is the path with its query, `headers` lowercased. Returns
        who signed it: the actor that owns the key."""
        params = parse_signature(headers.get("signature"))
        names = params.get("headers", "date").lower().split()
        if "(request-target)" not in names or "host" not in names:
            raise SignatureError("the signature doesn't cover the request target and host")
        if "digest" not in names:
            raise SignatureError("the signature doesn't cover the body")
        digests = dict(d.strip().split("=", 1) for d in headers.get("digest", "").split(",") if "=" in d)
        given = next((v for k, v in digests.items() if k.lower() == "sha-256"), None)
        if given != _b64(hashlib.sha256(body).digest()):
            raise SignatureError("the body isn't what was signed")
        when: datetime | None = None
        if "date" in names:
            try:
                when = parsedate_to_datetime(headers.get("date", ""))
            except (TypeError, ValueError, IndexError):
                raise SignatureError("unreadable Date") from None
        elif "(created)" in names and params.get("created", "").isdigit():
            when = datetime.fromtimestamp(int(params["created"]), timezone.utc)
        if when is None:
            raise SignatureError("the signature doesn't say when it was made")
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        if abs(datetime.now(timezone.utc) - when) > MAX_CLOCK_SKEW:
            raise SignatureError("the signature is too old, or from the future")
        lines = []
        for n in names:
            if n == "(request-target)":
                lines.append(f"(request-target): {method.lower()} {target}")
            elif n in ("(created)", "(expires)"):
                lines.append(f"{n}: {params.get(n.strip('()'), '')}")
            elif n in headers:
                lines.append(f"{n}: {headers[n]}")
            else:
                raise SignatureError(f"the signed header {n} is missing")
        signing = "\n".join(lines).encode()
        signature = base64.b64decode(params["signature"])
        for fresh in (False, True):  # a key that no longer verifies may have been replaced: fetch it again
            try:
                key, owner = self._public_key(params["keyId"], fresh=fresh)
            except RemoteError as exc:
                raise SignatureError(f"couldn't get the key {params['keyId']}: {exc}") from exc
            try:
                if isinstance(key, rsa.RSAPublicKey):
                    key.verify(signature, signing, padding.PKCS1v15(), hashes.SHA256())
                elif isinstance(key, ed25519.Ed25519PublicKey):
                    key.verify(signature, signing)
                else:
                    raise SignatureError("unsupported kind of key")
                return owner
            except InvalidSignature:
                if fresh:
                    break
        raise SignatureError("the signature doesn't match the sender's key")


# --- serving it -----------------------------------------------------------------

async def _respond(send: Any, status: int, body: bytes, content_type: str = "text/plain") -> None:
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-length", str(len(body)).encode()), (b"content-type", content_type.encode()),
                            (b"cache-control", b"no-store")]})
    await send({"type": "http.response.body", "body": body})


class ActorEndpoints:
    """ASGI middleware serving the actor, its (empty) outbox, its WebFinger
    and its inbox, ahead of sign-in: other servers have to reach these.
    `receive(activity, signer)` gets each delivery whose signature checks
    out, and answers what became of it. `expects(activity)` is asked first,
    so deliveries from anyone the actor doesn't follow are dropped before any
    key is fetched for them."""

    def __init__(self, app: Any, actor: Actor, receive: Callable[[dict[str, Any], str], str],
                 expects: Callable[[dict[str, Any]], bool]):
        self.app, self.actor, self.receive, self.expects = app, actor, receive, expects

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path, method = scope["path"], scope["method"]
        if method in ("GET", "HEAD") and path == ACTOR_PATH:
            doc = await run_in_threadpool(self.actor.document)
            return await _respond(send, 200, json.dumps(doc).encode(), AP_JSON)
        if method in ("GET", "HEAD") and path == OUTBOX_PATH:
            return await _respond(send, 200, json.dumps(self.actor.outbox_document()).encode(), AP_JSON)
        if method in ("GET", "HEAD") and path == WEBFINGER_PATH:
            query = parse_qs(scope.get("query_string", b"").decode("latin-1"))
            jrd = self.actor.webfinger((query.get("resource") or [""])[0])
            if jrd is not None:
                return await _respond(send, 200, json.dumps(jrd).encode(), "application/jrd+json")
        if path == INBOX_PATH:
            if method != "POST":
                return await _respond(send, 405, b"POST activities here")
            return await self._inbox(scope, receive, send)
        return await self.app(scope, receive, send)

    async def _inbox(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        body = bytearray()
        more = True
        while more:
            message = await receive()
            body += message.get("body", b"")
            more = message.get("more_body", False)
            if len(body) > MAX_ACTIVITY_BYTES:
                return await _respond(send, 413, b"Activity too large")
        try:
            activity = json.loads(bytes(body))
        except ValueError:
            return await _respond(send, 400, b"Not JSON")
        if not isinstance(activity, dict):
            return await _respond(send, 400, b"Not an activity")
        delivered(activity, len(body) + sum(len(k) + len(v) + 4 for k, v in scope["headers"]))
        if not self.expects(activity):
            return await _respond(send, 202, b"")  # not from anything followed: nothing to do, nothing fetched
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        raw = (scope.get("raw_path") or scope["path"].encode()).decode("latin-1")
        query = scope.get("query_string", b"").decode("latin-1")
        target = raw + (f"?{query}" if query else "")
        try:
            signer = await run_in_threadpool(self.actor.verify, "POST", target, headers, bytes(body))
        except RemotePaused as exc:
            return await _respond(send, 503, f"Busy, try again in {int(exc.seconds) + 1} s".encode())
        except SignatureError as exc:
            log.info("refused a delivery from %s: %s", activity.get("actor"), exc)
            return await _respond(send, 401, f"Signature: {exc}".encode())
        actor = activity.get("actor")
        actor = actor[0] if isinstance(actor, list) and len(actor) == 1 else actor
        actor = actor.get("id") if isinstance(actor, dict) else actor
        if signer != actor and urlparse(str(signer)).hostname != urlparse(str(actor)).hostname:
            return await _respond(send, 401, b"Signed by someone other than the activity's actor")
        try:
            await run_in_threadpool(self.receive, activity, signer)
        except Exception:
            log.exception("couldn't take a delivery from %s", actor)
            return await _respond(send, 500, b"Couldn't take it; try again later")
        await _respond(send, 202, b"")
