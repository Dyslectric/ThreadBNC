"""Your Mastodon server's public timeline, subscribed to.

Signed in to a Mastodon account (accounts.py), ThreadBNC can listen to your
server's public timeline as it's made: either everything public it hears of
from across the fediverse (its federated timeline, "public") or only what's
posted on it ("public:local"). You choose on the Trending page. Mastodon only
streams to someone signed in, so it's asked as your account, over the
WebSocket its API offers (/api/v1/streaming); nothing else is sent.

What arrives is used for:

1. Hashtags. Public posts (not replies) with a followed hashtag are captured
   into its feed, as they arrive, and expire like any other auto-captured
   post unless you keep them. While subscribed, this takes the place of the
   tag relays (tags.py): ThreadBNC stops following them, and follows them
   again when you unsubscribe. A post is filed under the first followed
   hashtag it lists.
2. Trending (trends.py): each post's links are counted, and so are the
   replies each post gets (for the post it answers). Mastodon streams no
   likes, so the totals (likes, replies, boosts) of the posts most replied to,
   and of your server's own trending posts (/api/v1/trends/statuses, every
   TRENDING_EVERY), are read from your server now and then, CHECK_POSTS at a
   time in one request, CHECK_EVERY apart.

A dropped connection is made again, waiting longer each time it fails. Unlike
Jetstream, Mastodon's stream can't carry on from where it left off, so posts
made while it's down are missed (as for the relays)."""

from __future__ import annotations

import json
import logging
import threading
import time
import traceback
from typing import Any
from urllib.parse import urlencode, urlparse

from websockets.exceptions import WebSocketException
from websockets.sync.client import connect

from . import trends
from .adapters import TAG_DOMAIN, RemoteError, RemoteNotFound, normalize_tag
from .adapters.activitypub import context_comments, plain_text, status_post
from .bouncer import Bouncer
from .db import fmt_ts, parse_ts, utcnow
from .traffic import record, tagged
from .vault import TokenVault, VaultError

log = logging.getLogger(__name__)

SETTING = "mastodon_public"  # app setting (JSON): {"scope": one of SCOPES}; missing: not subscribed
SCOPES = {"public": "Everything public your server hears of (its federated timeline)",
          "public:local": "Only what's posted on your server (its local timeline)"}
JOB = "mastodon_tagged"
FLUSH_EVERY = 5.0
REFRESH_EVERY = 60.0  # seconds: the hashtags followed, and the subscription, are read again this often
MAX_BACKOFF = 300.0
CHECK_EVERY = 60.0  # seconds between reads of posts' totals
CHECK_POSTS = 20  # posts read at once (the most Mastodon's /api/v1/statuses takes)
ONE_BY_ONE = 5  # a server too old to read several at once: this many, one request each
TRENDING_EVERY = 900.0
FOLLOWED_SQL = ("SELECT c.name, f.capture_since FROM community_follows f JOIN communities c ON c.id=f.community_id "
                "WHERE f.active=1 AND c.canonical_ap_id LIKE 'tag:%'")


def subscription(db: Any) -> dict[str, Any] | None:
    """What's subscribed to ({"scope": ...}), or None."""
    raw = db.get_setting(SETTING)
    try:
        got = json.loads(raw) if raw else None
    except ValueError:
        return None
    return got if isinstance(got, dict) and got.get("scope") in SCOPES else None


