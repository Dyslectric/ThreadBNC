"""Archive UI + API. Private by default: every route except /login and static
assets requires an authenticated session (or API bearer token)."""

from __future__ import annotations

import difflib
import hmac
import html
import json
import logging
import math
import re
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qsl, quote, urlencode, urlparse

from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import (
    FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from starlette.middleware.sessions import SessionMiddleware

from . import articles, dupes, store, youtube
from . import search as search_mod
from . import feed as feed_mod
from . import media as media_mod
from . import storage as storage_mod
from .adapters import RemoteError, host_of, is_reddit_host, is_rss
from .accounts import Account, AccountError, Poster
from .bouncer import PUSHED_POLL_MINUTES, Bouncer
from .inbox import KINDS as INBOX_KINDS, Inbox
from .moderation import REGISTRATION_MODES, Moderation
from .private import PrivateCommunities
from .reddit import CALLBACK_PATH as REDDIT_CALLBACK_PATH, RedditError
from .reddit import callback_url as reddit_callback_url
from .youtube import is_youtube_feed
from .sso import CALLBACK_PATH, SingleSignOn, callback_url

# The reverse proxy adds this (with THREADBNC_PROXY_SECRET) to requests it has signed in.
PROXY_SECRET_HEADER = "X-ThreadBNC-Proxy-Secret"
from .config import Settings, load_settings
from .federation import Federation, InboxRelay, summarize
from .db import fmt_ts, open_database, parse_ts, utcnow
from .render import MediaInfo, looks_like_media, render_markdown
from .vault import TokenVault

log = logging.getLogger("threadbnc.web")
HERE = Path(__file__).parent

EVENT_LABELS = {
    "author_deleted": "Deleted by author",
    "author_restored": "Restored by author",
    "removed": "Removed",
    "restored": "Restored",
    "locked": "Locked",
    "unlocked": "Unlocked",
    "missing": "No longer returned by server",
    "reappeared": "Returned by server again",
    "discovered": "First observed",
    "retained": "Retained",
    "auto_captured": "Auto-captured from followed community",
    "promoted": "Kept permanently",
    "community_removed": "Community removed",
    "community_restored": "Community restored",
    "community_deleted": "Community deleted",
    "community_undeleted": "Community undeleted",
    "instance_unavailable": "Instance unreachable",
    "instance_recovered": "Instance reachable again",
    "followed": "Followed",
    "unfollowed": "Unfollowed",
    "follow_settings_changed": "Follow settings changed",
    "community_created": "Community created via ThreadBNC",
    "remove_requested": "You removed this via ThreadBNC (waiting to see it on the server)",
    "restore_requested": "You restored this via ThreadBNC (waiting to see it on the server)",
    "lock_requested": "You locked this via ThreadBNC",
    "unlock_requested": "You unlocked this via ThreadBNC",
    "pin_requested": "You pinned this via ThreadBNC",
    "unpin_requested": "You unpinned this via ThreadBNC",
    "pinned": "Pinned",
    "unpinned": "Unpinned",
    "moderator_added": "Moderator added",
    "moderator_removed": "Moderator removed",
    "auto_capture_expired": "Auto-captured thread expired and purged",
    "trashed": "Moved to trash",
    "restored_from_trash": "Restored from trash",
    "trash_expired": "Trashed thread permanently deleted (trash period ended)",
    "deleted_from_trash": "Trashed thread permanently deleted",
    "delete_requested": "You deleted this via ThreadBNC (waiting to see it on the server)",
    "undelete_requested": "You restored this via ThreadBNC (waiting to see it on the server)",
    "reposted": "You reposted this via ThreadBNC",
    "aged_out": "Dropped out of its feed; kept as it was, no longer checked",
}
ATTRIBUTION_LABELS = {
    "moderator": "by moderator",
    "admin": "by administrator",
    "author": "by author",
    "unknown": "by moderator or admin (not confirmed)",
}
TONE = {
    "author_deleted": "warn", "removed": "bad", "missing": "bad", "locked": "warn",
    "restored": "good", "author_restored": "good", "reappeared": "good", "unlocked": "good",
    "discovered": "new", "instance_unavailable": "bad", "community_removed": "bad",
    "community_deleted": "bad", "auto_capture_expired": "muted", "trash_expired": "muted",
    "deleted_from_trash": "muted", "trashed": "warn", "restored_from_trash": "good", "reposted": "new",
}


COMMENT_SORTS = {"hot": "Hot", "top": "Top", "new": "New", "old": "Old", "controversial": "Controversial"}
# A feed's settings, in words (feed.py's SORTS, WINDOWS and unread filters).
FEED_SORTS = {"new": "New", "active": "Active", "top": "Top", "comments": "Most comments"}
FEED_WINDOWS = {"day": "Past day", "week": "Past week", "month": "Past month", "all": "All time"}
FEED_SHOWS = {"": "All posts", "posts": "Unread posts", "comments": "New comments", "any": "Unread or new comments"}


def _hours_since(ts: str | None) -> float:
    dt = parse_ts(ts)
    return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds() / 3600) if dt else 1e6


def _net_score(o: Any) -> int:
    return o["score"] if o["score"] is not None else (o["upvotes"] or 0) - (o["downvotes"] or 0)


def _controversy(o: Any) -> float:  # Lemmy's formula: many votes, evenly split
    up, down = o["upvotes"] or 0, o["downvotes"] or 0
    if not up or not down:
        return 0.0
    return (up + down) ** (min(up, down) / max(up, down))


def _hot(o: Any) -> float:  # Lemmy's formula: log(max(1, 3 + score)) / (hours + 2) ^ 1.8
    return math.log(max(1, 3 + _net_score(o))) / (_hours_since(o["created_at"]) + 2) ** 1.8


def comment_sort_key(sort: str) -> tuple[Callable[[Any], Any], bool]:
    """(key, reverse) for sibling comments, using counts from the last observation
    (combined across copies for squashed comments: see node["agg"])."""
    keys: dict[str, tuple[Callable[[Any], Any], bool]] = {
        "hot": (lambda n: _hot(n["agg"]), True),
        "top": (lambda n: (_net_score(n["agg"]), n["agg"]["created_at"] or ""), True),
        "new": (lambda n: n["agg"]["created_at"] or "", True),
        "old": (lambda n: n["agg"]["created_at"] or "", False),
        "controversial": (lambda n: (_controversy(n["agg"]), _net_score(n["agg"])), True),
    }
    return keys.get(sort, keys["hot"])


def sort_tree(node: dict[str, Any], sort: str) -> None:
    key, reverse = comment_sort_key(sort)
    stack = [node]
    while stack:
        n = stack.pop()
        n["children"].sort(key=key, reverse=reverse)
        stack.extend(n["children"])


