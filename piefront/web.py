"""The pages. Anyone can read; signing in (with an account on the server this
is a frontend for) adds your subscriptions, voting, commenting and your inbox.

Forms work without JavaScript and answer with a redirect. Sent by ThreadBNC's
app.js (which says so with a header) they're answered with where that would
have gone and its messages instead, so the page updates in place: the same
arrangement ThreadBNC's own pages have, which is what lets its scripts run here."""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, urlencode, urlparse

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

import threadbnc
from threadbnc.adapters import RemoteAuthError, RemoteError, RemoteNotFound, RemotePaused, RemoteRejected
from threadbnc.adapters.http import HttpClient
from threadbnc.web import THEMES, absolute, ago, chandle, flash_json, is_fetch, safe_anchor, safe_url

from . import forums as forums_mod
from .api import COMMENT_SORTS, LISTINGS, SORTS, WINDOWS, Me, PieFed
from .config import Settings, load_settings
from .render import render_markdown

log = logging.getLogger("piefront.web")
HERE = Path(__file__).parent
SHARED = Path(threadbnc.__file__).parent  # ThreadBNC's templates (macros) and static files (styles, icons, scripts)

VIEWS = ("list", "pictures", "tiles", "timeline")
INBOX_KINDS = {"reply": "Replies", "mention": "Mentions", "message": "Messages"}
LOGIN_TRIES, LOGIN_WINDOW = 10, 600  # sign-ins tried from one address, in so many seconds


class Tries:
    """How often each address has tried to sign in lately."""

    def __init__(self) -> None:
        self._at: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def allowed(self, who: str) -> bool:
        now = time.monotonic()
        with self._lock:
            if len(self._at) > 10_000:
                self._at = {k: v for k, v in self._at.items() if v and now - v[-1] < LOGIN_WINDOW}
            recent = [t for t in self._at.get(who, []) if now - t < LOGIN_WINDOW]
            if len(recent) >= LOGIN_TRIES:
                self._at[who] = recent
                return False
            self._at[who] = [*recent, now]
            return True