def subscribe(db: Any, scope: str | None) -> None:
    """Subscribe to `scope` of your server's public timeline, or (None) stop."""
    with db.transaction() as conn:
        if scope in SCOPES:
            conn.execute("INSERT INTO app_settings(key, value) VALUES (?, ?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (SETTING, json.dumps({"scope": scope})))
        else:
            conn.execute("DELETE FROM app_settings WHERE key=?", (SETTING,))


def status_tags(status: dict[str, Any]) -> list[str]:
    """A status's hashtags, named as followed ones are, in order."""
    out: list[str] = []
    for t in status.get("tags") or []:
        try:
            tag = normalize_tag(str(t.get("name") or "")) if isinstance(t, dict) else None
        except ValueError:
            continue
        if tag and tag not in out:
            out.append(tag)
    return out


def status_links(status: dict[str, Any]) -> list[tuple[str, str | None, str | None]]:
    """(link, title, description) for each page a status links to: its card
    first, then the links in its text that aren't mentions or hashtags."""
    from .adapters.activitypub import _CLASS, _HREF, _LINK

    out = []
    card = status.get("card") if isinstance(status.get("card"), dict) else None
    if card and isinstance(card.get("url"), str):
        out.append((card["url"], card.get("title") or None, card.get("description") or None))
    for tag in _LINK.findall(status.get("content") or ""):
        href, cls = _HREF.search(tag), _CLASS.search(tag)
        classes = cls.group(1).split() if cls else []
        if href and "mention" not in classes and "hashtag" not in classes:
            out.append((href.group(1).replace("&amp;", "&"), None, None))
    return out


def status_view(status: dict[str, Any], domain: str) -> dict[str, Any]:
    """What the Trending page shows of a status."""
    account = status.get("account") or {}
    acct = str(account.get("acct") or account.get("username") or "?")
    card = status.get("card") if isinstance(status.get("card"), dict) else {}
    media = [m for m in status.get("media_attachments") or [] if isinstance(m, dict)]
    warning = (status.get("spoiler_text") or "").strip()
    return {"url": status.get("url") or status.get("uri"), "uri": status.get("uri"),
            "text": plain_text(status.get("content")).strip()[:3000], "warning": warning or None,
            "handle": acct if "@" in acct else f"{acct}@{domain}", "name": account.get("display_name") or None,
            "author_url": account.get("url"), "author_uri": account.get("uri"),
            "link": card.get("url"), "link_title": card.get("title") or None,
            "pictures": sum(1 for m in media if m.get("type") == "image"),
            "video": any(m.get("type") in ("video", "gifv") for m in media), "quote": False,
            "images": [u for u in ([m.get("url") if m.get("type") == "image" else m.get("preview_url") for m in media
                                    if m.get("type") in ("image", "video", "gifv")] or [card.get("image")])
                       if isinstance(u, str) and u.startswith("https://")][:trends.PICTURES]}


def totals(status: dict[str, Any], domain: str) -> dict[str, Any]:
    created = parse_ts(status.get("created_at"))
    return {"likes": status.get("favourites_count"), "replies": status.get("replies_count"),
            "reposts": status.get("reblogs_count"), "created_at": fmt_ts(created) if created else None,
            "view": status_view(status, domain)}


class MastodonStream:
    def __init__(self, bouncer: Bouncer, vault: TokenVault, user_agent: str):
        self.bouncer, self.db, self.vault, self.user_agent = bouncer, bouncer.db, vault, user_agent
        self.tally = trends.Tally("mastodon")
        self.wake = threading.Event()  # the subscription, or the hashtags followed, changed
        self._stop = threading.Event()
        self._streaming: dict[str, str] = {}  # your server -> where its stream is
        self._checked = self._trending = 0.0
        self._peek_lock = threading.Lock()
        self._peeked: dict[str, tuple[float, dict[str, Any]]] = {}  # ref -> (when, what peek() read)
        # What the Trending and hashtag pages say of it.
        self.connected_since: str | None = None
        self.connected_to: str | None = None
        self.last_error: str | None = None
        self.last_error_at: str | None = None
        bouncer.follow_hooks.append(self._changed)
        bouncer.unfollow_hooks.append(self._changed)
        bouncer.job_handlers[JOB] = self.capture
        bouncer.hooks.append(self.upkeep)
        bouncer.tag_adapter.mastodon = self.active  # hashtags can be followed without an actor of ThreadBNC's own

    def _changed(self, community_id: int | None = None) -> None:
        self.wake.set()

    # -- what's wanted ---------------------------------------------------------------
    def account(self) -> tuple[str, str, str] | None:
        """(server, access token, handle) of your Mastodon account, when you're
        signed in to one and its session can be read."""
        with self.db.connect() as conn:
            row = conn.execute("SELECT domain, username, token_enc FROM accounts WHERE software='mastodon' "
                               "AND status='ok' AND token_enc IS NOT NULL ORDER BY id LIMIT 1").fetchone()
        if row is None:
            return None
        try:
            access = json.loads(self.vault.decrypt(row["token_enc"]))["access"]
        except (VaultError, ValueError, KeyError, TypeError):
            return None
        return row["domain"], access, f"@{row['username']}@{row['domain']}"

    def wanted(self) -> tuple[str, str, str, str] | None:
        """(server, token, handle, scope) while subscribed and signed in."""
        chosen = subscription(self.db)
        account = self.account() if chosen else None
        return (*account, chosen["scope"]) if chosen and account else None

    def active(self) -> bool:
        """Whether hashtags come from your server's public timeline (not the relays)."""
        return subscription(self.db) is not None and self.account() is not None

    def followed(self) -> set[str]:
        with self.db.connect() as conn:
            return {r["name"] for r in conn.execute(FOLLOWED_SQL)}

    # -- listening -------------------------------------------------------------------
    def run_forever(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            wanted = self.wanted()
            if wanted is None:
                self.wake.wait(REFRESH_EVERY)
                self.wake.clear()
                continue
            try:
                self._listen(wanted)
                backoff = 1.0
                continue
            except (OSError, WebSocketException, RemoteError) as exc:
                log.warning("Mastodon stream: %s; trying again in %.0f s", exc, backoff)
                self._trouble(str(exc) or type(exc).__name__)
            except Exception as exc:  # keep listening
                log.error("Mastodon stream crashed: %s", traceback.format_exc())
                self._trouble(f"{type(exc).__name__}: {exc}")
            self._stop.wait(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF)

    def _trouble(self, error: str) -> None:
        self.last_error, self.last_error_at = error[:300], utcnow()

    def streaming_url(self, domain: str) -> str:
        """Where the server's stream is: it says (/api/v2/instance, or v1's), else on the server itself."""
        if domain not in self._streaming:
            found = None
            for path in ("/api/v2/instance", "/api/v1/instance"):
                try:
                    info = self.bouncer.http.get_json(domain, path)
                except RemoteNotFound:
                    continue
                urls = ((info.get("configuration") or {}).get("urls") or {}) if path.endswith("v2/instance") \
                    else (info.get("urls") or {})
                found = urls.get("streaming") or urls.get("streaming_api")
                break
            self._streaming[domain] = (found if isinstance(found, str) and found.startswith(("wss://", "ws://"))
                                       else f"wss://{domain}").rstrip("/")
        return self._streaming[domain]

    def _listen(self, wanted: tuple[str, str, str, str]) -> None:
        domain, token, handle, scope = wanted
        url = f"{self.streaming_url(domain)}/api/v1/streaming?{urlencode({'stream': scope})}"
        host = urlparse(url).hostname
        with connect(url, additional_headers={"Authorization": f"Bearer {token}"}, user_agent_header=self.user_agent,
                     open_timeout=20, max_size=2 ** 22) as ws:
            self.connected_since, self.connected_to, self.last_error = utcnow(), f"{handle} ({scope})", None
            log.info("Mastodon stream: listening to %s's %s timeline", domain, scope)
            tags = self.followed()
            flushed = refreshed = time.monotonic()
            messages = received = 0
            try:
                while not self._stop.is_set():
                    try:
                        raw = ws.recv(timeout=1.0)
                    except TimeoutError:
                        raw = None
                    if raw is not None:
                        messages, received = messages + 1, received + len(raw)
                        self.take(raw, tags, domain)
                    now = time.monotonic()
                    if now - flushed >= FLUSH_EVERY:
                        self.tally.flush(self.db)
                        record("in", host, requests=messages, bytes_in=received)
                        flushed, messages, received = now, 0, 0
                    if self.wake.is_set() or now - refreshed >= REFRESH_EVERY:
                        self.wake.clear()
                        refreshed = now
                        if self.wanted() != wanted:
                            return
                        tags = self.followed()
            finally:
                self.connected_since = None
                self.tally.flush(self.db)
                record("in", host, requests=messages, bytes_in=received)

    def take(self, raw: str | bytes, tags: set[str], domain: str) -> None:
        """One message from the stream: a new status is counted, and captured
        into a followed hashtag's feed if it has one."""
        try:
            message = json.loads(raw)
            status = json.loads(message["payload"]) if message.get("event") == "update" else None
        except (ValueError, KeyError, TypeError, AttributeError):
            return
        if not isinstance(status, dict) or status.get("reblog") or not status.get("uri"):
            return
        reply_to = status.get("in_reply_to_id")
        if reply_to:
            self.tally.reply(f"{domain}/{reply_to}")
        account = status.get("account") if isinstance(status.get("account"), dict) else {}
        author = account.get("url") or account.get("acct")
        self.tally.post_links(status_links(status), author)
        self.tally.post_tags(status_tags(status), author)
        if not reply_to and status.get("visibility", "public") == "public":
            tag = next((t for t in status_tags(status) if t in tags), None)
            if tag and not self.bouncer._existing_thread(status["uri"]):
                self.bouncer.enqueue(JOB, {"status": status, "tag": tag})

    def start_thread(self) -> threading.Thread:
        t = threading.Thread(target=self.run_forever, name="mastodon-stream", daemon=True)
        t.start()
        return t

    def stop(self) -> None:
        self._stop.set()
        self.wake.set()

    # -- capturing -------------------------------------------------------------------
    def capture(self, payload: dict[str, Any]) -> dict[str, Any]:
        """A job: capture a status from the stream into its hashtag's feed,
        unless it's older than the follow, or no longer followed."""
        status, tag = payload["status"], payload["tag"]
        existing = self.bouncer._existing_thread(status["uri"])
        if existing:
            return {"thread_id": existing["id"]}
        with self.db.connect() as conn:
            followed = {r["name"]: r["capture_since"] for r in conn.execute(FOLLOWED_SQL)}
        if tag not in followed:
            tag = next((t for t in status_tags(status) if t in followed), None)
            if tag is None:
                return {"skipped": "no longer following its hashtag"}
        post = status_post(status, tag)
        created, since = parse_ts(post.created_at), parse_ts(followed[tag])
        if created and since and created < since:
            return {"skipped": "older than the follow"}
        if self.bouncer.hidden(post):
            return {"skipped": "by someone you've hidden"}
        adapter = self.bouncer.tag_adapter
        tid = self.bouncer._ingest_post(post, TAG_DOMAIN, post.local_id, adapter, capture=True,
                                        source_url=post.ap_id, retention="auto")
        return {"thread_id": tid}

    # -- a post's replies, read when it's expanded on Trending ------------------------
    def peek(self, ref: str, uri: str | None, text: str | None) -> dict[str, Any]:
        """A Mastodon post from Trending, expanded: its text, then its replies,
        read from your server (it's `ref` there) while you're subscribed to
        it, else from the post's own server as it shows anyone. Nothing is
        saved; what's read is shown again for PEEK_FRESH seconds without asking."""
        from .discussions import PEEK_FRESH, reply_tree

        with self._peek_lock:
            hit = self._peeked.get(ref)
        if hit and time.monotonic() - hit[0] < PEEK_FRESH:
            return hit[1]
        wanted = self.wanted()
        domain, _, sid = ref.partition("/")
        try:
            if wanted and wanted[0] == domain:
                context = self._get(wanted, f"/api/v1/statuses/{sid}/context")
                comments = context_comments(context, sid)
            elif uri:
                comments = self.bouncer.tag_adapter.fetch_comments(f"peek {uri}")
            else:
                raise RemoteNotFound("It can't be read without your Mastodon server")
        except RemoteError as exc:
            return {"body": None, "text": text, "replies": None, "more": 0, "error": str(exc)}
        replies, more = reply_tree(comments)
        got = {"body": None, "text": text, "replies": replies, "count": len(comments), "more": more, "error": None}
        with self._peek_lock:
            now = time.monotonic()
            self._peeked = {k: v for k, v in self._peeked.items() if now - v[0] < PEEK_FRESH}
            self._peeked[ref] = (now, got)
        return got

    # -- totals, for Trending --------------------------------------------------------
    def upkeep(self) -> None:
        """A bouncer hook: read the totals of the posts most replied to, and
        your server's trending posts, while subscribed."""
        now = time.monotonic()
        if now - self._checked < CHECK_EVERY:
            return
        self._checked = now
        wanted = self.wanted()
        if wanted is None:
            return
        with tagged("trends"):
            try:
                self.check(wanted)
                if now - self._trending >= TRENDING_EVERY:
                    self._trending = now
                    self.check_trending(wanted)
            except RemoteError as exc:
                log.warning("reading Mastodon posts' totals failed: %s", exc)

    def _get(self, wanted: tuple[str, str, str, str], path: str, **params: Any) -> Any:
        return self.bouncer.http.request_json("GET", wanted[0], path, params=params or None, token=wanted[1])

    def check(self, wanted: tuple[str, str, str, str], now: str | None = None) -> int:
        """Read the totals of the posts most replied to lately that are due, from your server."""
        domain = wanted[0]
        with self.db.connect() as conn:
            due = [r["ref"] for r in trends.due_checks(conn, "mastodon", CHECK_POSTS * 3, now)
                   if r["ref"].startswith(f"{domain}/")][:CHECK_POSTS]
        if not due:
            return 0
        self.read(wanted, due, now)
        return len(due)

    def read(self, wanted: tuple[str, str, str, str], refs: list[str], now: str | None = None) -> None:
        """Read these posts (refs on your server) from it: their totals, and
        who posted them and what they say and show."""
        domain = wanted[0]
        due = [ref for ref in refs if ref.startswith(f"{domain}/")][:CHECK_POSTS]
        if not due:
            return
        ids = [ref.split("/", 1)[1] for ref in due]
        try:
            got = self._get(wanted, "/api/v1/statuses", **{"id[]": ids})
        except RemoteNotFound:  # before Mastodon 4.3: one at a time, fewer
            got, due = [], due[:ONE_BY_ONE]
            for sid in ids[:ONE_BY_ONE]:
                try:
                    got.append(self._get(wanted, f"/api/v1/statuses/{sid}"))
                except RemoteNotFound:
                    continue
        found = {f"{domain}/{s['id']}": totals(s, domain) for s in got if isinstance(s, dict) and s.get("id")}
        with self.db.transaction() as conn:
            trends.record_totals(conn, "mastodon", found, due, now or utcnow())

    def check_trending(self, wanted: tuple[str, str, str, str], now: str | None = None) -> int:
        """Your server's own trending posts, with their totals: those it saw
        liked and boosted most, which replies alone would miss. With the
        local timeline, only those posted on your server."""
        domain, scope = wanted[0], wanted[3]
        got = self._get(wanted, "/api/v1/trends/statuses", limit=40)
        found = {}
        for s in got if isinstance(got, list) else []:
            if not isinstance(s, dict) or not s.get("id"):
                continue
            if scope == "public:local" and "@" in str((s.get("account") or {}).get("acct") or ""):
                continue
            created = parse_ts(s.get("created_at"))
            if created is None or created < parse_ts(utcnow()) - trends.MAX_POST_AGE:  # type: ignore[operator]
                continue
            found[f"{domain}/{s['id']}"] = totals(s, domain)
        with self.db.transaction() as conn:
            trends.record_totals(conn, "mastodon", found, [], now or utcnow())
        return len(found)