def squash_comments(children: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fold sibling comments that say the same thing, from the same person (a
    comment repeated under each copy of a crosspost, or a double submit) into
    one node whose `members` are the individual comments; their replies are
    pooled and squashed in turn."""
    out: list[dict[str, Any]] = []
    seen: dict[tuple[str, str], dict[str, Any]] = {}
    for n in children:
        key = dupes.comment_key(n["o"]["a_ap"], n["o"]["body"])
        first = seen.get(key) if key else None
        if first is None:
            if key:
                seen[key] = n
            out.append(n)
            continue
        first["members"].extend(n["members"])
        first["children"].extend(n["children"])
        first["events"] = sorted(first["events"] + n["events"], key=lambda e: e["observed_at"] or "")
        first["is_new"] = first["is_new"] or n["is_new"]
        first["is_changed"] = first["is_changed"] or n["is_changed"]
    for n in out:
        n["children"] = squash_comments(n["children"])
    return out


def _days(text: str) -> int | None:
    """Ban length from a form field: blank/0 means permanent."""
    try:
        n = int((text or "").strip() or 0)
    except ValueError:
        return None
    return n if n > 0 else None


def event_label(e: Any) -> str:
    et = e["event_type"]
    label = EVENT_LABELS.get(et, et.replace("_", " ").capitalize())
    if et in ("removed", "restored", "locked", "unlocked", "community_removed") and e["attribution"]:
        label += " " + ATTRIBUTION_LABELS.get(e["attribution"], f"by {e['attribution']}")
    meta = json.loads(e["metadata_json"] or "{}")
    if et == "reposted" and meta.get("to"):
        label = f"You reposted this to {meta['to']} as {meta.get('account', '?')}"
    if meta.get("state_at_first_observation"):
        label += " (already in this state when first observed)"
    return label


def human_size(n: int | None) -> str:
    """Bytes as KB/MB/GB (decimal, like disk makers and our size limits)."""
    n = n or 0
    for unit, scale in (("GB", 1e9), ("MB", 1e6), ("KB", 1e3)):
        if n >= scale:
            v = n / scale
            return f"{v:.1f} {unit}" if v < 10 else f"{v:,.0f} {unit}"
    return f"{n} bytes"


def ago(value: str | None) -> str:
    dt = parse_ts(value)
    if not dt:
        return "—"
    secs = (datetime.now(timezone.utc) - dt).total_seconds()
    future = secs < 0
    secs = abs(secs)
    for unit, size in (("year", 31536000), ("month", 2592000), ("day", 86400), ("hour", 3600), ("minute", 60)):
        if secs >= size:
            n = int(secs // size)
            s = f"{n} {unit}{'s' if n != 1 else ''}"
            return f"in {s}" if future else f"{s} ago"
    return "just now"


def absolute(value: str | None) -> str:
    dt = parse_ts(value)
    return dt.strftime("%Y-%m-%d %H:%M UTC") if dt else "—"


def clock(value: str | None) -> str:
    """The time of day (UTC), for lists already split into days."""
    dt = parse_ts(value)
    return dt.strftime("%H:%M") if dt else ""


def safe_url(value: str | None) -> str | None:
    if not value:
        return None
    return value if urlparse(value).scheme in ("http", "https") else None


def is_reddit(ap_id: str | None) -> bool:
    return is_reddit_host(host_of(ap_id or ""))


def chandle(name: str, ap_id: str | None, plain: bool = False) -> Markup | str:
    """A community's handle: r/name for subreddits, the feed's title for feeds
    (the channel's name for YouTube),
    !name@host otherwise (with the host dimmed unless `plain`)."""
    if is_reddit(ap_id):
        return f"r/{name}"
    if is_youtube_feed(ap_id):
        return f"{name} (YouTube)" if plain else Markup('{}<span class="muted"> · YouTube</span>').format(name)
    if is_rss(ap_id):
        return f"{name} (feed)" if plain else Markup('{}<span class="muted"> · feed</span>').format(name)
    host = host_of(ap_id or "")
    if plain:
        return f"!{name}@{host}"
    return Markup('!{}<span class="muted">@{}</span>').format(name, host)


FETCH_HEADER = "X-ThreadBNC-Fetch"  # app.js sends this with forms it submits in the background
THEMES = ("light", "dark")
_ANCHOR = re.compile(r"[A-Za-z][\w-]{0,40}")


def is_fetch(request: Request) -> bool:
    return request.headers.get(FETCH_HEADER) == "1"


def flash_json(f: list[Any]) -> dict[str, Any]:
    extra = f[2] if len(f) > 2 and f[2] else {}
    return {"kind": f[0], "text": f[1], "undo": extra if "action" in extra else None,
            "link": extra if "href" in extra else None}


def safe_anchor(anchor: str | None) -> str | None:
    """An element id a form asked to return to, if it looks like one."""
    return anchor if anchor and _ANCHOR.fullmatch(anchor) else None


def word_diff(old: str | None, new: str | None) -> Markup:
    a, b = (old or "").split(" "), (new or "").split(" ")
    out = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op == "equal":
            out.append(html.escape(" ".join(a[i1:i2])))
        if op in ("delete", "replace"):
            out.append(f"<del>{html.escape(' '.join(a[i1:i2]))}</del>")
        if op in ("insert", "replace"):
            out.append(f"<ins>{html.escape(' '.join(b[j1:j2]))}</ins>")
    return Markup(" ".join(out))


def create_app(settings: Settings | None = None, bouncer: Bouncer | None = None) -> FastAPI:
    settings = settings or load_settings()
    if settings.proxy_auth_header and len(settings.proxy_secret or "") < 16:
        raise SystemExit("THREADBNC_PROXY_AUTH_HEADER needs THREADBNC_PROXY_SECRET (16+ characters), which the "
                         "proxy adds to every request it has signed in; without it anyone who can reach the app "
                         "directly could send the header themselves.")
    if not settings.password and not settings.proxy_auth_header:
        raise SystemExit("Set THREADBNC_PASSWORD, or THREADBNC_PROXY_AUTH_HEADER when a proxy signs people in; "
                         "the archive is never served unauthenticated.")
    db = bouncer.db if bouncer else open_database(settings)
    bouncer = bouncer or Bouncer(db, settings)
    poster = Poster(bouncer, TokenVault(settings.credentials_key, settings.data_dir))
    mod = Moderation(poster)
    private = PrivateCommunities(mod)
    bouncer.hooks.append(private.sweep)
    bouncer.read_token = private.read_token
    sso = SingleSignOn(poster)
    inbox = Inbox(poster, settings.inbox_poll_minutes)
    bouncer.hooks.append(inbox.sweep)
    # Your own servers' inboxes, relayed through ThreadBNC so what they receive is pushed here too.
    federation = Federation(poster, settings.relay_inboxes) if settings.relay_inboxes else None

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if settings.embedded_bouncer:
            bouncer.start_thread()
            if federation:
                federation.start_thread()
        yield
        bouncer.stop()
        if federation:
            federation.stop()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    templates = Jinja2Templates(directory=str(HERE / "templates"))
    templates.env.filters.update(ago=ago, absolute=absolute, clock=clock,safe_url=safe_url, host=host_of,
                                 looks_like_media=looks_like_media, size=human_size, youtube_video=youtube.video_id)
    # A Lemmy or PieFed community (not a subreddit or a feed): one that can be pushed.
    templates.env.tests["is_federated"] = lambda ap_id: not is_rss(ap_id) and not is_reddit_host(host_of(ap_id))
    def static_url(name: str) -> str:
        # Cache-bust with the file's mtime so UI updates show up without a hard reload.
        return f"/static/{name}?v={int((HERE / 'static' / name).stat().st_mtime)}"

    def trash_count() -> int:
        with db.connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM archived_threads WHERE trashed_at IS NOT NULL").fetchone()[0]

    def mark_on_scroll() -> bool:
        return db.get_setting("mark_read_on_scroll") == "1"

    def custom_feeds() -> list[dict[str, Any]]:
        with db.connect() as conn:
            return feed_mod.custom_feeds(conn)

    def following_collapsed() -> bool:
        return db.get_setting("following_collapsed") == "1"

    templates.env.globals.update(event_label=event_label, tone=lambda e: TONE.get(e["event_type"], ""),
                                 static_url=static_url, trash_count=trash_count, inbox_count=inbox.unread_count,
                                 mark_on_scroll=mark_on_scroll, themes=THEMES, custom_feeds=custom_feeds,
                                 following_collapsed=following_collapsed, feed_sorts=FEED_SORTS,
                                 feed_windows=FEED_WINDOWS, feed_shows=FEED_SHOWS,
                                 password_login=bool(settings.password),
                                 proxy_login=bool(settings.proxy_auth_header), chandle=chandle,
                                 is_reddit=is_reddit, is_rss=is_rss, is_youtube=is_youtube_feed,
                                 reading_articles=bouncer.articles.enabled)
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

    # The SSO return page comes from the identity provider's site, so the
    # (SameSite=strict) session cookie isn't sent; it's authorised by its state.
    PUBLIC = ("/login", "/static/", "/robots.txt", "/healthz", CALLBACK_PATH, REDDIT_CALLBACK_PATH)

    def proxy_user(request: Request) -> str | None:
        """Who the reverse proxy says this is, if it really came through the
        proxy (it carries the proxy's secret). When the user header comes
        without a matching secret, say why on request.state.proxy_problem."""
        if not settings.proxy_auth_header:
            return None
        name = request.headers.get(settings.proxy_auth_header, "").strip()
        given = request.headers.get(PROXY_SECRET_HEADER, "")
        if name and given and hmac.compare_digest(given.encode(), settings.proxy_secret.encode()):  # type: ignore[union-attr]
            return name
        if name:
            problem = (f"{settings.proxy_auth_header} says you're {name}, but "
                       + (f"the {PROXY_SECRET_HEADER} header doesn't match THREADBNC_PROXY_SECRET" if given else
                          f"the request has no {PROXY_SECRET_HEADER} header")
                       + ", so ThreadBNC can't trust it. Check that the proxy adds the header with the same "
                         "secret the app has (see README).")
            request.state.proxy_problem = problem
            log.warning("proxy sign-in ignored: %s", problem)
        return None

    @app.middleware("http")
    async def guard(request: Request, call_next):
        path = request.url.path
        via_proxy = proxy_user(request)
        if via_proxy:
            if settings.proxy_allowed_users and via_proxy.lower() not in settings.proxy_allowed_users:
                return PlainTextResponse(f"{via_proxy} isn't allowed to use this archive.", status_code=403)
            if request.session.get("proxy_user") != via_proxy:
                request.session.clear()
                request.session.update(auth=True, proxy_user=via_proxy)
        elif request.session.get("proxy_user"):
            # Signed in by the proxy earlier, but this request didn't come through
            # it (the proxy session ended, or the app was reached directly).
            request.session.clear()
        authed = bool(request.session.get("auth"))
        if not authed and settings.api_token:
            header = request.headers.get("authorization", "")
            authed = header.startswith("Bearer ") and hmac.compare_digest(header[7:], settings.api_token)
        if not authed and not path.startswith(PUBLIC):
            wants_json = "application/json" in (request.headers.get("accept", "")
                                                + request.headers.get("content-type", ""))
            if path.startswith("/api/") or wants_json:
                resp: Response = JSONResponse({"error": "authentication required"}, status_code=401)
            else:
                target = path + (f"?{request.url.query}" if request.url.query else "")
                wanted = request.method == "GET" and target != "/"
                resp = RedirectResponse(f"/login?{urlencode({'next': target})}" if wanted else "/login",
                                        status_code=303)
        else:
            resp = await call_next(request)
            if is_fetch(request) and resp.status_code in (302, 303):
                # A form sent by app.js: answer with where it would have gone and
                # its messages, so the page can update in place instead of reloading.
                messages = [flash_json(f) for f in request.session.pop("flash", [])]
                cookies = resp.headers.getlist("set-cookie")
                resp = JSONResponse({"ok": not any(m["kind"] == "error" for m in messages),
                                     "redirect": resp.headers.get("location", ""), "messages": messages})
                for c in cookies:
                    resp.headers.append("set-cookie", c)
        resp.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive"
        resp.headers["Referrer-Policy"] = "same-origin"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self'; media-src 'self'; style-src 'self'; script-src 'self'; "
            "form-action 'self'; "
            "frame-ancestors 'none'"
        )
        if path.startswith("/media/") and resp.status_code == 200:
            resp.headers["Cache-Control"] = "private, max-age=31536000, immutable"
            resp.headers["Content-Security-Policy"] = "default-src 'none'; sandbox"
        elif not path.startswith("/static/"):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    # Added after the guard so it wraps it (session must be decoded first).
    app.add_middleware(SessionMiddleware, secret_key=settings.secret_key, same_site="strict",
                       https_only=settings.https_only_cookies, max_age=60 * 60 * 24 * 14,
                       session_cookie="threadbnc_session")
    if federation:  # outermost: deliveries to your servers' inboxes never reach sessions or sign-in
        app.add_middleware(InboxRelay, relays=settings.relay_inboxes, accept=federation.queue,
                           refuse=federation.refused)
    app.state.federation = federation

    def push_handle() -> str | None:
        account = federation.account() if federation else None
        return account.handle if account else None

    def acting(request: Request) -> Account | None:
        """The account forms act as: the one picked in the header, else the
        default. Never the Reddit account: Poster.account_for switches to it by
        itself for anything on Reddit."""
        chosen = poster.get(request.session.get("acting_account"))
        return chosen if chosen and not chosen.is_reddit else poster.default()

    def render(request: Request, name: str, **ctx: Any) -> HTMLResponse:
        if request.session.get("auth"):
            ctx.setdefault("accounts", poster.list())
            ctx.setdefault("acting", acting(request))
            ctx.setdefault("reddit_me", poster.reddit_account())
        return templates.TemplateResponse(request, name, ctx)

    def require_acting(request: Request) -> Account:
        account = acting(request) or poster.reddit_account()
        if account is None:
            raise AccountError("Add an account on the Accounts page first.")
        return account

    def flash(request: Request, msg: str, kind: str = "info", undo: dict[str, Any] | None = None,
              link: dict[str, str] | None = None) -> None:
        """A message for the next page. `undo` ({"action": url, "fields": {...}})
        adds an Undo button that posts those fields there; `link` ({"href", "label"}) a link."""
        extra = undo or link
        request.session.setdefault("flash", []).append([kind, msg, extra] if extra else [kind, msg])

    # ---- auth ------------------------------------------------------------
    @app.get("/healthz")
    def healthz():
        """Unauthenticated health check for containers/proxies. Reveals nothing
        about the archive beyond whether the database answers."""
        try:
            with db.connect() as conn:
                conn.execute("SELECT 1").fetchone()
        except Exception:
            return JSONResponse({"ok": False}, status_code=503)
        return {"ok": True}

    @app.get("/robots.txt", response_class=PlainTextResponse)
    def robots() -> str:
        return "User-agent: *\nDisallow: /\n"

    def local_path(url: str, default: str = "/") -> str:
        """`url` if it's a path on this site, else `default` (no open redirects)."""
        ok = url.startswith("/") and not url.startswith(("//", "/\\")) and not urlparse(url).netloc
        return url if ok else default

    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request, next: str = "/"):
        return render(request, "login.html", error=None, next=local_path(next),
                      proxy_problem=getattr(request.state, "proxy_problem", None))

    @app.post("/login")
    def login(request: Request, password: str = Form(...), next: str = Form("/")):
        if not settings.password:
            return render(request, "login.html", error=None, next=local_path(next))
        if hmac.compare_digest(password.encode(), settings.password.encode()):
            request.session.clear()
            request.session["auth"] = True
            return RedirectResponse(local_path(next), status_code=303)
        time.sleep(1.0)
        return render(request, "login.html", error="Wrong password.", next=local_path(next))

    @app.post("/logout")
    def logout(request: Request):
        via_proxy = bool(request.session.get("proxy_user"))
        request.session.clear()
        # Signed in by the proxy: end that session too, or the next page signs you straight back in.
        target = settings.proxy_logout_url if via_proxy and settings.proxy_logout_url else "/login"
        return RedirectResponse(target, status_code=303)

    # ---- feed (home) -----------------------------------------------------
    def back(request: Request, default: str, anchor: str | None = None) -> str:
        """The page the form was on (from the same-origin Referer), else `default`."""
        ref = urlparse(request.headers.get("referer") or "")
        if ref.path.startswith("/") and ref.netloc in ("", request.url.netloc):
            url = ref.path + (f"?{ref.query}" if ref.query else "")
        else:
            url = default.split("#")[0]
        return url + (f"#{anchor}" if anchor else "")

    def with_at(url: str, at: str) -> str:
        """A feed's address, with the list as of `at` (feed_as_of)."""
        u = urlparse(url)
        query = [(k, v) for k, v in parse_qsl(u.query, keep_blank_values=True) if k != "at"] + [("at", at)]
        return u._replace(query=urlencode(query)).geturl()

    def object_page(oid: int) -> str:
        """Where an object lives: its thread, scrolled to it."""
        with db.connect() as conn:
            row = conn.execute("SELECT thread_id FROM objects WHERE id=?", (oid,)).fetchone()
        return f"/t/{row[0]}" if row and row[0] else "/"

    def save_view(key: str, view: str | None, cid: int | None = None) -> None:
        """Remember a List/Tiles choice ('auto' forgets it) for a community, or
        for the home feed."""
        if view not in (*feed_mod.VIEWS, "auto"):
            return
        value = None if view == "auto" else view
        with db.transaction() as conn:
            if cid is not None:
                conn.execute("UPDATE communities SET view_mode=? WHERE id=?", (value, cid))
            elif value is None:
                conn.execute("DELETE FROM app_settings WHERE key=?", (key,))
            else:
                conn.execute("INSERT INTO app_settings(key, value) VALUES (?, ?) "
                             "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    SNAPSHOT_MAX_AGE = timedelta(days=1)

    def feed_as_of(request: Request, at: str | None) -> tuple[str, bool]:
        """The time a feed's list is as of, and whether that's from the address.
        A feed page puts the time it was made in its address (app.js), so going
        back to it shows the same posts: none that arrived since, and the ones
        read since still there. Reloading (the browser says max-age=0 or
        no-cache) starts afresh, as does an old or unreadable time."""
        then = parse_ts(at)
        now = datetime.now(timezone.utc)
        cache = request.headers.get("cache-control", "") + request.headers.get("pragma", "")
        reloaded = "max-age=0" in cache or "no-cache" in cache
        if then is None or reloaded or not timedelta(0) <= now - then <= SNAPSHOT_MAX_AGE:
            return fmt_ts(now), False
        return fmt_ts(then), True

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request, sort: str | None = None, t: str | None = None, unread: str | None = None,
             page: int = 1, view: str | None = None, at: str | None = None):
        """The main feed: every followed community, as the address says or else
        as its defaults say."""
        as_of, snapshot = feed_as_of(request, at)
        save_view("home_view", view)
        chosen = db.get_setting("home_view")
        with db.connect() as conn:
            defaults = feed_mod.load_defaults(conn)
            s = defaults.with_url(sort, t, unread)
            fp = feed_mod.load_feed(conn, sort=s.sort, window=s.window, unread=s.unread, page=page, as_of=as_of)
            follows = feed_mod.followed_communities(conn)
        # The home feed mixes communities, so "auto" goes by what's on this page.
        shown = feed_mod.pick_view(chosen, sum(1 for i in fp.items if i["thumb"]), len(fp.items))
        return render(request, "feed.html", feed=fp, follows=follows, sort=s.sort, window=s.window, unread=s.unread,
                      defaults=defaults, custom=None, base_url="/", community=None, view=shown, view_chosen=chosen,
                      snapshot=snapshot)

    @app.get("/f/{fid}", response_class=HTMLResponse)
    def custom_feed_page(request: Request, fid: int, sort: str | None = None, t: str | None = None,
                         unread: str | None = None, page: int = 1, view: str | None = None, at: str | None = None):
        """One of your own feeds: its communities, shown with its own defaults."""
        as_of, snapshot = feed_as_of(request, at)
        if view in (*feed_mod.VIEWS, "auto"):
            with db.transaction() as conn:
                conn.execute("UPDATE custom_feeds SET view_mode=? WHERE id=?", (None if view == "auto" else view, fid))
        with db.connect() as conn:
            custom = feed_mod.custom_feed(conn, fid)
            if custom is None:
                raise HTTPException(404)
            defaults = feed_mod.feed_settings(custom, feed_mod.load_defaults(conn))
            s = defaults.with_url(sort, t, unread)
            fp = feed_mod.load_feed(conn, community_ids=custom["community_ids"], sort=s.sort, window=s.window,
                                    unread=s.unread, page=page, as_of=as_of)
            follows = feed_mod.followed_communities(conn)
        shown = feed_mod.pick_view(custom["view_mode"], sum(1 for i in fp.items if i["thumb"]), len(fp.items))
        return render(request, "feed.html", feed=fp, follows=follows, sort=s.sort, window=s.window, unread=s.unread,
                      defaults=defaults, custom=custom, base_url=f"/f/{fid}", community=None, view=shown,
                      view_chosen=custom["view_mode"], snapshot=snapshot)

    @app.post("/feed/defaults")
    def feed_defaults(request: Request, sort: str = Form("new"), t: str = Form("all"), unread: str = Form(""),
                      feed_id: int | None = Form(None)):
        """Make what a feed shows now its default: the main feed's (which
        community pages use too), or one of your own feeds'."""
        s = feed_mod.FeedSettings.clean(sort, t, unread)
        with db.transaction() as conn:
            if feed_id is None:
                feed_mod.save_defaults(conn, s)
            else:
                feed_mod.save_custom_feed_settings(conn, feed_id, s)
        flash(request, f"Now shown by default: {describe_settings(s)}.")
        return RedirectResponse(f"/f/{feed_id}" if feed_id else "/", status_code=303)

    def describe_settings(s: feed_mod.FeedSettings) -> str:
        return " · ".join([FEED_SORTS[s.sort], FEED_WINDOWS[s.window], FEED_SHOWS[s.unread]])

    def feed_form(request: Request, custom: dict[str, Any] | None, error: str | None = None) -> HTMLResponse:
        with db.connect() as conn:
            follows = feed_mod.followed_communities(conn)
            chosen = set(custom["community_ids"]) if custom else set()
            followed = {f["id"] for f in follows}
            others = [dict(r) for r in conn.execute(  # in the feed, but not followed any more
                "SELECT id, name, title, canonical_ap_id FROM communities WHERE id IN ({})".format(
                    ",".join("?" * len(chosen - followed))), sorted(chosen - followed))] if chosen - followed else []
            defaults = feed_mod.load_defaults(conn)
        return render(request, "feed_edit.html", custom=custom, communities=follows + others, chosen=chosen,
                      settings=feed_mod.feed_settings(custom, defaults) if custom else defaults, error=error,
                      sorts=FEED_SORTS, windows=FEED_WINDOWS, shows=FEED_SHOWS)

    @app.get("/feeds/new", response_class=HTMLResponse)
    def new_feed(request: Request):
        return feed_form(request, None)

    @app.get("/f/{fid}/edit", response_class=HTMLResponse)
    def edit_feed(request: Request, fid: int):
        with db.connect() as conn:
            custom = feed_mod.custom_feed(conn, fid)
        if custom is None:
            raise HTTPException(404)
        return feed_form(request, custom)

    @app.post("/feeds")
    @app.post("/f/{fid}/edit")
    def save_feed(request: Request, fid: int | None = None, name: str = Form(""), community: list[int] = Form([]),
                  sort: str = Form("new"), t: str = Form("all"), unread: str = Form(""), view: str = Form("auto")):
        name = " ".join(name.split())[:80]
        if not name:
            with db.connect() as conn:
                custom = feed_mod.custom_feed(conn, fid) if fid else None
            return feed_form(request, custom, "Give the feed a name.")
        with db.transaction() as conn:
            fid = feed_mod.save_custom_feed(conn, fid, name, community, feed_mod.FeedSettings.clean(sort, t, unread),
                                            view, utcnow())
        flash(request, f"Saved “{name}”.")
        return RedirectResponse(f"/f/{fid}", status_code=303)

    @app.post("/f/{fid}/delete")
    def delete_feed(request: Request, fid: int):
        with db.transaction() as conn:
            feed_mod.delete_custom_feed(conn, fid)
        flash(request, "Feed deleted. Its communities and posts are untouched.")
        return RedirectResponse("/", status_code=303)

    @app.post("/feed/sidebar")
    def feed_sidebar(request: Request, collapsed: str = Form("0")):
        """Remember whether the Following list is collapsed."""
        with db.transaction() as conn:
            conn.execute("INSERT INTO app_settings(key, value) VALUES ('following_collapsed', ?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value", ("1" if collapsed == "1" else "0",))
        return RedirectResponse(back(request, "/"), status_code=303)

    @app.post("/feed/mark-read")
    def mark_read(request: Request, community_id: int | None = Form(None), feed_id: int | None = Form(None)):
        stamp = utcnow()
        with db.transaction() as conn:
            custom = feed_mod.custom_feed(conn, feed_id) if feed_id else None
            n = feed_mod.mark_read(conn, stamp, community_id, custom["community_ids"] if custom else None)
        if n:
            flash(request, f"Marked {n} post{'s' if n != 1 else ''} as read.",
                  undo={"action": "/feed/mark-unread", "fields": {"stamp": stamp}})
        else:
            flash(request, "Nothing was unread.")
        # The page's list is as of now, so what was just marked goes (feed_as_of).
        return RedirectResponse(with_at(back(request, "/"), stamp), status_code=303)

    @app.post("/feed/mark-unread")
    def mark_unread(request: Request, stamp: str = Form(...)):
        """Undo a Mark all read: whatever it marked goes back to how it was."""
        with db.transaction() as conn:
            feed_mod.unmark_read(conn, stamp)
        flash(request, "Back to how it was.")
        return RedirectResponse(back(request, "/"), status_code=303)

    @app.post("/feed/seen")
    def feed_seen(ids: list[int] = Form([])):
        """Posts scrolled past in the feed (when that setting is on): read, as
        if opened, but only if they were unread."""
        with db.transaction() as conn:
            n = feed_mod.mark_seen(conn, utcnow(), ids[:200])
        return {"ok": True, "marked": n}

    def feed_roots(conn: Any, ids: list[int]) -> list[Any]:
        """These threads' posts and their current links."""
        if not ids:
            return []
        marks = ",".join("?" * len(ids))
        return conn.execute(
            f"SELECT t.id, o.id AS oid, r.url, o.thumbnail_url FROM archived_threads t "
            f"JOIN objects o ON o.id=t.root_object_id JOIN revisions r ON r.object_id=o.id "
            f"AND r.seq=o.revision_count WHERE t.id IN ({marks})", ids).fetchall()

    @app.post("/feed/articles")
    def feed_articles(ids: list[int] = Form([])):
        """Posts app.js scrolled into view whose linked article (or its
        pictures) isn't saved yet, or whose audio file isn't downloaded: fetch
        them (Bouncer.fetch_articles, Bouncer.fetch_audio)."""
        ids = ids[:50]
        if ids and bouncer.articles.enabled:
            bouncer.enqueue("articles", {"thread_ids": ids})
        with db.connect() as conn:
            rows = feed_roots(conn, ids)
            sounds = feed_mod.audio(conn, [(r["oid"], r["url"]) for r in rows])
        if audio_ids := [r["id"] for r in rows if sounds.get(r["oid"], {}).get("status") == "pending"]:
            bouncer.enqueue("audio", {"thread_ids": audio_ids})
        return {"ok": True}

    @app.get("/feed/articles")
    def feed_articles_status(ids: list[int] = Query([])):
        """How those posts are getting on: which are still waiting on their
        article, its pictures or their audio file, how many pictures each has
        to show, and where each one's audio file is at."""
        with db.connect() as conn:
            rows = feed_roots(conn, ids[:50])
            waiting = articles.waiting(conn, [(r["oid"], r["url"]) for r in rows]) \
                if bouncer.articles.enabled else set()
            thumbs = feed_mod.thumbnails(conn, [(r["oid"], r["url"], r["thumbnail_url"]) for r in rows])
            sounds = feed_mod.audio(conn, [(r["oid"], r["url"]) for r in rows])
        return {"waiting": [r["id"] for r in rows if r["oid"] in waiting
                            or sounds.get(r["oid"], {}).get("status") == "pending"],
                "pics": {r["id"]: len(thumbs[r["oid"]]["pics"]) if r["oid"] in thumbs else 0 for r in rows},
                "audio": {r["id"]: sounds[r["oid"]]["status"] for r in rows if r["oid"] in sounds}}

    @app.post("/feed/settings")
    def feed_settings(request: Request, mark_read_on_scroll: str = Form("")):
        on = mark_read_on_scroll == "1"
        with db.transaction() as conn:
            conn.execute("INSERT INTO app_settings(key, value) VALUES ('mark_read_on_scroll', ?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value", ("1" if on else "0",))
        flash(request, "Posts are marked read as you scroll past them." if on else
              "Posts stay unread until you open them.")
        return RedirectResponse(back(request, "/"), status_code=303)

    @app.post("/theme")
    def set_theme(request: Request, theme: str = Form("")):
        """Light, dark, or (anything else) follow the device. Kept per browser."""
        resp = RedirectResponse(back(request, "/"), status_code=303)
        if theme in THEMES:
            resp.set_cookie("theme", theme, max_age=60 * 60 * 24 * 400, samesite="lax",
                            httponly=True, secure=settings.https_only_cookies)
        else:
            resp.delete_cookie("theme")
        return resp

    # ---- kept (the archive) ---------------------------------------------
    KEPT_TABS = ("threads", "videos", "audio", "articles", "changed", "log")
    KEPT_MEDIA = {"videos": "video", "audio": "audio"}  # tab: feed.MEDIA_KINDS
    KEPT_PAGE = 25
    LOG_PAGE = 100

    @app.get("/kept", response_class=HTMLResponse)
    def kept(request: Request, tab: str = "threads", sort: str | None = None, t: str | None = None,
             unread: str | None = None, page: int = 1, view: str | None = None, everything: str = "",
             at: str | None = None):
        """Kept posts read like a feed, with tabs for just the videos and audio,
        kept articles, posts where something changed lately, and the log of
        every change seen."""
        tab = tab if tab in KEPT_TABS else "threads"
        page = max(1, page)
        save_view("kept_view", view)
        as_of, snapshot = feed_as_of(request, at)
        out: dict[str, Any] = {"tab": tab, "page": page, "snapshot": snapshot}
        with db.connect() as conn:
            since = fmt_ts(parse_ts(utcnow()) - timedelta(hours=1))  # type: ignore[operator]
            jobs = conn.execute("SELECT * FROM jobs WHERE kind='ingest' AND (status IN ('queued', 'running') "
                                "OR updated_at>=?) ORDER BY id DESC LIMIT 6", (since,)).fetchall()
            out["counts"] = {
                "threads": conn.execute("SELECT COUNT(*) FROM archived_threads WHERE retention='manual' "
                                        "AND trashed_at IS NULL").fetchone()[0],
                "articles": kept_articles_count(conn),
                **{k: feed_mod.count_kept_media(conn, m) for k, m in KEPT_MEDIA.items()},
            }
            if tab == "threads" or tab in KEPT_MEDIA:
                s = feed_mod.load_defaults(conn).with_url(sort, t, unread)
                out.update(sort=s.sort, window=s.window, unread=s.unread,
                           feed=feed_mod.load_feed(conn, kept_only=True, media=KEPT_MEDIA.get(tab, ""), sort=s.sort,
                                                   window=s.window, unread=s.unread, page=page, as_of=as_of))
                chosen = db.get_setting("kept_view")
                out.update(view_chosen=chosen, view=feed_mod.pick_view(
                    chosen, sum(1 for i in out["feed"].items if i["thumb"]), len(out["feed"].items)))
            elif tab == "articles":
                out["articles"], out["has_more"] = kept_articles_page(conn, page)
            elif tab == "changed":
                out["feed"] = changed_posts(conn, page)
            else:
                out["everything"] = everything == "1"
                out["days"], out["has_more"] = changes_by_day(conn, page, out["everything"])
        out["jobs"] = [dict(j, payload=json.loads(j["payload_json"]), result=json.loads(j["result_json"] or "null"))
                       for j in jobs]
        out["pending"] = any(j["status"] in ("queued", "running") for j in jobs)
        return render(request, "kept.html", **out)

    # Articles on the Kept page: those kept while reading, and those kept posts link to.
    KEPT_ARTICLES = ("FROM articles a WHERE a.kept_at IS NOT NULL OR (a.status='ok' AND EXISTS ("
                     "SELECT 1 FROM article_refs ar JOIN objects o ON o.id=ar.object_id "
                     "JOIN archived_threads t ON t.root_object_id=o.id "
                     "WHERE ar.article_id=a.id AND t.retention='manual' AND t.trashed_at IS NULL))")

    def kept_articles_count(conn: Any) -> int:
        return conn.execute(f"SELECT COUNT(*) {KEPT_ARTICLES}").fetchone()[0]

    def kept_articles_page(conn: Any, page: int) -> tuple[list[dict[str, Any]], bool]:
        rows = conn.execute(
            f"SELECT a.id, a.url, a.status, a.title, a.byline, a.site_name, a.published, a.word_count, "
            f"a.content_html, a.kept_at, a.fetched_at, a.first_seen_at {KEPT_ARTICLES} "
            f"ORDER BY COALESCE(a.kept_at, a.fetched_at, a.first_seen_at) DESC, a.id DESC LIMIT ? OFFSET ?",
            (KEPT_PAGE + 1, (page - 1) * KEPT_PAGE)).fetchall()
        items = [dict(r) for r in rows[:KEPT_PAGE]]
        ids = [a["id"] for a in items]
        pics: dict[int, int] = {}
        posts: dict[int, dict[str, Any]] = {}
        if ids:
            marks = ",".join("?" * len(ids))
            for r in conn.execute(
                    f"SELECT am.article_id, MIN(m.id) AS mid FROM article_media am JOIN media m ON m.id=am.media_id "
                    f"WHERE am.article_id IN ({marks}) AND m.status='ok' AND m.content_type LIKE 'image/%' "
                    f"AND m.content_type != 'image/svg+xml' GROUP BY am.article_id", ids):
                pics[r["article_id"]] = r["mid"]
            for r in conn.execute(  # the kept post that links to it, newest first
                    f"SELECT ar.article_id, t.id AS tid, c.name AS cname, c.canonical_ap_id AS c_ap "
                    f"FROM article_refs ar JOIN objects o ON o.id=ar.object_id "
                    f"JOIN archived_threads t ON t.root_object_id=o.id AND t.trashed_at IS NULL "
                    f"JOIN communities c ON c.id=t.community_id "
                    f"WHERE ar.article_id IN ({marks}) ORDER BY t.retention='manual', t.id", ids):
                posts[r["article_id"]] = dict(r)
        for a in items:
            a["picture"] = pics.get(a["id"])
            a["post"] = posts.get(a["id"])
            a["excerpt"] = feed_mod.excerpt(articles.excerpt(a.pop("content_html"), 400), 260)
            a["minutes"] = max(1, round((a["word_count"] or 0) / 230)) if a["word_count"] else None
        return items, len(rows) > KEPT_PAGE

    def changed_posts(conn: Any, page: int) -> feed_mod.FeedPage:
        """Posts where something changed, the most recent change first, each
        with what changed lately."""
        rows = conn.execute(
            """SELECT ch.thread_id, MAX(ch.at) AS last_change FROM (
                 SELECT o.thread_id, e.observed_at AS at FROM state_events e JOIN objects o ON o.id=e.object_id
                 WHERE e.event_type NOT IN ('discovered', 'retained', 'auto_captured', 'promoted')
                 UNION ALL
                 SELECT o.thread_id, r.observed_at FROM revisions r JOIN objects o ON o.id=r.object_id WHERE r.seq>1
               ) AS ch JOIN archived_threads th ON th.id=ch.thread_id AND th.trashed_at IS NULL
               GROUP BY ch.thread_id ORDER BY last_change DESC LIMIT ? OFFSET ?""",
            (KEPT_PAGE + 1, (page - 1) * KEPT_PAGE)).fetchall()
        ids = [r["thread_id"] for r in rows[:KEPT_PAGE]]
        fp = feed_mod.load_feed(conn, thread_ids=ids, per_page=KEPT_PAGE)
        by_id = {i["id"]: i for i in fp.items}
        items = []
        for r in rows[:KEPT_PAGE]:
            item = by_id.get(r["thread_id"])
            if item is None:  # shown as another copy of the same link
                continue
            recent = recent_changes(conn, limit=12, thread_id=r["thread_id"], meaningful=True)
            tally: dict[tuple[str, str], int] = {}
            for ch in recent:
                key = (ch["label"] if ch["object_type"] == "post" else f"Comment {ch['label'].lower()}", ch["tone"])
                tally[key] = tally.get(key, 0) + 1
            item["last_change"] = r["last_change"]
            item["change_summary"] = [{"label": k[0], "tone": k[1], "n": n} for k, n in tally.items()]
            item["change_anchor"] = next((f"#o{ch['object_id']}" for ch in recent if ch["object_type"] != "post"), "")
            items.append(item)
        return feed_mod.FeedPage(items, page, len(rows) > KEPT_PAGE)

    def changes_by_day(conn: Any, page: int, everything: bool = False) -> tuple[list[dict[str, Any]], bool]:
        """The change log, a page at a time, split into days (in UTC, like the
        timestamps it is stored in). The same change to one post several times
        in a row is one line. `everything`: also what was first seen or kept."""
        rows = recent_changes(conn, limit=LOG_PAGE + 1, offset=(page - 1) * LOG_PAGE, meaningful=not everything)
        today = parse_ts(utcnow()).date()  # type: ignore[union-attr]
        days: list[dict[str, Any]] = []
        for ch in rows[:LOG_PAGE]:
            day = parse_ts(ch["at"]).date()  # type: ignore[union-attr]
            ago = (today - day).days
            label = ("Today" if ago == 0 else "Yesterday" if ago == 1 else f"{day:%A}" if ago < 7
                     else f"{day.day} {day:%B %Y}")
            if not days or days[-1]["day"] != day:
                days.append({"day": day, "label": label, "changes": []})
            last = days[-1]["changes"][-1] if days[-1]["changes"] else None
            if last and (last["thread_id"], last["label"], last["object_type"]) == \
                    (ch["thread_id"], ch["label"], ch["object_type"]):
                last["n"] += 1
                continue
            days[-1]["changes"].append(dict(ch, n=1))
        return days, len(rows) > LOG_PAGE

    def recent_changes(conn: Any, limit: int = 30, community_id: int | None = None,
                       thread_id: int | None = None, offset: int = 0,
                       meaningful: bool = False) -> list[dict[str, Any]]:
        """Changes seen, newest first. `meaningful`: leave out the ones that
        only say something was first seen or kept."""
        where, args = "", []
        if community_id:
            where, args = " AND o.community_id=?", [community_id]
        if thread_id:
            where, args = " AND o.thread_id=?", [thread_id]
        skip = " AND e.event_type NOT IN ('discovered', 'retained', 'auto_captured', 'promoted')" if meaningful else ""
        rows = conn.execute(
            f"""
            SELECT * FROM (
              SELECT e.observed_at AS at, e.event_type, e.attribution, e.metadata_json, e.reason,
                     o.id AS object_id, o.object_type, o.thread_id, NULL AS seq, th.community_id
              FROM state_events e JOIN objects o ON o.id=e.object_id
              JOIN archived_threads th ON th.id=o.thread_id AND th.trashed_at IS NULL WHERE 1=1 {where}{skip}
              UNION ALL
              SELECT r.observed_at, 'edited', NULL, '{{}}', NULL, o.id, o.object_type, o.thread_id, r.seq,
                     th.community_id
              FROM revisions r JOIN objects o ON o.id=r.object_id
              JOIN archived_threads th ON th.id=o.thread_id AND th.trashed_at IS NULL WHERE r.seq>1 {where}
            ) AS changes ORDER BY at DESC LIMIT ? OFFSET ?""",
            [*args, *args, limit, offset],
        ).fetchall()
        cids = sorted({r["community_id"] for r in rows})
        names = {c["id"]: c for c in conn.execute(
            f"SELECT id, name, canonical_ap_id FROM communities WHERE id IN ({','.join('?' * len(cids))})",
            cids)} if cids else {}
        titles: dict[int, str] = {}
        out = []
        for r in rows:
            d = dict(r)
            d["label"] = "Edited" if r["event_type"] == "edited" else event_label(r)
            d["tone"] = "warn" if r["event_type"] == "edited" else TONE.get(r["event_type"], "")
            if r["thread_id"] not in titles:
                titles[r["thread_id"]] = thread_title(conn, r["thread_id"])
            d["title"] = titles[r["thread_id"]]
            c = names.get(r["community_id"])
            d["cname"], d["c_ap"] = (c["name"], c["canonical_ap_id"]) if c else (None, None)
            out.append(d)
        return out

    def thread_title(conn: Any, thread_id: int | None) -> str:
        row = conn.execute(
            "SELECT r.title FROM archived_threads t JOIN objects o ON o.id=t.root_object_id "
            "JOIN revisions r ON r.object_id=o.id AND r.seq=o.revision_count WHERE t.id=?", (thread_id,)
        ).fetchone()
        return row["title"] if row else "(unknown thread)"

    # ---- archive / API --------------------------------------------------
    @app.post("/archive")
    async def archive(request: Request):
        is_json = request.headers.get("content-type", "").startswith("application/json")
        if is_json:
            body = await request.json()
            url = str(body.get("url", "")).strip()
        else:
            form = await request.form()
            url = str(form.get("url", "")).strip()
        from .adapters import parse_thread_url
        try:
            parse_thread_url(url)
        except ValueError as exc:
            if is_json:
                return JSONResponse({"error": str(exc)}, status_code=400)
            flash(request, f"Could not read that URL: {exc}", "error")
            return RedirectResponse(back(request, "/kept"), status_code=303)
        job_id = bouncer.enqueue("ingest", {"url": url, "retention": "manual"})
        if is_json:
            return JSONResponse({"job_id": job_id, "status": "queued"}, status_code=202)
        flash(request, "Queued for keeping. The bouncer is fetching the thread.")
        return RedirectResponse(back(request, "/kept"), status_code=303)

    @app.post("/presence")
    def presence():
        """app.js: someone is using ThreadBNC (a tab was interacted with). Subreddits
        are only checked while someone is."""
        bouncer.note_active()
        return {"ok": True}

    @app.get("/api/jobs/{job_id}")
    def job_status(job_id: int):
        with db.connect() as conn:
            j = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not j:
            raise HTTPException(404)
        return {"id": j["id"], "kind": j["kind"], "status": j["status"], "attempts": j["attempts"],
                "error": j["error"], "result": json.loads(j["result_json"] or "null")}

    # ---- communities ---------------------------------------------------
    @app.get("/communities", response_class=HTMLResponse)
    def communities(request: Request):
        with db.connect() as conn:
            follows = feed_mod.followed_communities(conn)
            others = conn.execute(
                """SELECT c.id, c.name, c.title, c.canonical_ap_id, COUNT(t.id) AS kept
                   FROM communities c JOIN archived_threads t ON t.community_id=c.id
                        AND t.trashed_at IS NULL AND t.retention='manual'
                   WHERE c.id NOT IN (SELECT community_id FROM community_follows WHERE active=1)
                   GROUP BY c.id ORDER BY c.name""").fetchall()
        return render(request, "communities.html", follows=follows, others=others, push_handle=push_handle(),
                      default_poll=settings.default_follow_poll_minutes, reddit_poll=settings.reddit_poll_minutes,
                      default_days=settings.default_follow_retention_days, reddit=bouncer.reddit.status())

    @app.post("/follow")
    def follow(request: Request, community: str = Form(...), poll_interval_minutes: str = Form(""),
               retention_days: str = Form("30"), backfill: str | None = Form(None),
               polling: str | None = Form(None)):
        """A blank check interval means the default for that kind of server
        (slower for Reddit). Lemmy and PieFed communities are only checked on a
        schedule with `polling`; otherwise their posts arrive by push."""
        days = None if retention_days.strip().lower() in ("", "forever", "none") else int(retention_days)
        every = int(poll_interval_minutes) if poll_interval_minutes.strip().isdigit() else None
        try:
            cid = bouncer.follow_community(community, max(1, every) if every else None, days, bool(backfill),
                                           polling=bool(polling))
        except (RemoteError, ValueError) as exc:
            flash(request, f"Could not follow {community}: {exc}", "error")
            return RedirectResponse(back(request, "/communities"), status_code=303)
        with db.connect() as conn:
            f = conn.execute("SELECT * FROM community_follows WHERE community_id=?", (cid,)).fetchone()
        if f["push_state"] == "subscribed":
            flash(request, f"Following. {push_handle()} subscribed, so posts arrive as they're made.")
        elif f["polling"]:
            flash(request, f"Following. It's checked every {f['poll_interval_minutes']} minutes.")
        elif f["push_state"] == "pending":
            flash(request, f"Following. {push_handle()} asked to subscribe; posts arrive once the community "
                           "accepts. If it doesn't, turn on checking it on a schedule here.", "warn-flash")
        else:
            flash(request, "Following, but nothing will arrive yet: posts from Lemmy and PieFed come through your "
                           "own server (THREADBNC_RELAY_INBOXES), which couldn't subscribe. You can have it "
                           "checked on a schedule instead, here.", "warn-flash")
        return RedirectResponse(f"/c/{cid}", status_code=303)

    @app.post("/c/{cid}/polling")
    def polling_setting(request: Request, cid: int, on: str = Form("1")):
        """Check a community on a schedule because its posts can't be pushed, or stop."""
        bouncer.set_polling(cid, on == "1")
        flash(request, "It's checked on a schedule now." if on == "1" else
              "It's no longer checked on a schedule; only posts pushed through your server arrive.")
        return RedirectResponse(back(request, f"/c/{cid}"), status_code=303)

    @app.post("/c/{cid}/follow-settings")
    def follow_settings(request: Request, cid: int, poll_interval_minutes: int = Form(...),
                        retention_days: str = Form("")):
        days = None if retention_days.strip().lower() in ("", "forever", "none") else int(retention_days)
        bouncer.update_follow(cid, max(1, poll_interval_minutes), days)
        flash(request, "Follow settings saved. Expiry dates of auto-captured threads were recalculated.")
        return RedirectResponse(back(request, f"/c/{cid}"), status_code=303)

    @app.post("/c/{cid}/unfollow")
    def unfollow(request: Request, cid: int):
        bouncer.unfollow(cid)
        flash(request, "Unfollowed. Already captured threads keep their expiry dates.")
        return RedirectResponse(back(request, f"/c/{cid}"), status_code=303)

    @app.post("/c/{cid}/push")
    def push_setting(request: Request, cid: int, on: str = Form("1")):
        """Get this community's changes pushed through your own server, or stop."""
        if federation is None:
            raise HTTPException(404)
        if on != "1":
            federation.unsubscribe(cid)
            with db.connect() as conn:
                f = conn.execute("SELECT polling FROM community_follows WHERE community_id=?", (cid,)).fetchone()
            flash(request, "Stopped pushes. " + ("It's checked on its schedule." if f and f["polling"] else
                                                 "Nothing new will arrive unless you have it checked on a schedule."))
        else:
            state = federation.subscribe(cid)
            who = push_handle()
            if state == "subscribed":
                flash(request, f"Subscribed as {who}. Changes here now arrive as they happen.")
            elif state == "pending":
                flash(request, f"Asked to subscribe as {who}. Waiting for the community to accept.")
            else:
                with db.connect() as conn:
                    row = conn.execute("SELECT push_error FROM community_follows WHERE community_id=?",
                                       (cid,)).fetchone()
                flash(request, "Couldn't subscribe" + (f": {row['push_error']}" if row and row["push_error"] else ".")
                      + " You can have it checked on a schedule instead.", "error")
        return RedirectResponse(back(request, f"/c/{cid}"), status_code=303)

    @app.post("/communities/push-all")
    def push_all(request: Request):
        if federation is None:
            raise HTTPException(404)
        federation.request_subscribe_all()
        flash(request, f"Subscribing as {push_handle()} in the background. Each community shows Pushed, "
                       "waiting to be accepted, or why it couldn't be, as it's done.")
        return RedirectResponse(back(request, "/communities"), status_code=303)

    PUSH_STATUSES = {"done": "Recorded", "skipped": "Skipped", "failed": "Failed", "pending": "Waiting",
                     "refused": "Refused"}

    @app.get("/pushes", response_class=HTMLResponse)
    def pushes(request: Request, status: str = ""):
        """What your own servers passed on: recent deliveries and what became
        of them, and which communities are subscribed."""
        if federation is None:
            raise HTTPException(404)
        status = status if status in PUSH_STATUSES else ""
        day_ago = fmt_ts(datetime.now(timezone.utc) - timedelta(days=1))
        with db.connect() as conn:
            today = {r[0]: r[1] for r in conn.execute(
                "SELECT status, COUNT(*) FROM ap_inbox WHERE received_at>=? GROUP BY status", (day_ago,))}
            waiting = conn.execute("SELECT COUNT(*) FROM ap_inbox WHERE status='pending'").fetchone()[0]
            failed = conn.execute("SELECT COUNT(*) FROM ap_inbox WHERE status='failed'").fetchone()[0]
            last = conn.execute("SELECT MAX(received_at) FROM ap_inbox").fetchone()[0]
            rows = conn.execute("SELECT * FROM ap_inbox" + (" WHERE status=?" if status else "")
                                + " ORDER BY id DESC LIMIT 100", [status] if status else []).fetchall()
            items = [{**dict(r), **summarize(r["body"])} for r in rows]
            aps = sorted({i["object"] for i in items if i["object"]} | {i["community"] for i in items if i["community"]})
            marks = ",".join("?" * len(aps))
            objects = {r["canonical_ap_id"]: r for r in conn.execute(
                f"SELECT id, canonical_ap_id, thread_id, object_type FROM objects WHERE canonical_ap_id IN ({marks})",
                aps)} if aps else {}
            communities = {r["canonical_ap_id"]: r for r in conn.execute(
                f"SELECT id, name, canonical_ap_id FROM communities WHERE canonical_ap_id IN ({marks})", aps)} if aps else {}
            follows = feed_mod.followed_communities(conn)
        for i in items:
            i["obj"] = objects.get(i["object"])
            i["comm"] = communities.get(i["community"])
        return render(request, "pushes.html", items=items, today=today, waiting=waiting, failed=failed, last=last,
                      status=status, statuses=PUSH_STATUSES, follows=follows, push_handle=push_handle(),
                      relays=settings.relay_inboxes)

    @app.post("/pushes/retry")
    def pushes_retry(request: Request):
        """Try failed deliveries again (e.g. after your server was down)."""
        if federation is None:
            raise HTTPException(404)
        with db.transaction() as conn:
            n = conn.execute("UPDATE ap_inbox SET status='pending', attempts=0, processed_at=NULL "
                             "WHERE status='failed'").rowcount
        federation.wake.set()
        flash(request, f"Trying {n} deliver{'y' if n == 1 else 'ies'} again.")
        return RedirectResponse("/pushes", status_code=303)

    @app.post("/c/{cid}/check-now")
    def check_now(request: Request, cid: int):
        """Check a community again now instead of waiting out the backoff after failures."""
        bouncer.check_follow_now(cid)
        flash(request, "Checking it now. Reload in a moment to see how it went.")
        return RedirectResponse(back(request, f"/c/{cid}"), status_code=303)

    REFRESH_WAIT_SECONDS = 10.0

    @app.post("/c/{cid}/refresh")
    def refresh_community(request: Request, cid: int):
        """Check a subreddit (or any checked community) for new posts now, and
        wait a little for it so the page comes back with them."""
        with db.connect() as conn:
            f = conn.execute("SELECT polling FROM community_follows WHERE community_id=? AND active=1",
                             (cid,)).fetchone()
            if not f or not f["polling"]:
                raise HTTPException(404)
            # Clicked twice: wait for the check already on its way rather than asking again.
            # (Matched here, not in SQL: json_extract is SQLite's, and Postgres has none.)
            pending = conn.execute("SELECT id, payload_json FROM jobs WHERE kind='poll' "
                                   "AND status IN ('queued', 'running') ORDER BY id").fetchall()
        job_id = next((j["id"] for j in pending if json.loads(j["payload_json"]).get("community_id") == cid),
                      None) or bouncer.enqueue("poll", {"community_id": cid})
        deadline = time.monotonic() + REFRESH_WAIT_SECONDS
        while True:
            with db.connect() as conn:
                j = conn.execute("SELECT status, error, result_json FROM jobs WHERE id=?", (job_id,)).fetchone()
            if j["status"] in ("done", "failed") or time.monotonic() >= deadline:
                break
            time.sleep(0.25)
        if j["status"] == "done":
            n = (json.loads(j["result_json"] or "null") or {}).get("captured", 0)
            flash(request, f"{n} new post{'' if n == 1 else 's'}." if n else "No new posts.")
        elif j["status"] == "failed":
            flash(request, f"Couldn't check it: {j['error']}", "error")
        else:
            flash(request, "Still checking. New posts will show when you reload.")
        return RedirectResponse(back(request, f"/c/{cid}"), status_code=303)

    @app.get("/c/{cid}", response_class=HTMLResponse)
    def community(request: Request, cid: int, tab: str = "feed", sort: str | None = None, t: str | None = None,
                  unread: str | None = None, page: int = 1, live_sort: str = "Hot", view: str | None = None,
                  at: str | None = None):
        save_view("", view, cid)
        as_of, snapshot = feed_as_of(request, at)
        with db.connect() as conn:
            s = feed_mod.load_defaults(conn).with_url(sort, t, unread)
        sort, t, unread = s.sort, s.window, s.unread
        with db.connect() as conn:
            c = conn.execute("SELECT c.*, i.domain FROM communities c JOIN instances i ON i.id=c.instance_id "
                             "WHERE c.id=?", (cid,)).fetchone()
            if not c:
                raise HTTPException(404)
            follow_row = conn.execute("SELECT * FROM community_follows WHERE community_id=?", (cid,)).fetchone()
            fp, shown = None, "list"
            if tab in ("feed", "kept"):
                fp = feed_mod.load_feed(conn, community_id=cid, sort=sort, window=t, unread=unread,
                                        kept_only=tab == "kept", page=page, as_of=as_of)
                shown = feed_mod.pick_view(c["view_mode"], *feed_mod.media_share(conn, cid))
            counts = {r["k"]: r["n"] for r in conn.execute(
                "SELECT CASE WHEN trashed_at IS NOT NULL THEN 'trash' ELSE retention END AS k, COUNT(*) n "
                "FROM archived_threads WHERE community_id=? GROUP BY 1", (cid,))}
            cevents = conn.execute("SELECT * FROM state_events WHERE community_id=? ORDER BY id DESC LIMIT 30",
                                   (cid,)).fetchall()
            follows = feed_mod.followed_communities(conn)
            media_data = media_mod.community_stats(conn, cid) if tab == "media" else None
        me = acting(request)
        powers = mod.powers(me, cid)
        mod_data: dict[str, Any] = {}
        if tab == "mod" and powers.any and me:
            try:
                mod_data["moderators"] = mod.moderators(me, cid)
            except AccountError as exc:
                mod_data["error"] = str(exc)
            mod_data["server_bans"], mod_data["our_bans"] = mod.community_bans(me, cid)
            mod_data["actions"] = mod.recent_actions(community_id=cid)
            mod_data["membership"] = private.overview(me, cid) if private.supported(cid) else None
        live, live_error = [], None
        if tab == "live":
            try:
                ref = bouncer.community_ref(cid)
                posts = bouncer.reader(ref.domain, cid).list_community_posts(ref, sort=live_sort, page=page,
                                                                           limit=25)
                with db.connect() as conn:
                    for p in posts:
                        th = conn.execute(
                            "SELECT t.id, t.retention, t.expires_at, t.trashed_at FROM archived_threads t "
                            "JOIN objects o ON o.id=t.root_object_id WHERE o.canonical_ap_id=?",
                            (p.ap_id,)).fetchone()
                        live.append({"post": p, "thread": th,
                                     "retain_url": p.ap_id if is_reddit_host(ref.domain)
                                     else f"https://{ref.domain}/post/{p.local_id}"})
            except RemoteError as exc:
                live_error = str(exc)
        return render(request, "community.html", c=c, follow=follow_row, tab=tab, feed=fp, counts=counts,
                      push_handle=push_handle(), pushed_poll=PUSHED_POLL_MINUTES,
                      events=cevents, live=live, live_error=live_error, sort=sort, window=t, unread=unread,
                      page=page, live_sort=live_sort, follows=follows, base_url=f"/c/{cid}",
                      community=c, powers=powers, mod_data=mod_data, view=shown, view_chosen=c["view_mode"],
                      media=media_data, media_default=bouncer.media.default_policy,
                      media_choices=media_mod.parse_choices(c["media_policy"]), media_kinds=media_mod.KINDS,
                      transcoded=media_mod.TRANSCODED, can_transcode=bouncer.media.can_transcode(),
                      snapshot=snapshot)

    def media_choices(form: Any) -> tuple[dict[str, dict[str, Any]], str | None]:
        """The choices a media settings form sent, one set for each kind of
        file ("default" and blanks follow the inherited settings), or what's wrong."""
        choices: dict[str, dict[str, Any]] = {}
        for kind, label in media_mod.KINDS.items():
            c: dict[str, Any] = {"save": {"on": True, "off": False}.get(str(form.get(f"{kind}_save", "")))}
            sizes = {}
            for field in ("keep", "target"):
                raw = str(form.get(f"{kind}_{field}", "")).strip()
                try:
                    sizes[field] = int(raw) if raw else None
                except ValueError:
                    sizes[field] = 0
                if sizes[field] is not None and not 1 <= sizes[field] <= 100_000:
                    return {}, f"{label}: sizes are a number of megabytes, or blank for the default."
            c["keep_mb"] = sizes["keep"]
            bigger = form.get(f"{kind}_bigger") if kind in media_mod.TRANSCODED else None
            if bigger == "leave":
                c["target_mb"] = 0
            elif bigger == "transcode":
                if sizes["target"] is None:
                    c["fit"] = True  # down to the size they're kept as they are up to
                elif sizes["keep"] is not None and sizes["target"] > sizes["keep"]:
                    return {}, f"{label}: transcode down to no more than the size they're kept as they are up to."
                else:
                    c["target_mb"] = sizes["target"]
            choices[kind] = {k: v for k, v in c.items() if v is not None}
        return choices, None

    @app.post("/c/{cid}/media-settings")
    async def media_settings(request: Request, cid: int):
        """What gets archived from a community, for each kind of file."""
        choices, error = media_choices(await request.form())
        if error:
            flash(request, error, "error")
            return RedirectResponse(f"/c/{cid}?tab=media", status_code=303)
        with db.transaction() as conn:
            conn.execute("UPDATE communities SET media_policy=? WHERE id=?", (media_mod.dump_choices(choices), cid))
            n = media_mod.requeue(conn, cid)
            media_mod.request_conversion(conn)
        bouncer.wake.set()
        flash(request, "Media settings saved." + (f" Trying again {n} file{'s' if n != 1 else ''} "
                                                  "the old settings left out." if n else ""))
        return RedirectResponse(f"/c/{cid}?tab=media", status_code=303)

    @app.post("/c/{cid}/media-retry")
    def media_retry(request: Request, cid: int):
        with db.transaction() as conn:
            n = media_mod.requeue(conn, cid)
            # Plus downloads that ran out of attempts (servers down, timeouts).
            n += conn.execute(
                "UPDATE media SET status='pending', attempts=0, next_attempt_at=?, error=NULL "
                f"WHERE id IN ({media_mod.community_ids_sql()}) AND status='failed'",
                (utcnow(), cid)).rowcount
        bouncer.wake.set()
        flash(request, f"Trying again {n} file{'s' if n != 1 else ''}." if n else "Nothing to try again.")
        return RedirectResponse(f"/c/{cid}?tab=media", status_code=303)

    # ---- media ----------------------------------------------------------
    media_root = (bouncer.media_dir).resolve()

    @app.get("/media/{mid}")
    def media_file(mid: int):
        with db.connect() as conn:
            m = conn.execute("SELECT * FROM media WHERE id=? AND status='ok'", (mid,)).fetchone()
        if not m:
            raise HTTPException(404)
        path = (media_root / m["storage_path"]).resolve()
        if not path.is_relative_to(media_root) or not path.is_file():
            raise HTTPException(404)
        ctype = m["content_type"] or "application/octet-stream"
        inline = ctype.startswith(("image/", "video/", "audio/")) and ctype != "image/svg+xml"
        return FileResponse(path, media_type=ctype, content_disposition_type="inline" if inline else "attachment")

    def md_for(object_ids: list[int]):
        with db.connect() as conn:
            table = media_mod.lookup_for_objects(conn, object_ids)

        def md(text: str | None) -> Markup:
            return render_markdown(text, table.get)

        def media_for(url: str | None) -> MediaInfo | None:
            return table.get(url) if url else None

        def preview(o: Any) -> MediaInfo | None:
            m = media_for(o["thumbnail_url"])
            ok = m and m.status == "ok" and (m.content_type or "").startswith("image/")
            return m if ok and m.content_type != "image/svg+xml" else None

        return md, media_for, preview

    # ---- threads --------------------------------------------------------
    def group_of(tid: int, group: str | None) -> list[int]:
        """The thread, plus its stored duplicates when the form asked for the group."""
        if not group:
            return [tid]
        with db.connect() as conn:
            row = conn.execute("SELECT o.dupe_key FROM archived_threads t JOIN objects o ON o.id=t.root_object_id "
                               "WHERE t.id=?", (tid,)).fetchone()
            copies = dupes.load_copies(conn, [row["dupe_key"]]).get(row["dupe_key"], []) if row else []
        return [tid] + [c["id"] for c in copies if c["id"] != tid]

    @app.post("/t/{tid}/keep")
    def keep(request: Request, tid: int, group: str | None = Form(None), anchor: str = Form("")):
        ids = group_of(tid, group)
        for t_id in ids:
            bouncer.promote(t_id)
        bouncer.enqueue("open", {"thread_ids": ids})  # its comments, article and videos, to keep with it
        flash(request, "Kept permanently. It won't expire." if len(ids) == 1 else
              f"Kept all {len(ids)} copies permanently. They won't expire.")
        return RedirectResponse(back(request, f"/t/{tid}", safe_anchor(anchor)), status_code=303)

    @app.post("/t/{tid}/unkeep")
    def unkeep(request: Request, tid: int, group: str | None = Form(None), anchor: str = Form("")):
        outcomes = {bouncer.unkeep(t_id) for t_id in group_of(tid, group)} - {"noop"}
        if outcomes == {"auto"}:
            flash(request, "No longer kept. It stays in your feed and expires with the community's retention.")
        elif outcomes == {"trash"}:
            flash(request, "No longer kept. Moved to the trash; you can restore it from there.")
        elif outcomes:
            flash(request, "No longer kept. Copies from communities you follow stay in your feed and expire "
                           "normally; the others moved to the trash.")
        return RedirectResponse(back(request, f"/t/{tid}", safe_anchor(anchor)), status_code=303)

    @app.post("/t/{tid}/read")
    def set_thread_read(request: Request, tid: int, read: str = Form("1"), group: str | None = Form(None),
                        anchor: str = Form("")):
        """Mark a post (and its copies when `group`) read or, with read=0, unread."""
        with db.transaction() as conn:
            feed_mod.set_read(conn, utcnow(), group_of(tid, group), read == "1")
        return RedirectResponse(back(request, f"/t/{tid}", safe_anchor(anchor)), status_code=303)

    @app.post("/t/{tid}/trash")
    def trash_thread(request: Request, tid: int, group: str | None = Form(None), anchor: str = Form("")):
        """`anchor`: the element to come back to (the next post), since this one leaves the page."""
        ids = group_of(tid, group)
        for t_id in ids:
            bouncer.move_to_trash(t_id)
        days = bouncer.trash_days()
        when = f"in {days} day{'s' if days != 1 else ''}" if days is not None else "only when you empty the trash"
        what = "Moved to trash" if len(ids) == 1 else f"Moved all {len(ids)} copies to trash"
        flash(request, f"{what}. Permanently deleted {when}.",
              undo={"action": f"/t/{tid}/restore", "fields": {"also": ids[1:]}})
        return RedirectResponse(back(request, f"/t/{tid}", safe_anchor(anchor)), status_code=303)

    @app.post("/t/{tid}/restore")
    def restore_thread(request: Request, tid: int, also: list[int] = Form([])):
        """Out of the trash; `also` restores more threads (the copies trashed with it)."""
        retentions = [r for r in (bouncer.restore_from_trash(t) for t in dict.fromkeys([tid, *also])) if r]
        if retentions == ["manual"] * len(retentions) and retentions:
            flash(request, "Restored and kept permanently.")
        elif retentions == ["auto"] * len(retentions) and retentions:
            flash(request, "Restored as auto-captured. Its original expiry date still applies."
                  if len(retentions) == 1 else "Restored as auto-captured. Their original expiry dates still apply.")
        elif retentions:
            flash(request, f"Restored {len(retentions)} copies.")
        return RedirectResponse(back(request, f"/t/{tid}"), status_code=303)

    @app.get("/t/{tid}/delete", response_class=HTMLResponse)
    def confirm_delete(request: Request, tid: int):
        with db.connect() as conn:
            t = conn.execute(
                "SELECT t.*, r.title, (SELECT COUNT(*) FROM objects x WHERE x.thread_id=t.id) AS n "
                "FROM archived_threads t JOIN objects o ON o.id=t.root_object_id "
                "JOIN revisions r ON r.object_id=o.id AND r.seq=o.revision_count WHERE t.id=?", (tid,)).fetchone()
        if not t or not t["trashed_at"]:
            raise HTTPException(404)
        return render(request, "confirm_delete.html", threads=[t], action=f"/t/{tid}/delete")

    @app.post("/t/{tid}/delete")
    def delete_thread(request: Request, tid: int, confirm: str = Form("")):
        if confirm != "delete":
            flash(request, "Type delete to confirm.", "error")
            return RedirectResponse(f"/t/{tid}/delete", status_code=303)
        n = bouncer.delete_from_trash([tid])
        flash(request, "Permanently deleted." if n else "Nothing deleted: the thread was not in the trash.")
        return RedirectResponse("/trash", status_code=303)

    def trashed_threads(conn: Any) -> list[Any]:
        return conn.execute(
            """SELECT t.*, r.title, c.name AS cname, c.canonical_ap_id AS c_ap,
                      (SELECT COUNT(*) FROM objects x WHERE x.thread_id=t.id) AS n
               FROM archived_threads t JOIN objects o ON o.id=t.root_object_id
               JOIN revisions r ON r.object_id=o.id AND r.seq=o.revision_count
               JOIN communities c ON c.id=t.community_id
               WHERE t.trashed_at IS NOT NULL ORDER BY t.trashed_at DESC""").fetchall()

    @app.get("/storage", response_class=HTMLResponse)
    def storage_page(request: Request):
        with db.connect() as conn:
            usage = storage_mod.overview(db, conn, bouncer.media_dir)
            saved = media_mod.load_defaults(conn)
        return render(request, "storage.html", u=usage, nouns=storage_mod.NOUNS, env=bouncer.media.env_policy,
                      saved=saved, media_kinds=media_mod.KINDS, transcoded=media_mod.TRANSCODED,
                      can_transcode=bouncer.media.can_transcode())

    @app.post("/storage/media-defaults")
    async def media_defaults(request: Request):
        """Media settings for communities that haven't chosen their own; "default"
        and blank choices follow the THREADBNC_MEDIA_* environment settings."""
        choices, error = media_choices(await request.form())
        if error:
            flash(request, error, "error")
            return RedirectResponse("/storage#media-defaults", status_code=303)
        with db.transaction() as conn:
            media_mod.save_defaults(conn, choices)
            n = media_mod.requeue(conn)
            media_mod.request_conversion(conn)
        bouncer.wake.set()
        flash(request, "Media defaults saved." + (f" Trying again {n} file{'s' if n != 1 else ''} "
                                                  "the old settings left out." if n else ""))
        return RedirectResponse("/storage#media-defaults", status_code=303)

    @app.get("/trash", response_class=HTMLResponse)
    def trash(request: Request):
        with db.connect() as conn:
            threads = trashed_threads(conn)
        return render(request, "trash.html", threads=threads, trash_days=bouncer.trash_days())

    @app.post("/trash/settings")
    def trash_settings(request: Request, trash_days: str = Form(...)):
        days = None if trash_days.strip().lower() in ("", "forever", "none") else max(1, int(trash_days))
        bouncer.set_trash_days(days)
        flash(request, "Trash setting saved. Deletion dates of trashed threads were recalculated.")
        return RedirectResponse("/trash", status_code=303)

    @app.get("/trash/empty", response_class=HTMLResponse)
    def confirm_empty(request: Request):
        with db.connect() as conn:
            threads = trashed_threads(conn)
        if not threads:
            return RedirectResponse("/trash", status_code=303)
        return render(request, "confirm_delete.html", threads=threads, action="/trash/empty")

    @app.post("/trash/empty")
    def empty_trash(request: Request, confirm: str = Form("")):
        if confirm != "delete":
            flash(request, "Type delete to confirm.", "error")
            return RedirectResponse("/trash/empty", status_code=303)
        n = bouncer.delete_from_trash()
        flash(request, f"Permanently deleted {n} thread{'s' if n != 1 else ''}.")
        return RedirectResponse("/trash", status_code=303)

    # ---- accounts & acting as them -----------------------------------------
    @app.get("/accounts", response_class=HTMLResponse)
    def accounts_page(request: Request, server: str = ""):
        sign_in = None
        if server.strip():
            try:
                sign_in = sso.options(server, callback_url(str(request.base_url)))
            except AccountError:
                pass
        poster.sync_reddit_account()
        return render(request, "accounts.html", server=server, sign_in=sign_in, reddit=bouncer.reddit.status())

    # ---- single sign-on -------------------------------------------------------
    def our_callback(request: Request) -> str:
        return callback_url(str(request.base_url))

    @app.get("/accounts/sign-in-options")
    def sign_in_options(request: Request, server: str = ""):
        if not server.strip():
            return {"providers": [], "ready": False, "note": None}
        try:
            return sso.options(server, our_callback(request))
        except AccountError as exc:
            return {"providers": [], "ready": False, "note": str(exc)}

    @app.get("/accounts/sso/start")
    def sso_start(request: Request, server: str, provider: int):
        try:
            return RedirectResponse(sso.start(server, provider), status_code=303)
        except AccountError as exc:
            flash(request, str(exc), "error")
            return RedirectResponse("/accounts", status_code=303)

    @app.get(CALLBACK_PATH, response_class=HTMLResponse)
    def sso_callback(request: Request, state: str = "", code: str | None = None, error: str | None = None):
        row = sso.finish(state, code, error) if state else None
        # A plain page that moves on by itself: the next request is same-site,
        # so it carries the session cookie again.
        return templates.TemplateResponse(request, "sso_return.html", {
            "next": f"/accounts/sso/{state}" if row else "/accounts",
            "message": None if row else "That sign-in link is unknown or was already used. Start again."},
            status_code=200 if row else 400)

    @app.get("/accounts/sso/go/{state}", response_class=HTMLResponse)
    def sso_go(request: Request, state: str):
        row = sso.pending(state)
        if row is None or row["status"] != "started":
            return RedirectResponse("/accounts", status_code=303)
        return render(request, "sso_go.html", url=row["authorize_url"], provider=row["provider_name"])

    @app.get("/accounts/sso/{state}", response_class=HTMLResponse)
    def sso_outcome(request: Request, state: str):
        row = sso.pending(state)
        if row is None:
            return RedirectResponse("/accounts", status_code=303)
        if row["status"] in ("need_username", "need_answer"):
            return render(request, "sso_more.html", row=row)
        if row["status"] == "done" and row["account_id"]:
            request.session["acting_account"] = row["account_id"]
        flash(request, row["message"] or "Sign-in didn't finish; start again.",
              "info" if row["status"] in ("done", "waiting") else "error")
        return RedirectResponse("/accounts", status_code=303)

    @app.post("/accounts/sso/{state}/retry")
    def sso_retry(request: Request, state: str, username: str = Form(""), answer: str = Form("")):
        try:
            new_state = sso.retry(state, username, answer)
        except AccountError as exc:
            flash(request, str(exc), "error")
            return RedirectResponse("/accounts", status_code=303)
        # Via a page, not a redirect: form-action 'self' would block a form
        # submission that ends up on the provider's site.
        return RedirectResponse(f"/accounts/sso/go/{new_state}", status_code=303)

    @app.post("/accounts")
    def add_account(request: Request, server: str = Form(...), username: str = Form(...),
                    password: str = Form(...), totp: str = Form("")):
        try:
            account = poster.add(server, username, password, totp)
        except AccountError as exc:
            flash(request, str(exc), "error")
        else:
            flash(request, f"Logged in as {account.handle}. Only an encrypted session token is stored, "
                           "not your password.")
        return RedirectResponse("/accounts", status_code=303)

    @app.post("/accounts/{aid}/default")
    def default_account(request: Request, aid: int):
        poster.set_default(aid)
        request.session.pop("acting_account", None)
        return RedirectResponse(back(request, "/accounts"), status_code=303)

    @app.post("/accounts/{aid}/remove")
    def remove_account(request: Request, aid: int):
        poster.remove(aid)
        if request.session.get("acting_account") == aid:
            request.session.pop("acting_account", None)
        flash(request, "Account removed and its session ended.")
        return RedirectResponse("/accounts", status_code=303)

    @app.post("/accounts/{aid}/refresh")
    def refresh_account(request: Request, aid: int):
        account = poster.get(aid)
        if account:
            try:
                account = poster.refresh_roles(account)
                flash(request, f"{account.handle}: {'admin' if account.is_admin else 'not an admin'} "
                               f"on {account.domain}.")
            except AccountError as exc:
                flash(request, str(exc), "error")
        return RedirectResponse("/accounts", status_code=303)

    @app.get("/communities/new", response_class=HTMLResponse)
    def new_community_form(request: Request):
        return render(request, "new_community.html")

    @app.post("/communities/new")
    def new_community(request: Request, account_id: int = Form(...), name: str = Form(...),
                      title: str = Form(...), description: str = Form(""), nsfw: str | None = Form(None),
                      mods_only: str | None = Form(None)):
        account = poster.get(account_id)
        if account is None:
            flash(request, "Pick one of your accounts.", "error")
            return RedirectResponse("/communities/new", status_code=303)
        try:
            cid = poster.create_community(account, name, title, description, bool(nsfw), bool(mods_only))
        except AccountError as exc:
            flash(request, str(exc), "error")
            return RedirectResponse("/communities/new", status_code=303)
        request.session["acting_account"] = account.id
        flash(request, f"Created !{name.strip().lower()}@{account.domain}. You're its moderator, and it's "
                       "followed here with no expiry. Use ✎ New post to start it off.")
        return RedirectResponse(f"/c/{cid}", status_code=303)

    # ---- moderation ----------------------------------------------------------
    @app.post("/c/{cid}/mods")
    def community_mods(request: Request, cid: int, who: str = Form(...), action: str = Form("add")):
        return account_action(request, f"/c/{cid}?tab=mod",
                              lambda a: mod.set_moderator(a, cid, who, action == "add"),
                              "Moderators updated." if action == "add" else "Moderator removed.")

    @app.post("/c/{cid}/bans")
    def community_bans(request: Request, cid: int, who: str = Form(...), action: str = Form("ban"),
                       reason: str = Form(""), days: str = Form(""), remove_content: str | None = Form(None)):
        ban = action == "ban"
        return account_action(request, f"/c/{cid}?tab=mod",
                              lambda a: mod.community_ban(a, cid, who, ban, reason, _days(days), bool(remove_content)),
                              "Banned from the community." if ban else "Unbanned.")

    @app.post("/c/{cid}/visibility")
    def community_visibility(request: Request, cid: int, visibility: str = Form(...)):
        return account_action(request, f"/c/{cid}?tab=mod",
                              lambda a: private.set_visibility(a, cid, visibility),
                              "Now private: only approved members can see or post." if visibility == "private"
                              else "Now public: anyone can see and join.")

    @app.post("/c/{cid}/members")
    def community_members(request: Request, cid: int, who: str = Form(...), action: str = Form("add"),
                          revoke: str | None = Form(None)):
        if action == "add":
            def add(a: Account) -> None:
                flash(request, f"{private.add_member(a, cid, who)} is on the list. If they've already asked to "
                               "join they're in; otherwise they will be as soon as they ask.")
            return account_action(request, f"/c/{cid}?tab=mod", add)

        def remove(a: Account) -> None:
            handle = private.remove_member(a, cid, who, bool(revoke))
            flash(request, f"{handle} is off the list and banned from the community, so their access is revoked."
                  if revoke else f"{handle} is off the list. They keep access if they're already a member.")
        return account_action(request, f"/c/{cid}?tab=mod", remove)

    @app.post("/c/{cid}/join-requests/{rid}")
    def join_request(request: Request, cid: int, rid: int, action: str = Form(...)):
        if action not in ("approve", "approve_add", "deny"):
            raise HTTPException(400)

        def answer(a: Account) -> None:
            handle = private.answer(a, cid, rid, approve=action != "deny", add_to_list=action == "approve_add")
            flash(request, {"approve": f"Let {handle} in.",
                            "approve_add": f"Let {handle} in and added them to the list.",
                            "deny": f"Turned {handle} away."}[action])
        return account_action(request, f"/c/{cid}?tab=mod", answer)

    @app.post("/o/{oid}/mod")
    def moderate_object(request: Request, oid: int, action: str = Form(...), reason: str = Form("")):
        actions = {
            "remove": (lambda a: mod.remove(a, oid, reason, True), "Removed. The archive keeps what it saw."),
            "restore": (lambda a: mod.remove(a, oid, reason, False), "Restored."),
            "lock": (lambda a: mod.lock(a, oid, True), "Locked."),
            "unlock": (lambda a: mod.lock(a, oid, False), "Unlocked."),
            "pin": (lambda a: mod.pin(a, oid, True), "Pinned in the community."),
            "unpin": (lambda a: mod.pin(a, oid, False), "Unpinned."),
        }
        if action not in actions:
            raise HTTPException(400)
        fn, ok = actions[action]
        return account_action(request, object_page(oid), fn, ok, anchor=f"o{oid}")

    @app.post("/o/{oid}/ban-author")
    def ban_author(request: Request, oid: int, reason: str = Form(""), days: str = Form(""),
                   remove_content: str | None = Form(None)):
        return account_action(request, object_page(oid),
                              lambda a: mod.ban_author(a, oid, reason, _days(days), bool(remove_content)),
                              "Author banned from the community.", anchor=f"o{oid}")

    # ---- admin -----------------------------------------------------------------
    def admin_account(aid: int) -> Account:
        account = poster.get(aid)
        if account is None:
            raise HTTPException(404)
        return account

    def admin_action(request: Request, aid: int, fn: Any, ok: str) -> RedirectResponse:
        try:
            fn(admin_account(aid))
            flash(request, ok)
        except AccountError as exc:
            flash(request, str(exc), "error")
        return RedirectResponse(f"/admin?account={aid}", status_code=303)

    @app.get("/admin", response_class=HTMLResponse)
    def admin_page(request: Request, account: int | None = None):
        admins = [a for a in poster.list() if a.is_admin]
        chosen = next((a for a in admins if a.id == account), admins[0] if admins else None)
        site, error, sso_info = None, None, None
        if chosen:
            try:
                site = mod.site_settings(chosen)
            except AccountError as exc:
                error = str(exc)
            if getattr(bouncer.adapter_for(chosen.domain), "supports_sso", False):
                try:
                    sso_info = sso.settings(chosen, our_callback(request))
                except AccountError as exc:
                    sso_info = {"error": str(exc)}
        return render(request, "admin.html", admins=admins, chosen=chosen, site=site, error=error, sso=sso_info,
                      our_callback=our_callback(request),
                      actions=mod.recent_actions(account_id=chosen.id) if chosen else [],
                      registration_modes=REGISTRATION_MODES)

    @app.post("/admin/{aid}/sso/providers")
    def admin_sso_add(request: Request, aid: int, name: str = Form(...), issuer: str = Form(...),
                      client_id: str = Form(...), client_secret: str = Form(...),
                      scopes: str = Form("openid email profile"), id_claim: str = Form("sub"),
                      use_pkce: str | None = Form(None)):
        return admin_action(request, aid, lambda a: sso.add_provider(
            a, name, issuer, client_id, client_secret, scopes, id_claim, bool(use_pkce)),
            f"Added {name.strip()} as a sign-in option.")

    @app.post("/admin/{aid}/sso/providers/{pid}/edit")
    def admin_sso_edit(request: Request, aid: int, pid: int, name: str = Form(...), scopes: str = Form(""),
                       id_claim: str = Form(...)):
        return admin_action(request, aid, lambda a: sso.edit_provider(a, pid, name, scopes, id_claim),
                            f"Saved. People are now identified by their {id_claim.strip()} claim.")

    @app.post("/admin/{aid}/sso/providers/{pid}")
    def admin_sso_provider(request: Request, aid: int, pid: int, action: str = Form(...)):
        return admin_action(request, aid, lambda a: sso.set_provider(a, pid, action),
                            {"enable": "Sign-in option turned on.", "disable": "Sign-in option turned off.",
                             "delete": "Sign-in option removed."}.get(action, "Done."))

    @app.post("/admin/{aid}/sso/signups")
    def admin_sso_signups(request: Request, aid: int, allowed: str = Form("0")):
        on = allowed == "1"
        return admin_action(request, aid, lambda a: sso.set_signups(a, on),
                            "New accounts can now sign up with single sign-on." if on else
                            "Single sign-on now only signs in to accounts that already exist.")

    @app.post("/admin/{aid}/registration")
    def admin_registration(request: Request, aid: int, mode: str = Form(...)):
        return admin_action(request, aid, lambda a: mod.set_registration_mode(a, mode),
                            f"Registration set to {REGISTRATION_MODES.get(mode, mode)}.")

    @app.post("/admin/{aid}/ban")
    def admin_ban(request: Request, aid: int, who: str = Form(...), action: str = Form("ban"),
                  reason: str = Form(""), days: str = Form(""), remove_content: str | None = Form(None)):
        ban = action == "ban"
        return admin_action(request, aid,
                            lambda a: mod.site_ban(a, who, ban, reason, _days(days), bool(remove_content)),
                            "Banned from the server." if ban else "Unbanned from the server.")

    @app.post("/admin/{aid}/block-instance")
    def admin_block_instance(request: Request, aid: int, domain: str = Form(...), action: str = Form("block")):
        block = action == "block"
        return admin_action(request, aid, lambda a: mod.block_instance(a, domain, block),
                            f"{'Blocked' if block else 'Unblocked'} {domain.strip()}.")

    @app.post("/admin/{aid}/block-link")
    def admin_block_link(request: Request, aid: int, domain: str = Form(...), action: str = Form("block")):
        block = action == "block"
        return admin_action(request, aid, lambda a: mod.block_link_domain(a, domain, block),
                            f"Links to {domain.strip()} {'blocked' if block else 'allowed again'}.")

    @app.post("/accounts/act-as")
    def act_as(request: Request, account_id: int = Form(...)):
        chosen = poster.get(account_id)
        if chosen and not chosen.is_reddit:
            request.session["acting_account"] = account_id
        return RedirectResponse(back(request, "/"), status_code=303)

    def account_action(request: Request, fallback: str, fn: Any, ok: str | None = None,
                       anchor: str | None = None) -> RedirectResponse:
        """Run a write as the acting account; show errors instead of raising."""
        try:
            result = fn(require_acting(request))
        except AccountError as exc:
            flash(request, str(exc), "error")
            return RedirectResponse(back(request, fallback, anchor), status_code=303)
        if ok:
            flash(request, ok)
        return result if isinstance(result, RedirectResponse) else RedirectResponse(
            back(request, fallback, anchor), status_code=303)

    @app.post("/t/{tid}/reply")
    def reply(request: Request, tid: int, body: str = Form(...), parent_id: int | None = Form(None),
              to: list[int] = Form([]), choose: str | None = Form(None)):
        """Comment on the thread (or reply to parent_id). With `choose`, `to` lists
        the posts and comments to answer instead (a thread shown with its
        duplicates): one comment is made under each, on its own thread."""
        targets = list(dict.fromkeys(to)) if choose else [parent_id or 0]
        with db.connect() as conn:
            where: dict[int, tuple[int, int | None]] = {}
            for oid in targets:
                if not oid:
                    where[oid] = (tid, None)
                    continue
                row = conn.execute("SELECT thread_id, object_type, canonical_ap_id FROM objects WHERE id=?",
                                   (oid,)).fetchone()
                skip = choose and is_reddit(row["canonical_ap_id"]) and not poster.reddit_account() if row else False
                if row and row["thread_id"] and not skip:
                    where[oid] = (row["thread_id"], None if row["object_type"] == "post" else oid)
        if not where:
            flash(request, "Pick at least one post to comment on.", "error")
            return RedirectResponse(back(request, f"/t/{tid}"), status_code=303)

        def act(account: Account) -> RedirectResponse:
            made, failed = [], []
            for thread_id, parent in where.values():
                try:
                    made.append(poster.reply(account, thread_id, body, parent))
                except AccountError as exc:
                    if len(where) == 1:
                        raise
                    failed.append(f"{thread_title_of(thread_id)}: {exc}")
            if failed:
                flash(request, f"Posted {len(made)} of {len(where)}. Failed: " + "; ".join(failed), "error")
            elif len(made) > 1:
                flash(request, f"Comment posted to {len(made)} copies.")
            else:
                flash(request, "Comment posted.")
            if not made:
                return RedirectResponse(back(request, f"/t/{tid}"), status_code=303)
            return RedirectResponse(f"/t/{tid}#o{made[0]}", status_code=303)
        return account_action(request, f"/t/{tid}", act)

    def thread_title_of(thread_id: int) -> str:
        with db.connect() as conn:
            t = conn.execute("SELECT c.name, c.canonical_ap_id FROM archived_threads t "
                             "JOIN communities c ON c.id=t.community_id WHERE t.id=?", (thread_id,)).fetchone()
        return f"!{t['name']}@{host_of(t['canonical_ap_id'])}" if t else f"thread {thread_id}"

    @app.post("/o/{oid}/vote")
    def vote(request: Request, oid: int, score: int = Form(...), also: list[int] = Form([])):
        """Vote on an object, and on its duplicates (`also`) when shown squashed."""
        ids = list(dict.fromkeys([oid, *also]))
        if len(ids) > 1 and not poster.reddit_account():  # not logged in with Reddit: vote on the other copies
            with db.connect() as conn:
                rows = conn.execute(f"SELECT id, canonical_ap_id FROM objects WHERE id IN ({','.join('?' * len(ids))})",
                                    ids).fetchall()
            reddit_ids = {r["id"] for r in rows if is_reddit(r["canonical_ap_id"])}
            ids = [i for i in ids if i not in reddit_ids] or ids
            oid = ids[0]
        if len(ids) == 1:
            return account_action(request, object_page(oid), lambda a: poster.vote(a, oid, score),
                                  anchor=f"o{oid}")

        def act(account: Account) -> None:
            errors = []
            for i in ids:
                try:
                    poster.vote(account, i, score)
                except AccountError as exc:
                    errors.append(str(exc))
            if len(errors) == len(ids):
                raise AccountError(errors[0])
            if errors:
                flash(request, f"Voted on {len(ids) - len(errors)} of {len(ids)} copies. " + "; ".join(errors),
                      "error")
        return account_action(request, object_page(oid), act, anchor=f"o{oid}")

    @app.get("/o/{oid}/edit", response_class=HTMLResponse)
    def edit_form(request: Request, oid: int):
        with db.connect() as conn:
            o = conn.execute("SELECT o.*, r.title, r.body, r.url FROM objects o JOIN revisions r "
                             "ON r.object_id=o.id AND r.seq=o.revision_count WHERE o.id=?", (oid,)).fetchone()
        if not o:
            raise HTTPException(404)
        return render(request, "edit.html", o=o)

    @app.post("/o/{oid}/edit")
    def edit(request: Request, oid: int, body: str = Form(""), title: str | None = Form(None),
             url: str | None = Form(None)):
        with db.connect() as conn:
            tid = conn.execute("SELECT thread_id FROM objects WHERE id=?", (oid,)).fetchone()
        def act(account: Account) -> RedirectResponse:
            poster.edit(account, oid, body, title, url)
            return RedirectResponse(f"/t/{tid[0]}#o{oid}" if tid else "/", status_code=303)
        return account_action(request, f"/o/{oid}/edit", act, "Edited.")

    @app.post("/o/{oid}/delete")
    def delete_own(request: Request, oid: int, restore: str | None = Form(None)):
        undo = bool(restore)
        return account_action(request, object_page(oid), lambda a: poster.delete(a, oid, deleted=not undo),
                              "Restored on the server." if undo else
                              "Deleted on the server. The archive keeps what it saw.", anchor=f"o{oid}")

    @app.get("/c/{cid}/submit", response_class=HTMLResponse)
    def submit_form(request: Request, cid: int):
        with db.connect() as conn:
            c = conn.execute("SELECT * FROM communities WHERE id=?", (cid,)).fetchone()
        if not c:
            raise HTTPException(404)
        return render(request, "submit.html", c=c)

    @app.post("/c/{cid}/submit")
    def submit(request: Request, cid: int, title: str = Form(...), url: str = Form(""), body: str = Form("")):
        def act(account: Account) -> RedirectResponse:
            tid = poster.submit(account, cid, title, body, url)
            return RedirectResponse(f"/t/{tid}", status_code=303)
        return account_action(request, f"/c/{cid}/submit", act, "Posted. It's kept automatically.")

    # ---- reposting -----------------------------------------------------------
    def repost_targets(account: Account | None, reddit_me: Account | None) -> list[dict[str, Any]]:
        """Communities a post can be reposted to, grouped: those on the acting
        account's own server, followed ones, followed subreddits (when logged
        in with Reddit), then the rest."""
        with db.connect() as conn:
            rows = conn.execute(
                "SELECT c.id, c.name, c.canonical_ap_id, "
                "EXISTS(SELECT 1 FROM community_follows f WHERE f.community_id=c.id AND f.active=1) AS followed "
                "FROM communities c ORDER BY c.name").fetchall()
        home = account.domain if account else None
        out = []
        for r in rows:
            c = dict(r, host=host_of(r["canonical_ap_id"]), label=chandle(r["name"], r["canonical_ap_id"], True))
            if is_rss(r["canonical_ap_id"]):
                continue
            if is_reddit(r["canonical_ap_id"]):
                if not reddit_me or not c["followed"]:
                    continue
                c["rank"], c["group"] = 2, f"Subreddits (as {reddit_me.handle})"
            elif account is None:
                continue
            elif c["host"] == home:
                c["rank"], c["group"] = 0, f"On {home}"
            else:
                c["rank"], c["group"] = (1, "Following") if c["followed"] else (3, "Other communities")
            out.append(c)
        out.sort(key=lambda c: (c["rank"], c["name"].lower(), c["host"]))
        return out

    def picked_community(community_id: str, community: str) -> int:
        """The community a post form chose: from its list, or typed as
        !name@host (looked up, not followed), which wins."""
        cid = int(community_id) if community_id.strip().isdigit() else None
        if community.strip():
            try:
                _ref, found = bouncer.resolve_community(community)
            except (RemoteError, ValueError) as exc:
                raise AccountError(f"Couldn't find {community.strip()}: {exc}") from exc
            with db.transaction() as conn:
                cid = store.upsert_community(conn, found, utcnow())
        if not cid:
            raise AccountError("Pick a community to post it in.")
        return cid

    @app.get("/t/{tid}/repost", response_class=HTMLResponse)
    def repost_form(request: Request, tid: int):
        with db.connect() as conn:
            t = conn.execute(THREAD_SQL, (tid,)).fetchone()
        if not t:
            raise HTTPException(404)
        try:
            draft = poster.repost_draft(tid)
        except AccountError as exc:
            flash(request, str(exc), "error")
            return RedirectResponse(f"/t/{tid}", status_code=303)
        me = acting(request)
        targets = repost_targets(me, poster.reddit_account())
        last = request.session.get("repost_to")
        chosen = last if any(c["id"] == last for c in targets) else (targets[0]["id"] if targets else None)
        return render(request, "repost.html", t=t, draft=draft, targets=targets, chosen=chosen,
                      action=f"/t/{tid}/repost")

    @app.post("/t/{tid}/repost")
    def repost(request: Request, tid: int, title: str = Form(...), url: str = Form(""), body: str = Form(""),
               community_id: str = Form(""), community: str = Form("")):
        """Post a copy in one of your communities: picked from the list, or
        typed as !name@host (looked up, not followed)."""
        def act(account: Account) -> RedirectResponse:
            cid = picked_community(community_id, community)
            new_tid = poster.repost(account, tid, cid, title, body, url)
            request.session["repost_to"] = cid
            return RedirectResponse(f"/t/{new_tid}", status_code=303)
        return account_action(request, f"/t/{tid}/repost", act, "Reposted. It's kept automatically, and shown "
                                                               "together with the original.")

    # ---- YouTube -------------------------------------------------------------
    @app.get("/youtube", response_class=HTMLResponse)
    def youtube_page(request: Request):
        with db.connect() as conn:
            counts = {r["status"]: r["n"] for r in conn.execute(
                f"SELECT status, COUNT(*) AS n FROM media WHERE kept_only=1 AND (status!='pending' OR "
                f"{media_mod.KEPT_SQL}) GROUP BY status")}
            channels = conn.execute(
                "SELECT c.id, c.name FROM communities c JOIN community_follows f ON f.community_id=c.id "
                "WHERE f.active=1 AND c.canonical_ap_id LIKE ? ORDER BY lower(c.name)",
                (youtube.FEED_PREFIX + "%",)).fetchall()
        return render(request, "youtube.html", yt=bouncer.youtube.status(), counts=counts, channels=channels,
                      heights=youtube.HEIGHTS)

    @app.post("/youtube/session")
    def youtube_session(request: Request, cookies: str = Form(""), po_token: str = Form("")):
        try:
            bouncer.youtube.save_session(cookies, po_token)
        except youtube.YouTubeError as exc:
            flash(request, str(exc), "error")
            return RedirectResponse("/youtube", status_code=303)
        with db.transaction() as conn:
            retried = media_mod.retry_youtube(conn)
        bouncer.wake.set()
        flash(request, "Saved. It's used only to download the videos in posts you keep"
                       + (f"; trying {retried} that failed again." if retried else "."))
        return RedirectResponse("/youtube", status_code=303)

    @app.post("/youtube/forget")
    def youtube_forget(request: Request):
        bouncer.youtube.forget()
        flash(request, "Forgot your YouTube session. Videos already saved stay.")
        return RedirectResponse("/youtube", status_code=303)

    @app.post("/youtube/limits")
    def youtube_limits(request: Request, max_mb: int = Form(youtube.DEFAULT_MAX_MB),
                       max_height: int = Form(youtube.DEFAULT_MAX_HEIGHT)):
        bouncer.youtube.save_limits(max_mb, max_height)
        flash(request, "Saved. Videos already downloaded aren't changed.")
        return RedirectResponse("/youtube", status_code=303)

    # ---- Reddit --------------------------------------------------------------
    @app.get("/reddit", response_class=HTMLResponse)
    def reddit_page(request: Request, subs: int = 0):
        status = bouncer.reddit.status()
        subscriptions, subs_error = None, None
        if subs and status and status["has_account"]:
            try:
                subscriptions = bouncer.reddit_adapter.subscriptions()
            except RemoteError as exc:
                subs_error = str(exc)
        followed: set[str] = set()
        if subscriptions:
            with db.connect() as conn:
                followed = {r["canonical_ap_id"].lower() for r in conn.execute(
                    "SELECT c.canonical_ap_id FROM communities c JOIN community_follows f ON f.community_id=c.id "
                    "WHERE f.active=1")}
        return render(request, "reddit.html", reddit=status, callback=reddit_callback_url(str(request.base_url)),
                      subscriptions=subscriptions, subs_error=subs_error, followed=followed,
                      reddit_poll=settings.reddit_poll_minutes, reddit_interval=settings.reddit_min_request_interval,
                      default_days=settings.default_follow_retention_days)

    @app.post("/reddit/app")
    def reddit_app(request: Request, client_id: str = Form(...), client_secret: str = Form("")):
        try:
            bouncer.reddit.connect_app(client_id, client_secret)
            poster.sync_reddit_account()
            flash(request, "Connected to Reddit with your app (no Reddit account). Follow subreddits as r/name.")
        except RedditError as exc:
            flash(request, str(exc), "error")
        return RedirectResponse("/reddit", status_code=303)

    @app.post("/reddit/cookie")
    def reddit_cookie(request: Request, cookie: str = Form(...)):
        try:
            cfg = bouncer.reddit.connect_cookie(cookie)
            poster.sync_reddit_account()
            flash(request, f"Connected to Reddit as u/{cfg['username']} with your browser's cookie. Logging out of "
                           "Reddit in that browser ends it; then paste a fresh one here.")
        except RedditError as exc:
            flash(request, str(exc), "error")
        return RedirectResponse("/reddit", status_code=303)

    @app.post("/reddit/login")
    def reddit_login(request: Request, client_id: str = Form(...), client_secret: str = Form("")):
        try:
            bouncer.reddit.start_login(client_id, client_secret, reddit_callback_url(str(request.base_url)))
        except RedditError as exc:
            flash(request, str(exc), "error")
            return RedirectResponse("/reddit", status_code=303)
        # Via a page, not a redirect: form-action 'self' would block a form
        # submission that ends up on reddit.com.
        return RedirectResponse("/reddit/go", status_code=303)

    @app.get("/reddit/go", response_class=HTMLResponse)
    def reddit_go(request: Request):
        pending = bouncer.reddit.pending_url()
        if not pending:
            return RedirectResponse("/reddit", status_code=303)
        return render(request, "sso_go.html", url=pending, provider="Reddit", heading="Log in with Reddit")

    @app.get(REDDIT_CALLBACK_PATH, response_class=HTMLResponse)
    def reddit_callback(request: Request, state: str = "", code: str | None = None, error: str | None = None):
        # Reached from reddit.com, so the (SameSite=strict) session cookie isn't
        # sent: authorised by the single-use state instead, like single sign-on.
        ok, message = bouncer.reddit.finish_login(state, code, error)
        if ok:
            poster.sync_reddit_account()
        return templates.TemplateResponse(request, "sso_return.html", {
            "next": "/reddit", "message": None if ok else message, "heading": "Reddit"},
            status_code=200 if ok else 400)

    @app.post("/reddit/disconnect")
    def reddit_disconnect(request: Request):
        bouncer.reddit.disconnect()
        poster.sync_reddit_account()
        flash(request, "Disconnected from Reddit. Followed subreddits stay, but aren't checked until you connect "
                       "again; everything already saved stays.")
        return RedirectResponse("/reddit", status_code=303)

    @app.post("/reddit/follow")
    def reddit_follow(request: Request, names: list[str] = Form([]), retention_days: str = Form("30")):
        days = None if retention_days.strip().lower() in ("", "forever", "none") else int(retention_days)
        done, failed = 0, []
        for name in dict.fromkeys(n.strip() for n in names if n.strip()):
            try:
                bouncer.follow_community(f"r/{name}", None, days, backfill=True)
                done += 1
            except (RemoteError, ValueError) as exc:
                failed.append(f"r/{name}: {exc}")
        if done:
            flash(request, f"Following {done} subreddit{'s' if done != 1 else ''}, each checked every "
                           f"{settings.reddit_poll_minutes} minutes while you're using ThreadBNC.")
        if failed:
            flash(request, "Couldn't follow " + "; ".join(failed), "error")
        return RedirectResponse("/reddit?subs=1", status_code=303)

    @app.post("/t/{tid}/sync")
    def sync_now(request: Request, tid: int):
        bouncer.enqueue("sync", {"thread_id": tid})
        flash(request, "Re-check queued.")
        return RedirectResponse(f"/t/{tid}", status_code=303)

    THREAD_SQL = ("SELECT t.*, c.name AS cname, c.canonical_ap_id AS c_ap, c.id AS cid, "
                  "f.retention_days FROM archived_threads t JOIN communities c ON c.id=t.community_id "
                  "LEFT JOIN community_follows f ON f.community_id=c.id WHERE t.id=?")

    @app.get("/t/{tid}", response_class=HTMLResponse)
    def thread(request: Request, tid: int, sort: str | None = None, merge: int = 1, refreshed: int = 0):
        """A thread, shown together with its stored duplicates (the same link or
        text posted elsewhere) unless merge=0: one post listing every copy,
        every copy's comments in one tree, repeated comments squashed.

        Opening it asks the bouncer to re-read the comments (and save the
        linked article) unless that was done in the last few minutes; the page
        shows the stored copy at once and thread.js swaps in the fresh one.
        `refreshed`: that fresh copy, which doesn't count as another visit."""
        if sort in COMMENT_SORTS:
            request.session["comment_sort"] = sort
        sort = request.session.get("comment_sort", "hot") if sort not in COMMENT_SORTS else sort
        with db.connect() as conn:
            t = conn.execute(THREAD_SQL, (tid,)).fetchone()
            if not t:
                raise HTTPException(404)
            key_row = conn.execute("SELECT dupe_key FROM objects WHERE id=?", (t["root_object_id"],)).fetchone()
            key = key_row["dupe_key"] if key_row else None
            copies = dupes.load_copies(conn, [key]).get(key, []) if key else []
            others = [c["id"] for c in copies if c["id"] != tid] if merge else []
            threads = {tid: t, **{i: conn.execute(THREAD_SQL, (i,)).fetchone() for i in others}}
            tids = list(threads)
            marks = ",".join("?" * len(tids))
            objs = conn.execute(
                f"""SELECT o.*, a.username, a.instance AS a_instance, a.display_name, a.canonical_ap_id AS a_ap,
                          r.title, r.body, r.url, r.metadata_json AS rmeta
                   FROM objects o LEFT JOIN actors a ON a.id=o.author_id
                   JOIN revisions r ON r.object_id=o.id AND r.seq=o.revision_count
                   WHERE o.thread_id IN ({marks}) ORDER BY o.created_at, o.id""", tids).fetchall()
            events: dict[int, list[Any]] = {}
            for e in conn.execute(
                "SELECT e.*, a.username AS actor_name, a.instance AS actor_instance FROM state_events e "
                "LEFT JOIN actors a ON a.id=e.actor_id JOIN objects o ON o.id=e.object_id "
                f"WHERE o.thread_id IN ({marks}) AND e.event_type!='discovered' ORDER BY e.observed_at", tids):
                events.setdefault(e["object_id"], []).append(e)
            instance = conn.execute("SELECT * FROM instances WHERE domain=?", (t["source_domain"],)).fetchone()
            post = next((o for o in objs if o["thread_id"] == tid and o["object_type"] == "post"), None)
            article = articles.for_object(conn, post["id"], post["url"]) if post else None
            seen_key = "prev_viewed_at" if refreshed else "last_viewed_at"  # this visit already moved it along
            seen_at = {i: th[seen_key] for i, th in threads.items()}
            if not refreshed:
                # Every copy's comments are on this page, so they all count as read.
                now = utcnow()
                conn.execute(f"UPDATE archived_threads SET prev_viewed_at=last_viewed_at, last_viewed_at=?, "
                             f"opened_at=? WHERE id IN ({marks})", [now, now, *tids])
        since = t[seen_key]
        refresh_job = None
        if not refreshed:
            stale = [i for i, th in threads.items() if not th["trashed_at"] and (
                bouncer.comments_stale(th) or (i == tid and article is not None and article["status"] == "pending"))]
            if stale:
                refresh_job = bouncer.enqueue("open", {"thread_ids": stale})
        srcs = {i: {"id": i, "cname": th["cname"], "c_ap": th["c_ap"], "server": th["source_domain"]}
                for i, th in threads.items()}
        nodes: dict[int, dict[str, Any]] = {}
        for o in objs:
            seen = seen_at.get(o["thread_id"])
            n = {"o": o, "children": [], "events": events.get(o["id"], []), "src": srcs[o["thread_id"]],
                 "is_new": bool(seen and o["discovered_late"] and o["first_seen_at"] > seen),
                 "is_changed": bool(seen and o["last_changed_at"] and o["last_changed_at"] > seen)}
            n["members"] = [n]
            nodes[o["id"]] = n
        roots: dict[int, dict[str, Any]] = {}
        orphans: dict[int, list[dict[str, Any]]] = {}
        for n in nodes.values():
            o = n["o"]
            if o["object_type"] == "post":
                roots[o["thread_id"]] = n
            elif o["parent_id"] in nodes:
                nodes[o["parent_id"]]["children"].append(n)
            else:
                orphans.setdefault(o["thread_id"], []).append(n)
        root = roots.get(tid)

        def finish(n: dict[str, Any]) -> int:
            """Combined votes, where it's from and the vote breakdown; returns descendants."""
            members = [m["o"] for m in n["members"]]
            n["agg"] = {**dupes.sum_votes(members), "created_at": min(m["created_at"] or "" for m in members)}
            n["sources"] = list({m["src"]["id"]: m["src"] for m in n["members"]}.values())
            n["breakdown"] = [dupes.breakdown_row(m["src"]["cname"], m["src"]["c_ap"], m["src"]["server"], m["o"])
                              for m in n["members"]]
            n["descendants"] = sum(1 + finish(c) for c in n["children"])
            return n["descendants"]

        displayed: list[dict[str, Any]] = []
        if root is not None:
            # Every copy's comments hang off the post being viewed.
            root["children"] = squash_comments([c for i in tids if i in roots
                                                for c in roots[i]["children"] + orphans.get(i, [])])
            root["members"] = [roots[i] for i in tids if i in roots]
            finish(root)
            sort_tree(root, sort)
            stack = list(root["children"])
            while stack:
                n = stack.pop()
                displayed.append(n)
                stack.extend(n["children"])
        raw_comments = sum(1 for o in objs if o["object_type"] == "comment")
        new_count = sum(1 for n in displayed if n["is_new"])
        changed_count = sum(1 for n in displayed if n["is_changed"] and not n["is_new"])
        md, media_for, preview = md_for(list(nodes))
        me = acting(request)
        ap_ids = [o["canonical_ap_id"] for o in objs]
        my_votes = {**poster.my_votes(me, ap_ids), **poster.my_votes(poster.reddit_account(), ap_ids)}
        powers = {th["cid"]: mod.powers(me, th["cid"]) for th in threads.values()}
        counts = {c["id"]: c["n_comments"] for c in copies}
        post_copies = [{"t": threads[i], "o": roots[i]["o"], "n_comments": counts.get(i, raw_comments),
                        "reddit": is_reddit(roots[i]["o"]["canonical_ap_id"])} for i in tids if i in roots]
        return render(request, "thread.html", t=t, root=root, total_comments=len(displayed),
                      raw_comments=raw_comments, md=md, media_for=media_for, preview=preview, my_votes=my_votes,
                      me=me, powers_by_community=powers, new_count=new_count, changed_count=changed_count,
                      comment_sort=sort, comment_sorts=COMMENT_SORTS, instance=instance, since=since,
                      grouped=len(tids) > 1, post_copies=post_copies, merge=merge, article=article,
                      n_copies=max(len(copies), 1), refresh_job=refresh_job,
                      all_kept=all(th["retention"] == "manual" for th in threads.values()))

    def read_href(url: str) -> str:
        """Where a link to an article goes to be read here."""
        return "/read?url=" + quote(url, safe="")

    def article_view(request: Request, a: Any, t: Any = None, o: Any = None, pane: bool = False) -> HTMLResponse:
        """An article in the reader: a post's (t, o) or one opened from a link.
        Links in it to other articles are read here too. Kept, it lists the
        articles here that link to it. `pane`: just the article, for app.js to
        open beside (or inside) the one you're reading."""
        kept = bool(a["kept_at"] or (t and t["retention"] == "manual"))
        with db.connect() as conn:
            table = {**(media_mod.lookup_for_objects(conn, [o["id"]]) if o else {}),
                     **articles.media_lookup(conn, a["id"])}
            discussed = conn.execute(  # posts that link here, besides this one
                "SELECT t.id, r.title, c.name, c.canonical_ap_id FROM article_refs ar "
                "JOIN archived_threads t ON t.root_object_id=ar.object_id AND t.trashed_at IS NULL "
                "JOIN objects o ON o.id=t.root_object_id JOIN revisions r ON r.object_id=o.id AND r.seq=o.revision_count "
                "JOIN communities c ON c.id=t.community_id WHERE ar.article_id=? AND t.id!=? ORDER BY t.id DESC LIMIT 10",
                (a["id"], t["id"] if t else 0)).fetchall()
            mentions = articles.mentioned_by(conn, a) if kept else []
        lead = table.get(a["lead_image_url"] or "")
        shown = lead and lead.status == "ok" and (lead.content_type or "").startswith("image/") \
            and lead.content_type != "image/svg+xml"
        content = articles.render(a["content_html"], table.get, a["fetched_from"] or a["url"], read_href) \
            if a["status"] == "ok" else None
        return render(request, "article_pane.html" if pane else "article.html", t=t, o=o, a=a, content=content,
                      lead=lead if shown else None, discussed=discussed, kept=kept, mentions=mentions, pane=pane)

    @app.get("/t/{tid}/article", response_class=HTMLResponse)
    def article_page(request: Request, tid: int):
        """The article the post links to, as the bouncer read it."""
        with db.connect() as conn:
            t = conn.execute(THREAD_SQL, (tid,)).fetchone()
            if not t:
                raise HTTPException(404)
            o = conn.execute("SELECT o.*, r.title, r.url FROM objects o JOIN revisions r ON r.object_id=o.id "
                             "AND r.seq=o.revision_count WHERE o.id=?", (t["root_object_id"],)).fetchone()
            a = articles.for_object(conn, o["id"], o["url"]) if o else None
        if not a or a["status"] != "ok":
            raise HTTPException(404)
        return article_view(request, a, t, o)

    @app.get("/read")
    def read_link(url: str = "", pane: bool = False):
        """A link to an article, read here: fetched the first time (you're
        waiting for it, so now rather than in the background), then shown
        (`pane`: just the article, see article_view)."""
        if not safe_url(url):
            raise HTTPException(400, "Not a web address")
        if not articles.candidate(url):  # a picture, a video, a social site: the page itself
            return RedirectResponse(url, status_code=303)
        with db.transaction() as conn:
            a = articles.ensure(conn, url, utcnow())
        if a["status"] == "pending":
            bouncer.articles.fetch_one(a)
        return RedirectResponse(f"/a/{a['id']}" + ("?pane=1" if pane else ""), status_code=303)

    @app.get("/a/{aid}", response_class=HTMLResponse)
    def article_by_id(request: Request, aid: int, pane: bool = False):
        """An article opened from a link (or any saved article): read here, or
        why it couldn't be."""
        with db.connect() as conn:
            a = conn.execute("SELECT * FROM articles WHERE id=?", (aid,)).fetchone()
        if a is None:
            raise HTTPException(404)
        return article_view(request, a, pane=pane)

    @app.post("/a/{aid}/retry")
    def article_try_again(request: Request, aid: int):
        with db.transaction() as conn:
            conn.execute("UPDATE articles SET status='pending', attempts=0, next_attempt_at=?, error=NULL "
                         "WHERE id=? AND status!='ok'", (utcnow(), aid))
            a = conn.execute("SELECT * FROM articles WHERE id=?", (aid,)).fetchone()
        if a is not None and a["status"] == "pending":
            bouncer.articles.fetch_one(a)
        return RedirectResponse(back(request, f"/a/{aid}"), status_code=303)  # the page it's open on

    @app.post("/a/{aid}/keep")
    @app.post("/a/{aid}/unkeep")
    def article_keep(request: Request, aid: int):
        kept = request.url.path.endswith("/keep")
        with db.transaction() as conn:
            articles.keep(conn, aid, kept, utcnow())
        flash(request, "Kept. It stays until you unkeep it; find it on the Kept page." if kept else
              "No longer kept. It goes after a month without being opened, unless a post links to it.")
        return RedirectResponse(back(request, f"/a/{aid}"), status_code=303)

    def article_draft(a: Any) -> dict[str, str]:
        excerpt = articles.excerpt(a["content_html"])
        quoted = "\n".join(f"> {line}" if line.strip() else ">" for line in excerpt.splitlines())
        return {"title": (a["title"] or "")[:200], "url": a["url"], "body": quoted, "source": a["url"]}

    @app.get("/a/{aid}/post", response_class=HTMLResponse)
    def article_post_form(request: Request, aid: int):
        with db.connect() as conn:
            a = conn.execute("SELECT * FROM articles WHERE id=? AND status='ok'", (aid,)).fetchone()
        if a is None:
            raise HTTPException(404)
        targets = repost_targets(acting(request), poster.reddit_account())
        last = request.session.get("repost_to")
        chosen = last if any(c["id"] == last for c in targets) else (targets[0]["id"] if targets else None)
        return render(request, "article_post.html", a=a, draft=article_draft(a), targets=targets, chosen=chosen,
                      action=f"/a/{aid}/post")

    @app.post("/a/{aid}/post")
    def article_post(request: Request, aid: int, title: str = Form(...), url: str = Form(""), body: str = Form(""),
                     community_id: str = Form(""), community: str = Form("")):
        """Share an article read here as a link post in one of your communities."""
        def act(account: Account) -> RedirectResponse:
            cid = picked_community(community_id, community)
            tid = poster.submit(account, cid, title, body, url)
            request.session["repost_to"] = cid
            return RedirectResponse(f"/t/{tid}", status_code=303)
        return account_action(request, f"/a/{aid}/post", act, "Posted. It's kept automatically.")

    @app.post("/t/{tid}/article/retry")
    def article_retry(request: Request, tid: int):
        with db.transaction() as conn:
            t = conn.execute("SELECT root_object_id FROM archived_threads WHERE id=?", (tid,)).fetchone()
            o = conn.execute("SELECT r.url FROM objects o JOIN revisions r ON r.object_id=o.id "
                             "AND r.seq=o.revision_count WHERE o.id=?", (t["root_object_id"],)).fetchone() if t else None
            a = articles.for_object(conn, t["root_object_id"], o["url"]) if o else None
            queued = bool(a) and articles.retry(conn, a["id"])
        if queued:
            bouncer.wake.set()
            flash(request, "The article will be read again shortly.")
        return RedirectResponse(f"/t/{tid}", status_code=303)

    @app.get("/o/{oid}/history", response_class=HTMLResponse)
    def history(request: Request, oid: int):
        with db.connect() as conn:
            o = conn.execute("SELECT o.*, a.username, a.instance AS a_instance FROM objects o "
                             "LEFT JOIN actors a ON a.id=o.author_id WHERE o.id=?", (oid,)).fetchone()
            if not o:
                raise HTTPException(404)
            revs = conn.execute("SELECT * FROM revisions WHERE object_id=? ORDER BY seq", (oid,)).fetchall()
            evs = conn.execute("SELECT e.*, a.username AS actor_name, a.instance AS actor_instance "
                               "FROM state_events e LEFT JOIN actors a ON a.id=e.actor_id "
                               "WHERE e.object_id=? ORDER BY e.observed_at", (oid,)).fetchall()
        timeline: list[dict[str, Any]] = []
        if o["created_at"]:
            timeline.append({"at": o["created_at"], "label": "Created (remote timestamp)", "tone": ""})
        timeline.append({"at": o["first_seen_at"], "label": "First observed by the bouncer", "tone": "muted"})
        for r in revs[1:]:
            timeline.append({"at": r["observed_at"], "label": f"Edited → version {r['seq']}", "tone": "warn",
                             "remote": r["remote_updated_at"]})
        for e in evs:
            if e["event_type"] == "discovered":
                continue
            timeline.append({"at": e["observed_at"], "label": event_label(e), "tone": TONE.get(e["event_type"], ""),
                             "reason": e["reason"], "remote": e["remote_timestamp"],
                             "actor": f"{e['actor_name']}@{e['actor_instance']}" if e["actor_name"] else None,
                             "meta": json.loads(e["metadata_json"] or "{}")})
        timeline.sort(key=lambda x: x["at"] or "")
        versions = []
        for i, r in enumerate(revs):
            prev = revs[i - 1] if i else None
            versions.append({
                "r": r, "meta": json.loads(r["metadata_json"] or "{}"),
                "title_diff": word_diff(prev["title"], r["title"]) if prev and prev["title"] != r["title"] else None,
                "body_diff": word_diff(prev["body"], r["body"]) if prev and prev["body"] != r["body"] else None,
                "url_changed": bool(prev and prev["url"] != r["url"]),
            })
        md, media_for, _preview = md_for([oid])
        return render(request, "history.html", o=o, timeline=timeline, versions=list(reversed(versions)),
                      md=md, media_for=media_for)

    @app.get("/changes", response_class=HTMLResponse)
    def changes(request: Request):
        """Now the Log tab of the Kept page."""
        return RedirectResponse("/kept?tab=log", status_code=303)

    # ---- search ---------------------------------------------------------------
    def archived_communities() -> list[Any]:
        with db.connect() as conn:
            return conn.execute("SELECT DISTINCT c.id, c.name, c.canonical_ap_id FROM communities c "
                                "JOIN archived_threads t ON t.community_id=c.id AND t.trashed_at IS NULL "
                                "ORDER BY c.name, c.canonical_ap_id").fetchall()

    def search_filters(what: str, community: str, author: str, kept: str, only: str,
                       sort: str) -> search_mod.Filters:
        return search_mod.Filters(kind=what if what in search_mod.KINDS else "all",
                                  community_id=int(community) if community.strip().isdigit() else None,
                                  author=author.strip(), kept_only=kept == "1",
                                  only=only if only in search_mod.ONLY else "",
                                  sort=sort if sort in search_mod.SORTS else "relevance")

    def add_post_details(conn: Any, hits: list[dict[str, Any]]) -> None:
        """What a post hit needs to look like it does in the feed: its
        thumbnail and comment count."""
        if not hits:
            return
        marks = ",".join("?" * len(hits))
        extra = {r["id"]: r for r in conn.execute(
            f"SELECT o.id, o.thumbnail_url, (SELECT COUNT(*) FROM objects x WHERE x.thread_id=o.thread_id "
            f"AND x.object_type='comment') AS n_comments FROM objects o WHERE o.id IN ({marks})",
            [h["id"] for h in hits])}
        thumbs = feed_mod.thumbnails(conn, [(h["id"], h["url"], extra[h["id"]]["thumbnail_url"]) for h in hits])
        for h in hits:
            h["thumb"] = thumbs.get(h["id"])
            h["n_comments"] = extra[h["id"]]["n_comments"]

    @app.get("/search", response_class=HTMLResponse)
    def search_page(request: Request, q: str = "", what: str = "all", community: str = "", author: str = "",
                    kept: str = "", only: str = "", sort: str = "relevance", page: int = 1):
        """Every version of every archived post and comment (not the trash)."""
        f = search_filters(what, community, author, kept, only, sort)
        results, error = None, None
        if q.strip():
            try:
                with db.connect() as conn:
                    results = search_mod.search(conn, db.search_backend, q, f, page)
                    add_post_details(conn, [h for h in results.hits if h["object_type"] == "post"])
            except search_mod.SearchError as exc:
                error = str(exc)
        params ={"q": q, "what": f.kind if f.kind != "all" else "", "community": f.community_id or "",
                  "author": f.author, "kept": "1" if f.kept_only else "", "only": f.only,
                  "sort": f.sort if f.sort != "relevance" else ""}

        def page_url(n: int) -> str:
            return "/search?" + urlencode({**{k: v for k, v in params.items() if v}, **({"page": n} if n > 1 else {})})

        def without(*keys: str) -> str:
            """This search with those filters taken off."""
            return "/search?" + urlencode({k: v for k, v in params.items() if v and k not in keys})
        return render(request, "search.html", q=q, f=f, results=results, error=error,
                      communities=archived_communities(), kinds=search_mod.KINDS, sorts=search_mod.SORTS,
                      only_options=search_mod.ONLY, page_url=page_url, without=without, backend=db.search_backend)

    @app.get("/api/search")
    def search_api(q: str = "", what: str = "all", community: str = "", author: str = "", kept: str = "",
                   only: str = "", sort: str = "relevance", page: int = 1):
        try:
            with db.connect() as conn:
                res = search_mod.search(conn, db.search_backend, q,
                                        search_filters(what, community, author, kept, only, sort), page)
        except search_mod.SearchError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return {"total": res.total, "page": res.page, "pages": res.pages, "results": [{
            "id": h["id"], "type": h["object_type"], "thread_id": h["thread_id"],
            "path": f"/t/{h['thread_id']}" + (f"#o{h['id']}" if h["object_type"] == "comment" else ""),
            "title": h["title"] if h["object_type"] == "post" else h["thread_title"],
            "community": chandle(h["cname"], h["c_ap"], plain=True) if h["cname"] else None,
            "author": f"{h['username']}@{h['a_instance']}" if h["username"] else None,
            "created_at": h["created_at"], "text": search_mod.plain(h["body"])[:500],
            "matched_current_version": h["matched_current"], "deleted": bool(h["cur_deleted"]),
            "removed": bool(h["cur_removed"]), "kept": h["retention"] == "manual",
        } for h in res.hits]}

    # ---- inbox ----------------------------------------------------------------
    @app.get("/inbox", response_class=HTMLResponse)
    def inbox_page(request: Request, show: str = "unread", kind: str = "", account: str = "", page: int = 1):
        aid = int(account) if account.strip().isdigit() else None
        kind = kind if kind in INBOX_KINDS else ""
        show = "all" if show == "all" else "unread"
        items, more = inbox.items(unread_only=show == "unread", kind=kind or None, account_id=aid, page=page)
        params = {"show": show if show == "all" else "", "kind": kind, "account": aid or ""}

        def page_url(n: int) -> str:
            return "/inbox?" + urlencode({**{k: v for k, v in params.items() if v}, "page": n})
        return render(request, "inbox.html", items=items, more=more, show=show, kind=kind, account_id=aid,
                      page=page, page_url=page_url, status=inbox.status(), kinds=INBOX_KINDS, md=render_markdown,
                      can_reply_reddit=poster.reddit_account() is not None)

    @app.post("/inbox/check")
    def inbox_check(request: Request):
        new, errors, checked = 0, [], 0
        for a in poster.list():
            if inbox.can_check(a):
                continue
            try:
                new += inbox.check(a)
                checked += 1
            except AccountError as exc:
                errors.append(f"{a.handle}: {exc}")
        if checked:
            flash(request, f"Checked. {new} new." if new else "Checked. Nothing new.")
        if errors:
            flash(request, "Couldn't check " + "; ".join(errors), "error")
        if not checked and not errors:
            flash(request, "There's no inbox to check yet: add an account on the Accounts page.", "error")
        return RedirectResponse(back(request, "/inbox"), status_code=303)

    @app.get("/inbox/read-all", response_class=HTMLResponse)
    def inbox_confirm_read_all(request: Request, account: str = ""):
        aid = int(account) if account.strip().isdigit() else None
        rows = [s for s in inbox.status() if s["unread"] and (aid is None or s["account"].id == aid)]
        if not rows:
            return RedirectResponse("/inbox", status_code=303)
        return render(request, "inbox_confirm.html", rows=rows, account_id=aid)

    @app.post("/inbox/read-all")
    def inbox_read_all(request: Request, account_id: str = Form(""), confirmed: str = Form("")):
        """Marks items read on their servers too, which can't be undone from here, so it asks first."""
        if confirmed != "1":
            return RedirectResponse("/inbox/read-all" + (f"?account={account_id}" if account_id.isdigit() else ""),
                                    status_code=303)
        try:
            n = inbox.mark_all_read(int(account_id) if account_id.strip().isdigit() else None)
            flash(request, f"Marked {n} as read." if n else "Nothing was unread.")
        except AccountError as exc:
            flash(request, str(exc), "error")
        return RedirectResponse(back(request, "/inbox"), status_code=303)

    @app.post("/inbox/{iid}/read")
    def inbox_read(request: Request, iid: int, read: str = Form("1"), anchor: str = Form("")):
        """`anchor`: where to come back to (the next item, when this one leaves the Unread list)."""
        try:
            inbox.mark_read(iid, read == "1")
        except AccountError as exc:
            flash(request, str(exc), "error")
            return RedirectResponse(back(request, "/inbox", f"i{iid}"), status_code=303)
        if read == "1":
            flash(request, "Marked read.", undo={"action": f"/inbox/{iid}/read", "fields": {"read": "0"}})
        return RedirectResponse(back(request, "/inbox", safe_anchor(anchor)), status_code=303)

    @app.post("/inbox/{iid}/reply")
    def inbox_reply(request: Request, iid: int, body: str = Form("")):
        try:
            oid = inbox.reply(iid, body)
        except AccountError as exc:
            flash(request, str(exc), "error")
            return RedirectResponse(back(request, "/inbox", f"i{iid}"), status_code=303)
        flash(request, "Reply sent. It's in the archived thread too." if oid else "Reply sent.",
              link={"href": f"{object_page(oid)}#o{oid}", "label": "Show in thread"} if oid else None)
        return RedirectResponse(back(request, "/inbox", f"i{iid}"), status_code=303)

    app.state.bouncer = bouncer
    app.state.db = db
    return app