def create_app(settings: Settings | None = None, http: HttpClient | None = None) -> FastAPI:
    settings = settings or load_settings()
    http = http or HttpClient(settings.user_agent, min_interval=0)
    api = PieFed(settings.server, http, settings.cache_seconds)
    tries = Tries()

    app = FastAPI(title="piefront", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.api = api
    app.mount("/static", StaticFiles(directory=SHARED / "static"), name="static")
    templates = Jinja2Templates(directory=[HERE / "templates", SHARED / "templates"])

    def static_url(name: str) -> str:
        return f"/static/{name}?v={int((SHARED / 'static' / name).stat().st_mtime)}"

    def host(url: str | None) -> str:
        return urlparse(url or "").hostname or ""

    templates.env.filters.update(ago=ago, absolute=absolute, safe_url=safe_url, host=host)
    templates.env.globals.update(static_url=static_url, chandle=chandle, themes=THEMES, md=render_markdown,
                                 server=settings.server, listings=LISTINGS, sorts=SORTS, windows=WINDOWS)

    # ---- who's asking ----------------------------------------------------------------------
    def token_of(request: Request) -> str | None:
        return request.session.get("jwt") or None

    def flash(request: Request, msg: str, kind: str = "info") -> None:
        # Set anew, not added to in place: the session is only sent back when it's seen to change.
        request.session["flash"] = [*request.session.get("flash", []), [kind, msg]]

    def who(request: Request) -> Me | None:
        """Whoever is signed in. Someone whose session the server no longer accepts is signed out here too."""
        if hasattr(request.state, "me"):
            return request.state.me
        token, me = token_of(request), None
        if token:
            try:
                me = api.me(token)
            except RemoteAuthError:
                request.session.pop("jwt", None)
                flash(request, f"Your session on {settings.server} has ended. Sign in again to vote, comment "
                               "and see your subscriptions.", "error")
        request.state.me = me
        return me

    def reading(request: Request) -> str | None:
        """The session to read as: none unless it's still accepted (see who)."""
        return token_of(request) if who(request) else None

    def site() -> dict[str, Any]:
        try:
            found = api.site()
        except RemoteError:
            found = {"name": settings.server, "description": "", "icon": None, "downvotes": True}
        return {**found, "name": settings.name or found["name"]}

    def render(request: Request, name: str, status: int = 200, **ctx: Any) -> HTMLResponse:
        try:
            ctx.setdefault("me", who(request))
        except RemoteError:  # the error page itself, while the server can't be reached
            ctx.setdefault("me", None)
        ctx.setdefault("site", site())
        return templates.TemplateResponse(request, name, ctx, status_code=status)

    def back(request: Request, default: str = "/", anchor: str | None = None) -> str:
        """Where a form was sent from, when that's here."""
        ref = urlparse(request.headers.get("referer", ""))
        here = ref.netloc == request.url.netloc and ref.path.startswith("/") and not ref.path.startswith("//")
        dest = (ref.path + (f"?{ref.query}" if ref.query else "")) if here else default
        return dest + (f"#{anchor}" if safe_anchor(anchor) else "")

    def to_login(request: Request) -> RedirectResponse:
        flash(request, "Sign in to do that.", "error")
        return RedirectResponse(f"/login?{urlencode({'next': back(request)})}", status_code=303)

    def act(request: Request, do: Callable[[str], str | None], dest: str) -> Response:
        """Do something as whoever is signed in, then go to `dest`. `do` gets
        their session and returns what to tell them, if anything."""
        token = token_of(request)
        if not token:
            return to_login(request)
        try:
            said = do(token)
            if said:
                flash(request, said)
        except RemoteAuthError as exc:
            if exc.code and "not_logged_in" not in exc.code.lower() and "jwt" not in exc.code.lower():
                flash(request, f"{settings.server} wouldn't allow that: {exc.code}", "error")
            else:
                request.session.pop("jwt", None)
                api.forget(token)
                return to_login(request)
        except RemoteRejected as exc:
            flash(request, f"{settings.server} refused: {exc.code or exc}", "error")
        except RemotePaused:
            flash(request, f"{settings.server} asked for fewer requests. Try again in a minute.", "error")
        except RemoteNotFound:
            flash(request, f"{settings.server} couldn't find that. It may have been deleted, or the name is wrong.", "error")
        except RemoteError as exc:
            log.warning("action failed: %s", exc)
            flash(request, f"That didn't reach {settings.server}. Try again.", "error")
        return RedirectResponse(dest, status_code=303)

    # ---- every request ---------------------------------------------------------------------
    @app.middleware("http")
    async def guard(request: Request, call_next):
        path = request.url.path
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            # Forms are only taken from this site's own pages. (The session cookie is
            # SameSite=Lax, so another site's form wouldn't carry it anyway.)
            origin = request.headers.get("origin")
            if (origin and urlparse(origin).netloc != request.url.netloc) or \
                    request.headers.get("sec-fetch-site") == "cross-site":
                return PlainTextResponse("That form came from another site.", status_code=403)
        resp = await call_next(request)
        if is_fetch(request) and resp.status_code in (302, 303):
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
            # Pictures and videos are shown from where they are (the server's own copies, mostly): nothing is kept here.
            "default-src 'self'; img-src 'self' https: data:; media-src 'self' https:; style-src 'self'; "
            "script-src 'self'; form-action 'self'; frame-ancestors 'none'")
        if not path.startswith("/static/"):
            resp.headers["Cache-Control"] = "no-store"
        elif "v" in request.query_params:
            resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return resp

    app.add_middleware(SessionMiddleware, secret_key=settings.secret_key, same_site="lax",
                       https_only=settings.https_only_cookies, max_age=60 * 60 * 24 * 30,
                       session_cookie="piefront_session")

    @app.exception_handler(RemoteNotFound)
    def not_found(request: Request, exc: RemoteNotFound):
        return render(request, "error.html", 404, title="Not found",
                      message=f"{settings.server} has nothing at that address. It may have been deleted.")

    @app.exception_handler(RemoteError)
    def unreachable(request: Request, exc: RemoteError):
        log.warning("%s %s: %s", request.method, request.url.path, exc)
        if isinstance(exc, RemotePaused):
            message = f"{settings.server} asked for fewer requests. Try again in a minute."
        elif isinstance(exc, RemoteAuthError):
            message = f"{settings.server} doesn't show that to you" + ("." if token_of(request) else " unless you sign in.")
        else:
            message = f"{settings.server} couldn't be reached just now. Try again in a moment."
        return render(request, "error.html", 403 if isinstance(exc, RemoteAuthError) else 502,
                      title="Can't show that", message=message)

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.get("/robots.txt", response_class=PlainTextResponse)
    def robots():
        return "User-agent: *\nDisallow: /\n"

    # ---- signing in --------------------------------------------------------------------------
    def local_path(dest: str | None) -> str:
        return dest if dest and dest.startswith("/") and not dest.startswith("//") and "\\" not in dest else "/"

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request, next: str = "/"):
        if who(request):
            return RedirectResponse(local_path(next), status_code=303)
        return render(request, "login.html", next=local_path(next), error=None, username="")

    @app.post("/login")
    def login(request: Request, username: str = Form(""), password: str = Form(""), next: str = Form("/")):
        username, error = username.strip(), None
        if not tries.allowed(request.client.host if request.client else "?"):
            error = "Too many sign-ins tried from your address. Wait ten minutes and try again."
        elif not username or not password:
            error = "Fill in your user name and password."
        else:
            try:
                token = api.adapter.login(username, password)
                me = api.me(token)
            except (RemoteAuthError, RemoteRejected) as exc:
                error = f"{settings.server} didn't accept that" + (f": {exc.code}" if getattr(exc, "code", "") else ".")
            except RemotePaused:
                error = f"{settings.server} asked for fewer requests. Try again in a minute."
            except RemoteError as exc:
                log.warning("sign-in failed: %s", exc)
                error = f"{settings.server} couldn't be reached. Try again in a moment."
        if error:
            return render(request, "login.html", 401, next=local_path(next), error=error, username=username)
        request.session.clear()
        request.session["jwt"] = token
        flash(request, f"Signed in as {me.person['display']}.")
        return RedirectResponse(local_path(next), status_code=303)

    @app.post("/logout")
    def logout(request: Request):
        token = token_of(request)
        if token:
            api.forget(token)
            try:
                api.adapter.logout(token)
            except RemoteError:
                pass  # signed out here either way
        request.session.clear()
        return RedirectResponse("/", status_code=303)

    @app.post("/theme")
    def set_theme(request: Request, theme: str = Form("")):
        resp = RedirectResponse(back(request), status_code=303)
        if theme in THEMES:
            resp.set_cookie("theme", theme, max_age=60 * 60 * 24 * 400, samesite="lax", httponly=True,
                            secure=settings.https_only_cookies)
        else:
            resp.delete_cookie("theme")
        return resp

    # What ThreadBNC's page script asks of its own server as a matter of course: nothing to do here.
    @app.post("/presence")
    def presence():
        return Response(status_code=204)

    @app.get("/live/owncast")
    def owncast():
        return []  # (which linked sites are Owncast servers: none are looked into here)

    @app.post("/t/{post_id}/comments/check")
    def comments_check(post_id: int):
        return {"jobs": []}  # comments are read from the server each time they're shown

    # ---- feeds -------------------------------------------------------------------------------
    def feed_page(request: Request, name: str, base: str, feed: str, sort: str, t: str, view: str, page: int,
                  community: dict[str, Any] | None = None, **ctx: Any) -> Response:
        me = who(request)
        sort = sort if sort in SORTS else "hot"
        t = t if t in WINDOWS else "day"
        page = max(1, min(page, 500))
        chosen = view if view in VIEWS else None
        view = chosen or (request.cookies.get("view") if request.cookies.get("view") in VIEWS else "list")
        if community is None:
            feed = feed if feed in LISTINGS else ("subscribed" if me and me.follows else "popular")
            if feed == "subscribed" and not me:
                return RedirectResponse(f"/login?{urlencode({'next': '/?feed=subscribed'})}", status_code=303)
            base = f"{base}feed={feed}&"
        items, more = api.posts(reading(request), feed, sort, t, page, community["handle"] if community else None)
        resp = render(request, name, items=items, more=more, page=page, base=base, feed=feed, sort=sort, window=t,
                      view=view, community=community, **ctx)
        if chosen:  # remembered in this browser
            resp.set_cookie("view", chosen, max_age=60 * 60 * 24 * 400, samesite="lax", httponly=True,
                            secure=settings.https_only_cookies)
        return resp

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request, feed: str = "", sort: str = "", t: str = "", view: str = "", page: int = 1):
        return feed_page(request, "feed.html", "/?", feed, sort, t, view, page)

    @app.get("/c/{handle}", response_class=HTMLResponse)
    def community_page(request: Request, handle: str, sort: str = "", t: str = "", view: str = "", page: int = 1):
        community = api.community_page(reading(request), handle)
        if not community["ap_id"]:
            raise RemoteNotFound(handle)
        return feed_page(request, "community.html", f"/c/{quote(community['handle'], safe='@')}?", "", sort, t, view,
                         page, community=community)

    @app.post("/follow")
    def follow(request: Request, community_id: int = Form(...), follow: str = Form("1"), anchor: str = Form("")):
        on = follow == "1"

        def do(token: str) -> str:
            state = api.adapter.follow_community(token, str(community_id), on)
            api.forget(token)
            if not on:
                return "Unsubscribed."
            return "Subscribed." if state == "subscribed" else \
                "Asked to subscribe: it shows in your list once the community's server answers."
        return act(request, do, back(request, anchor=anchor))

    # ---- a post and its comments -------------------------------------------------------------
    @app.get("/t/{post_id}", response_class=HTMLResponse)
    def thread(request: Request, post_id: int, sort: str = ""):
        token = reading(request)
        sort = sort if sort in COMMENT_SORTS else "hot"
        post = api.thread(token, post_id)
        comments, shown, more = api.comments(token, post_id, sort)
        return render(request, "thread.html", post=post, comments=comments, shown=shown, more=more,
                      comment_sort=sort, comment_sorts=COMMENT_SORTS)

    @app.post("/t/{post_id}/reply")
    def reply(request: Request, post_id: int, body: str = Form(""), parent_id: str = Form("")):
        body = body.strip()
        if not body:
            flash(request, "Write something first.", "error")
            return RedirectResponse(f"/t/{post_id}#comments", status_code=303)
        made: dict[str, str] = {}

        def do(token: str) -> None:
            made["id"] = api.adapter.create_comment(token, str(post_id), body,
                                                    parent_id if parent_id.isdigit() else None).local_id
        resp = act(request, do, f"/t/{post_id}#comments")
        if made:
            return RedirectResponse(f"/t/{post_id}#o{made['id']}", status_code=303)
        return resp

    def vote(request: Request, cast: Callable[[str, int], Any], score: int) -> Response:
        if score not in (-1, 0, 1):
            raise HTTPException(400)
        return act(request, lambda token: cast(token, score) and None, back(request))

    @app.post("/p/{post_id}/vote")
    def vote_post(request: Request, post_id: int, score: int = Form(...)):
        return vote(request, lambda token, s: api.adapter.vote_post(token, str(post_id), s), score)

    @app.post("/k/{comment_id}/vote")
    def vote_comment(request: Request, comment_id: int, score: int = Form(...)):
        return vote(request, lambda token, s: api.adapter.vote_comment(token, str(comment_id), s), score)

    # ---- the forum directory -----------------------------------------------------------------
    def forum_page(request: Request, kind: str, path: str, part: int, depth: int) -> Response:
        me = who(request)
        depth = max(0, min(depth, 20))
        parts = [p for p in path.split("/") if p]
        try:
            tree = api.forums(kind, settings.forum_cache_seconds,
                              lambda data: forums_mod.build(data, settings.server, kind))
        except ValueError as exc:  # the server doesn't list them
            return render(request, "error.html", 502, title="Can't show that", message=str(exc))
        v = forums_mod.view(api, kind, tree, parts, me.followed if me else set(), part, depth)
        if v is None:
            raise HTTPException(404)
        if part:
            return render(request, "forum_part.html", v=v, depth=depth)
        node = v["node"]
        return render(request, "forum.html", v=v, kind=kind, kinds=forums_mod.KINDS, node=node, head=v["head"],
                      parts=parts, description=render_markdown(node["description"]) if node["description"] else None)

    @app.get("/forums", response_class=HTMLResponse)
    def forums_index(request: Request, part: int = 0, depth: int = 0):
        return forum_page(request, "topic", "", part, depth)

    @app.get("/forums/{kinds}", response_class=HTMLResponse)
    @app.get("/forums/{kinds}/{path:path}", response_class=HTMLResponse)
    def forums_part(request: Request, kinds: str, path: str = "", part: int = 0, depth: int = 0):
        if kinds not in ("topics", "feeds"):
            raise HTTPException(404)
        return forum_page(request, kinds[:-1], path, part, depth)

    # ---- inbox and messages ------------------------------------------------------------------
    def inbox_items(token: str, me: Me) -> list[dict[str, Any]]:
        out = []
        for i in api.adapter.inbox(token, me.person["ap_id"]):
            community = i.community
            out.append({
                "id": f"{i.kind}-{i.remote_id}", "kind": i.kind, "remote_id": i.remote_id, "unread": i.unread,
                "author": i.author.display_name or i.author.username, "author_ap_id": i.author.ap_id,
                "author_handle": api.handle(i.author.username, i.author.ap_id), "author_id": i.author_local_id,
                "body": i.body or "", "created_at": i.created_at, "deleted": i.deleted,
                "comment_id": i.object_local_id if i.object_type == "comment" else None,
                "post_id": i.post_local_id, "post_title": i.post_title,
                "community": api.handle(community.name, community.ap_id) if community and community.ap_id else None})
        out.sort(key=lambda i: i["created_at"] or "", reverse=True)
        return out

    @app.get("/inbox", response_class=HTMLResponse)
    def inbox(request: Request, show: str = "unread", kind: str = "", to: str = ""):
        token, me = token_of(request), who(request)
        if not token or not me:
            return RedirectResponse(f"/login?{urlencode({'next': '/inbox'})}", status_code=303)
        items = inbox_items(token, me)
        unread = sum(1 for i in items if i["unread"])
        if kind in INBOX_KINDS:
            items = [i for i in items if i["kind"] == kind]
        if show != "all":
            show, items = "unread", [i for i in items if i["unread"]]
        return render(request, "inbox.html", items=items, show=show, kind=kind if kind in INBOX_KINDS else "",
                      kinds=INBOX_KINDS, unread_total=unread, to=to)

    @app.post("/inbox/message")
    def send_message(request: Request, to: str = Form(""), body: str = Form("")):
        to, body = to.strip(), body.strip()
        if not to or not body:
            flash(request, "Say who it's for, and write something.", "error")
            return RedirectResponse("/inbox#new-message", status_code=303)
        return act(request, lambda token: f"Sent to {api.adapter.message_person(token, to, body)}.",
                   back(request, "/inbox"))

    @app.post("/inbox/read-all")
    def inbox_read_all(request: Request):
        def do(token: str) -> str:
            api.adapter.mark_all_inbox_read(token, [])
            api.forget(token)
            return "Marked everything read."
        return act(request, do, "/inbox")

    @app.post("/inbox/{kind}/{remote_id}/read")
    def inbox_read(request: Request, kind: str, remote_id: int, read: str = Form("1"), anchor: str = Form("")):
        if kind not in INBOX_KINDS:
            raise HTTPException(404)

        def do(token: str) -> None:
            api.adapter.mark_inbox_read(token, kind, str(remote_id), read == "1")
            api.forget(token)
        return act(request, do, back(request, "/inbox", anchor))

    @app.post("/inbox/{kind}/{remote_id}/reply")
    def inbox_reply(request: Request, kind: str, remote_id: int, body: str = Form(""), post_id: str = Form(""),
                    comment_id: str = Form(""), author_id: str = Form("")):
        body = body.strip()
        if kind not in INBOX_KINDS:
            raise HTTPException(404)
        if not body:
            flash(request, "Write something first.", "error")
            return RedirectResponse(back(request, "/inbox"), status_code=303)

        def do(token: str) -> str:
            if kind == "message":
                if not author_id.isdigit():
                    raise HTTPException(400)
                api.adapter.send_message(token, author_id, body)
            else:
                if not (post_id.isdigit() and comment_id.isdigit()):
                    raise HTTPException(400)
                api.adapter.create_comment(token, post_id, body, comment_id)
            try:  # answered, so read
                api.adapter.mark_inbox_read(token, kind, str(remote_id), True)
            except RemoteError:
                pass
            api.forget(token)
            return "Reply sent."
        return act(request, do, back(request, "/inbox"))

    return app
