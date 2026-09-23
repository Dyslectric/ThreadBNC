"""Pushes: what your own Lemmy servers receive, as it arrives.

This is how Lemmy and PieFed communities you follow arrive: they aren't checked
on a schedule unless you ask (bouncer.py). For your own servers
(THREADBNC_RELAY_INBOXES, e.g. dyslectric.dev), their ActivityPub inbox is
routed through ThreadBNC:

1. InboxRelay passes every delivery straight on to Lemmy, unchanged, and gives
   the sender Lemmy's answer. Lemmy checks each activity's signature, so the
   ones it accepts are genuine, and only those are queued (ap_inbox). While
   ThreadBNC is down deliveries fail, and their senders try again later.
2. Your account on that server subscribes to the Lemmy and PieFed communities
   you follow, so their home servers deliver every post, comment, edit,
   deletion, removal, lock and pin to it the moment it happens. (Votes come
   too; they're ignored here, and read from your server's copy of the
   community on the vote schedule instead, see Bouncer.check_votes.)
3. The push worker takes each queued activity, re-reads the post or comment it
   is about from your own server, where it has just arrived, and records that
   like a polled observation. Created and edited text is taken from the
   activity itself, so a comment deleted seconds later, or an edit replaced by
   the next one, is still kept. A new post in a followed community is captured
   at once, and read from your server from then on.

Every PUSHED_POLL_MINUTES (bouncer.py) your server's copy of a pushed community
is looked over to catch anything a delivery missed; that asks nothing of the
community's home server.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import traceback
from dataclasses import dataclass, replace
from datetime import timedelta
from typing import Any, Callable

import httpx
from starlette.concurrency import run_in_threadpool

from . import store
from .accounts import Account, AccountError, Poster
from .adapters import NComment, NPost, RemoteError, RemoteNotFound, RemotePaused, ThreadiverseAdapter, host_of, is_reddit_host, is_rss
from .db import fmt_ts, parse_ts, utcnow

log = logging.getLogger(__name__)

INBOX_PATH = re.compile(r"^/(?:site_)?inbox$|^/(?:u|c)/[^/]+/inbox$")
MAX_ACTIVITY_BYTES = 2_000_000
# Headers about one connection rather than the request; the rest are passed on as they came.
# Accept-Encoding too: the relay reads Lemmy's answer, so it asks only for encodings
# it can decode (the sender's, e.g. Cloudflare's Brotli, may not be). Senders don't sign it.
HOP_BY_HOP = {"connection", "keep-alive", "transfer-encoding", "te", "trailer", "upgrade", "content-length",
              "proxy-authorization", "proxy-authenticate", "accept-encoding"}
MAX_ATTEMPTS = 5
RETRY_AFTER = timedelta(minutes=2)
KEEP_HANDLED = timedelta(days=3)
HOUSEKEEPING_EVERY = 1800  # seconds: tidy the queue, re-check subscriptions waiting to be accepted

VOTES = {"Like", "Dislike", "Vote"}
POSTS_AND_COMMENTS = {"Note", "Page", "Article", "Question", "Video", "Image", "Event"}


# --- the relay ------------------------------------------------------------------

class InboxRelay:
    """ASGI middleware: a POST to an inbox on one of your servers goes to that
    server's Lemmy, and what Lemmy accepts is queued. What it refuses is passed
    to `refuse` (with Lemmy's answer), for the pushes page. Everything else is the app's."""

    def __init__(self, app: Any, relays: dict[str, str], accept: Callable[[str, str, bytes], None],
                 client: httpx.AsyncClient | None = None,
                 refuse: Callable[[str, str, bytes, int, str], None] | None = None):
        self.app, self.relays, self.accept, self.refuse = app, relays, accept, refuse
        self.client = client or httpx.AsyncClient(timeout=60.0, follow_redirects=False)

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http" or scope["method"] != "POST" or not INBOX_PATH.match(scope["path"]):
            return await self.app(scope, receive, send)
        headers = [(k.decode("latin-1"), v.decode("latin-1")) for k, v in scope["headers"]]
        host = next((v for k, v in headers if k.lower() == "host"), "").split(":")[0].lower()
        upstream = self.relays.get(host)
        if upstream is None:
            return await self.app(scope, receive, send)
        body = bytearray()
        more = True
        while more:
            message = await receive()
            body += message.get("body", b"")
            more = message.get("more_body", False)
            if len(body) > MAX_ACTIVITY_BYTES:
                return await _respond(send, 413, b"Activity too large", "text/plain")
        query = scope.get("query_string", b"").decode("latin-1")
        path = (scope.get("raw_path") or scope["path"].encode()).decode("latin-1")  # exactly as it came
        url = upstream + path + (f"?{query}" if query else "")
        try:
            # The Host header goes along unchanged: Lemmy checks it as part of the signature.
            resp = await self.client.post(url, content=bytes(body),
                                          headers=[(k, v) for k, v in headers if k.lower() not in HOP_BY_HOP])
        except httpx.HTTPError as exc:
            log.warning("relaying to %s failed: %s", url, exc)
            return await _respond(send, 502, b"Server unavailable, try again later", "text/plain")
        if 200 <= resp.status_code < 300:
            try:
                await run_in_threadpool(self.accept, host, scope["path"], bytes(body))
            except Exception:  # Lemmy has it either way; polling reconciles
                log.error("couldn't queue an activity for %s: %s", host, traceback.format_exc())
        else:
            answer = readable(resp.content)
            log.warning("%s refused a delivery to %s (HTTP %d): %s", host, scope["path"], resp.status_code, answer)
            if self.refuse:
                try:
                    await run_in_threadpool(self.refuse, host, scope["path"], bytes(body), resp.status_code, answer)
                except Exception:
                    log.error("couldn't note a refused delivery: %s", traceback.format_exc())
        await _respond(send, resp.status_code, resp.content, resp.headers.get("content-type"))


def readable(content: bytes, limit: int = 500) -> str:
    """An answer as text fit to log and store (Postgres takes no NUL bytes)."""
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return f"({len(content)} bytes that aren't text)"
    return text.replace("\x00", "")[:limit]


def _text(body: bytes) -> str:
    return body.decode("utf-8", "replace").replace("\x00", "")


async def _respond(send: Any, status: int, body: bytes, content_type: str | None) -> None:
    headers = [(b"content-length", str(len(body)).encode())]
    if content_type:
        headers.append((b"content-type", content_type.encode("latin-1")))
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


# --- reading activities ---------------------------------------------------------

@dataclass
class Target:
    """The post or comment an activity is about."""

    ap_id: str
    payload: dict[str, Any] | None  # the object as sent, when the activity carries its content
    community: str | None  # who announced it


def _id(value: Any) -> str | None:
    if isinstance(value, dict):
        value = value.get("id")
    return value if isinstance(value, str) and value.startswith(("https://", "http://")) else None


def describe(activity: dict[str, Any]) -> Target | str:
    """What an activity is about, or why it's of no interest."""
    community = None
    if activity.get("type") == "Announce":  # a community passing on what happened in it
        community = _id(activity.get("actor"))
        activity = activity.get("object")  # type: ignore[assignment]
        if not isinstance(activity, dict):
            return "announced activity not included"
    kind, obj = activity.get("type"), activity.get("object")
    undo = kind == "Undo"
    if undo:  # a restore, unlock or unpin: re-read the object
        if not isinstance(obj, dict):
            return "undo of an activity not included"
        kind, obj = obj.get("type"), obj.get("object")
    if kind in VOTES:
        return "vote"
    if kind in ("Create", "Update"):
        if not (isinstance(obj, dict) and obj.get("type") in POSTS_AND_COMMENTS and _id(obj)):
            return f"{kind} of {obj.get('type') if isinstance(obj, dict) else 'a reference'}"
        # Content is only taken from its author's own server (Lemmy checks this too).
        own = host_of(_id(obj) or "") == host_of(_id(obj.get("attributedTo")) or "")
        return Target(_id(obj) or "", obj if own and not undo else None, community)
    if kind in ("Delete", "Remove", "Add", "Lock") and _id(obj):
        return Target(_id(obj) or "", None, community)
    return f"{kind or 'unknown'} activity"


# For the pushes page: (activity, object type) -> what happened, in words.
LABELS = {("Create", "Note"): "New comment", ("Create", "Page"): "New post", ("Update", "Note"): "Edited comment",
          ("Update", "Page"): "Edited post", ("Delete", None): "Deleted", ("Undo Delete", None): "Restored",
          ("Lock", None): "Locked", ("Undo Lock", None): "Unlocked", ("Add", None): "Pinned or added",
          ("Remove", None): "Unpinned or removed", ("Undo Remove", None): "Restored",
          ("Like", None): "Vote", ("Dislike", None): "Vote", ("Undo Like", None): "Vote taken back",
          ("Undo Dislike", None): "Vote taken back", ("Block", None): "Ban", ("Undo Block", None): "Unban",
          ("Update", "Group"): "Community settings changed"}


def summarize(body: str) -> dict[str, Any]:
    """What a delivered activity was, for the pushes page: a label, the raw
    types (for its tooltip), the post or comment it's about and who announced it."""
    try:
        activity = json.loads(body)
    except ValueError:
        return {"label": "Unreadable", "types": "", "object": None, "community": None}
    community = None
    if isinstance(activity, dict) and activity.get("type") == "Announce":
        community = _id(activity.get("actor"))
        activity = activity.get("object")
    if not isinstance(activity, dict):
        return {"label": "Announce", "types": "Announce", "object": None, "community": community}
    kind, obj = str(activity.get("type") or "?"), activity.get("object")
    if kind == "Undo" and isinstance(obj, dict):
        kind, obj = f"Undo {obj.get('type')}", obj.get("object")
    otype = obj.get("type") if isinstance(obj, dict) else None
    label = LABELS.get((kind, otype)) or LABELS.get((kind, None)) or f"{kind} {otype or ''}".strip()
    return {"label": label, "types": f"{kind} {otype or ''}".strip(), "object": _id(obj), "community": community}


def _markdown(obj: dict[str, Any]) -> str | None:
    source = obj.get("source")
    if isinstance(source, dict) and "markdown" in str(source.get("mediaType", "")) \
            and isinstance(source.get("content"), str):
        return source["content"]
    return None


def _ts(value: Any) -> str | None:
    dt = parse_ts(value) if isinstance(value, str) else None
    return fmt_ts(dt) if dt else None


def with_payload(item: NPost | NComment, payload: dict[str, Any]) -> NPost | NComment:
    """What the server shows now, with the text as it was when this activity
    was sent: the server may already show a later edit, or a deletion's blank."""
    body = _markdown(payload)
    if body is None and payload.get("content"):  # no Markdown source to compare: keep the server's
        return item
    changes: dict[str, Any] = {"body": body, "updated_at": _ts(payload.get("updated"))}
    if isinstance(item, NPost) and isinstance(payload.get("name"), str):
        changes["title"] = payload["name"]
    return replace(item, **changes)


def _federated(ap_id: str) -> bool:
    return not is_rss(ap_id) and not is_reddit_host(host_of(ap_id))


# --- the push worker ------------------------------------------------------------

class Federation:
    def __init__(self, poster: Poster, relays: dict[str, str]):
        self.poster, self.bouncer, self.db = poster, poster.bouncer, poster.bouncer.db
        self.relays = relays
        self.wake = threading.Event()
        self._stop = threading.Event()
        self._housekept = 0.0
        for domain in relays:  # your own servers: no need to space out requests
            self.bouncer.http.throttle.exempt.add(domain)
        self.bouncer.follow_hooks.append(self.subscribe)
        self.bouncer.unfollow_hooks.append(self.unsubscribe)

    def account(self, domain: str | None = None) -> Account | None:
        """Your account on one of your servers (on `domain`, if given)."""
        return next((a for a in self.poster.list() if a.domain in self.relays and not a.is_reddit
                     and a.status == "ok" and domain in (None, a.domain)), None)

    # -- the queue -------------------------------------------------------------------
    def queue(self, domain: str, path: str, body: bytes) -> None:
        """An activity your server accepted (InboxRelay's `accept`)."""
        try:
            activity = json.loads(body)
        except ValueError:
            return
        if not isinstance(activity, dict):
            return
        with self.db.transaction() as conn:
            conn.execute("INSERT INTO ap_inbox(domain, path, activity_id, activity_type, body, received_at) "
                         "VALUES (?,?,?,?,?,?) ON CONFLICT(activity_id) DO NOTHING",
                         (domain, path, _id(activity.get("id")), str(activity.get("type") or "")[:40],
                          _text(body), utcnow()))
        self.wake.set()

    def refused(self, domain: str, path: str, body: bytes, status: int, answer: str) -> None:
        """A delivery your server refused (InboxRelay's `refuse`): kept only to be
        shown on the pushes page, never processed. Without its activity id, so a
        later delivery of the same activity that is accepted still gets queued."""
        try:
            kind = str((json.loads(body) or {}).get("type") or "")[:40]
        except (ValueError, AttributeError):
            kind = ""
        now = utcnow()
        with self.db.transaction() as conn:
            conn.execute("INSERT INTO ap_inbox(domain, path, activity_type, body, received_at, status, outcome, "
                         "processed_at) VALUES (?,?,?,?,?,'refused',?,?)",
                         (domain, path, kind, _text(body), now,
                          f"{domain} answered HTTP {status}: {answer}"[:500].replace("\x00", ""), now))

    def process_pending(self, limit: int = 50) -> int:
        retry = fmt_ts(parse_ts(utcnow()) - RETRY_AFTER)  # type: ignore[operator]
        with self.db.connect() as conn:
            rows = conn.execute("SELECT * FROM ap_inbox WHERE status='pending' AND (attempts=0 OR processed_at<=?) "
                                "ORDER BY id LIMIT ?", (retry, limit)).fetchall()
        for row in rows:
            self.process(row)
        return len(rows)

    def process(self, row: Any) -> None:
        attempts = row["attempts"] + 1
        try:
            status, outcome = self.handle(row)
        except RemotePaused as exc:  # your server asked us to wait: this try doesn't count
            status, outcome, attempts = "pending", str(exc), row["attempts"]
        except (AccountError, RemoteError) as exc:  # the server may be busy: try again shortly
            status, outcome = ("failed" if attempts >= MAX_ATTEMPTS else "pending"), str(exc)
        except Exception as exc:
            log.error("push %s crashed: %s", row["id"], traceback.format_exc())
            status, outcome = "failed", f"{type(exc).__name__}: {exc}"
        with self.db.transaction() as conn:
            conn.execute("UPDATE ap_inbox SET status=?, outcome=?, attempts=?, processed_at=? WHERE id=?",
                         (status, outcome[:500], attempts, utcnow(), row["id"]))

    def handle(self, row: Any) -> tuple[str, str]:
        target = describe(json.loads(row["body"]))
        if isinstance(target, str):
            return "skipped", target
        account = self.account(row["domain"])
        if account is None:
            return "skipped", f"no account on {row['domain']}"
        got = self.poster._run(account, lambda adapter, token: self._fetch(account.domain, adapter, token,
                                                                           target.ap_id))
        if got is None:
            return "skipped", "not a post or comment"
        post, comment, parent_ap = got
        if target.payload:
            if comment is not None:
                comment = with_payload(comment, target.payload)  # type: ignore[assignment]
            else:
                post = with_payload(post, target.payload)  # type: ignore[assignment]
        with self.db.connect() as conn:
            thread = conn.execute("SELECT t.* FROM archived_threads t JOIN objects o ON o.id=t.root_object_id "
                                  "WHERE o.canonical_ap_id=?", (post.ap_id,)).fetchone()
        if thread is None:
            return self._capture(account, post)
        if thread["trashed_at"]:
            return "skipped", "thread in the trash"
        now = utcnow()
        result = store.ApplyResult()
        # Counts from your server are its own partial view, unless the thread is
        # read from your server anyway (captured from a push).
        partial = thread["source_domain"] != account.domain
        with self.db.transaction() as conn:
            root_id = store.apply_post(conn, thread["id"], post, account.domain, now, False, result,
                                       keep_counts=partial)
            if comment is not None:
                parent: int | None = root_id
                if comment.parent_local_id:
                    r = conn.execute("SELECT id FROM objects WHERE canonical_ap_id=?", (parent_ap,)).fetchone()
                    parent = r["id"] if r else None  # not archived (yet): the next full check places it
                store.apply_comment(conn, thread["id"], root_id, thread["community_id"], comment, parent,
                                    account.domain, now, False, result, keep_counts=partial)
            conn.execute("UPDATE community_follows SET last_push_at=? WHERE community_id=?",
                         (now, thread["community_id"]))
        self._mark_subscribed(thread["community_id"])  # its changes are arriving, whatever your server says
        self.bouncer._enrich_moderation(self.bouncer.adapter_for(account.domain), result, post.community)
        what = "comment" if comment is not None else "post"
        return "done", f"{what}: {result.new_revisions} new version(s), {result.new_events} event(s)"

    def _fetch(self, domain: str, adapter: ThreadiverseAdapter, token: str,
               ap_id: str) -> tuple[NPost, NComment | None, str | None] | None:
        """The post (and comment) as your server has it now, and the comment's
        parent's ActivityPub id."""
        reader = adapter.reading_as(token)  # type: ignore[attr-defined]
        with self.db.connect() as conn:
            known = conn.execute("SELECT o.object_type, l.local_id FROM objects o JOIN object_local_ids l "
                                 "ON l.object_id=o.id AND l.domain=? WHERE o.canonical_ap_id=?",
                                 (domain, ap_id)).fetchone()
        if known:
            found = {known["object_type"]: known["local_id"]}
        else:
            try:
                found = adapter.resolve_as(token, ap_id)
            except RemoteNotFound:
                return None
        if "comment" in found:
            comment, post_local = reader.fetch_comment(found["comment"])
            return reader.fetch_post(post_local), comment, self._parent_ap_id(domain, reader, comment)
        if "post" in found:
            return reader.fetch_post(found["post"]), None, None
        return None

    def _parent_ap_id(self, domain: str, reader: ThreadiverseAdapter, comment: NComment) -> str | None:
        if not comment.parent_local_id:
            return None
        with self.db.connect() as conn:
            row = conn.execute("SELECT o.canonical_ap_id FROM object_local_ids l JOIN objects o ON o.id=l.object_id "
                               "WHERE l.domain=? AND l.local_id=? AND o.object_type='comment'",
                               (domain, comment.parent_local_id)).fetchone()
        if row:
            return row["canonical_ap_id"]
        try:
            return reader.fetch_comment(comment.parent_local_id)[0].ap_id
        except RemoteError:
            return None

    def _capture(self, account: Account, post: NPost) -> tuple[str, str]:
        """A post not archived yet: captured when it's new in a followed community."""
        with self.db.connect() as conn:
            f = conn.execute("SELECT f.* FROM community_follows f JOIN communities c ON c.id=f.community_id "
                             "WHERE c.canonical_ap_id=? AND f.active=1", (post.community.ap_id,)).fetchone()
        if f is None:
            return "skipped", "not in a followed community"
        created, since = parse_ts(post.created_at), parse_ts(f["capture_since"])
        if created and since and created < since:
            return "skipped", "older than the follow"
        tid = self.bouncer._ingest_post(post, account.domain, post.local_id, self.bouncer.adapter_for(account.domain),
                                        capture=True,
                                        source_url=post.ap_id, retention="auto")
        with self.db.transaction() as conn:
            conn.execute("UPDATE community_follows SET last_push_at=? WHERE community_id=?",
                         (utcnow(), f["community_id"]))
        self._mark_subscribed(f["community_id"])
        return "done", f"captured as thread {tid}"

    # -- subscriptions ------------------------------------------------------------
    def subscribe(self, community_id: int) -> str | None:
        """Subscribe your account on your own server to a followed Lemmy or
        PieFed community, so its changes are pushed. Returns subscribed or
        pending, or None when it can't be (the community stays polled)."""
        account = self.account()
        with self.db.connect() as conn:
            row = conn.execute("SELECT c.canonical_ap_id FROM communities c JOIN community_follows f "
                               "ON f.community_id=c.id AND f.active=1 WHERE c.id=?", (community_id,)).fetchone()
        if account is None or row is None or not _federated(row["canonical_ap_id"]):
            return None

        def act(adapter: ThreadiverseAdapter, token: str) -> str:
            local = adapter.resolve_as(token, row["canonical_ap_id"]).get("community")
            if not local:
                raise AccountError(f"{account.domain} couldn't find this community.")
            return adapter.follow_community(token, local, True)

        try:
            state: str | None = self.poster._run(account, act)
            error = None if state != "not_subscribed" else f"{account.domain} didn't subscribe."
        except AccountError as exc:
            state, error = None, str(exc)
        if state == "not_subscribed":
            state = None
        now = utcnow()
        with self.db.transaction() as conn:
            conn.execute("UPDATE community_follows SET push_domain=?, push_state=?, push_error=?, push_changed_at=? "
                         "WHERE community_id=?", (account.domain, state, error, now, community_id))
            if state is None:  # not pushed after all: back to the usual checks now
                conn.execute("UPDATE community_follows SET next_poll_at=? WHERE community_id=?", (now, community_id))
        return state

    def unsubscribe(self, community_id: int) -> None:
        """Stop pushes for a community (after unfollowing it, or when asked):
        your account there unsubscribes, and the community is polled as usual."""
        with self.db.connect() as conn:
            row = conn.execute("SELECT c.canonical_ap_id, f.push_domain FROM communities c JOIN community_follows f "
                               "ON f.community_id=c.id WHERE c.id=?", (community_id,)).fetchone()
        if row is None or not row["push_domain"]:
            return
        account = self.account(row["push_domain"])
        if account is not None:
            try:
                self.poster._run(account, lambda adapter, token: adapter.follow_community(
                    token, adapter.resolve_as(token, row["canonical_ap_id"])["community"], False))
            except (AccountError, KeyError) as exc:
                log.warning("unsubscribing %s from %s failed: %s", account.handle, row["canonical_ap_id"], exc)
        with self.db.transaction() as conn:
            conn.execute("UPDATE community_follows SET push_domain=NULL, push_state=NULL, push_error=NULL, "
                         "push_changed_at=?, next_poll_at=? WHERE community_id=?", (utcnow(), utcnow(), community_id))
        self.bouncer.wake.set()

    def subscribe_all(self) -> tuple[int, int]:
        """Subscribe to every followed Lemmy and PieFed community that isn't yet.
        Returns (subscribed or pending, failed)."""
        with self.db.connect() as conn:
            rows = conn.execute("SELECT f.community_id, c.canonical_ap_id FROM community_follows f "
                                "JOIN communities c ON c.id=f.community_id "
                                "WHERE f.active=1 AND f.push_state IS NULL").fetchall()
        ok = failed = 0
        for r in rows:
            if _federated(r["canonical_ap_id"]):
                if self.subscribe(r["community_id"]):
                    ok += 1
                else:
                    failed += 1
        return ok, failed

    # -- running ----------------------------------------------------------------
    def housekeeping(self) -> None:
        if time.monotonic() - self._housekept < HOUSEKEEPING_EVERY:
            return
        self._housekept = time.monotonic()
        old = fmt_ts(parse_ts(utcnow()) - KEEP_HANDLED)  # type: ignore[operator]
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM ap_inbox WHERE status!='pending' AND received_at<?", (old,))
            waiting = [r[0] for r in conn.execute("SELECT community_id FROM community_follows "
                                                  "WHERE active=1 AND push_state='pending'")]
        for cid in waiting:
            self.check_subscription(cid)

    def check_subscription(self, community_id: int) -> str | None:
        """Whether a pending subscription has been accepted yet, read from your
        server. Only subscribes again when your server has no subscription."""
        with self.db.connect() as conn:
            row = conn.execute("SELECT c.canonical_ap_id, f.push_domain FROM communities c JOIN community_follows f "
                               "ON f.community_id=c.id AND f.active=1 WHERE c.id=?", (community_id,)).fetchone()
        account = self.account(row["push_domain"]) if row and row["push_domain"] else None
        if account is None:
            return None

        def read(adapter: ThreadiverseAdapter, token: str) -> str:
            local = adapter.resolve_as(token, row["canonical_ap_id"]).get("community")
            return adapter.community_follow_state(token, local) if local else "not_subscribed"

        try:
            state = self.poster._run(account, read)
        except AccountError as exc:
            log.warning("checking %s's subscription to %s failed: %s", account.handle, row["canonical_ap_id"], exc)
            return "pending"
        if state == "not_subscribed":
            return self.subscribe(community_id)
        if state == "subscribed":
            self._mark_subscribed(community_id)
        return state

    def _mark_subscribed(self, community_id: int) -> None:
        with self.db.transaction() as conn:
            conn.execute("UPDATE community_follows SET push_state='subscribed', push_error=NULL, push_changed_at=? "
                         "WHERE community_id=? AND push_state='pending'", (utcnow(), community_id))

    def run_forever(self, idle_seconds: float = 60.0) -> None:
        log.info("push worker started for %s", ", ".join(self.relays))
        while not self._stop.is_set():
            try:
                while not self._stop.is_set() and self.process_pending():
                    pass
                self.housekeeping()
            except Exception:
                log.error("push worker crashed: %s", traceback.format_exc())
            self.wake.wait(idle_seconds)
            self.wake.clear()

    def start_thread(self) -> threading.Thread:
        t = threading.Thread(target=self.run_forever, name="pushes", daemon=True)
        t.start()
        return t

    def stop(self) -> None:
        self._stop.set()
        self.wake.set()
