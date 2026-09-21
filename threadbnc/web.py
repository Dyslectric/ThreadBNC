"""Archive UI + API. Private by default: every route except /login and static
assets requires an authenticated session (or API bearer token)."""

from __future__ import annotations

import difflib
import hmac
import html
import json
import logging
import math
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import (
    FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from starlette.middleware.sessions import SessionMiddleware

from . import feed as feed_mod
from . import media as media_mod
from .adapters import RemoteError, host_of
from .accounts import Account, AccountError, Poster
from .bouncer import Bouncer
from .config import Settings, load_settings
from .db import open_database, parse_ts, utcnow
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
    "auto_capture_expired": "Auto-captured thread expired and purged",
    "trashed": "Moved to trash",
    "restored_from_trash": "Restored from trash",
    "trash_expired": "Trashed thread permanently deleted (trash period ended)",
    "deleted_from_trash": "Trashed thread permanently deleted",
    "delete_requested": "You deleted this via ThreadBNC (waiting to see it on the server)",
    "undelete_requested": "You restored this via ThreadBNC (waiting to see it on the server)",
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
    "deleted_from_trash": "muted", "trashed": "warn", "restored_from_trash": "good",
}


COMMENT_SORTS = {"hot": "Hot", "top": "Top", "new": "New", "old": "Old", "controversial": "Controversial"}


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
    """(key, reverse) for sibling comments, using counts from the last observation."""
    keys: dict[str, tuple[Callable[[Any], Any], bool]] = {
        "hot": (lambda n: _hot(n["o"]), True),
        "top": (lambda n: (_net_score(n["o"]), n["o"]["created_at"] or ""), True),
        "new": (lambda n: n["o"]["created_at"] or "", True),
        "old": (lambda n: n["o"]["created_at"] or "", False),
        "controversial": (lambda n: (_controversy(n["o"]), _net_score(n["o"])), True),
    }
    return keys.get(sort, keys["hot"])


def sort_tree(node: dict[str, Any], sort: str) -> None:
    key, reverse = comment_sort_key(sort)
    stack = [node]
    while stack:
        n = stack.pop()
        n["children"].sort(key=key, reverse=reverse)
        stack.extend(n["children"])


def event_label(e: Any) -> str:
    et = e["event_type"]
    label = EVENT_LABELS.get(et, et.replace("_", " ").capitalize())
    if et in ("removed", "restored", "locked", "unlocked", "community_removed") and e["attribution"]:
        label += " " + ATTRIBUTION_LABELS.get(e["attribution"], f"by {e['attribution']}")
    meta = json.loads(e["metadata_json"] or "{}")
    if meta.get("state_at_first_observation"):
        label += " (already in this state when first observed)"
    return label


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


def safe_url(value: str | None) -> str | None:
    if not value:
        return None
    return value if urlparse(value).scheme in ("http", "https") else None


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
    if not settings.password:
        raise SystemExit("THREADBNC_PASSWORD must be set; the archive is never served unauthenticated.")
    db = bouncer.db if bouncer else open_database(settings)
    bouncer = bouncer or Bouncer(db, settings)
    poster = Poster(bouncer, TokenVault(settings.credentials_key, settings.data_dir))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if settings.embedded_bouncer:
            bouncer.start_thread()
        yield
        bouncer.stop()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    templates = Jinja2Templates(directory=str(HERE / "templates"))
    templates.env.filters.update(ago=ago, absolute=absolute, safe_url=safe_url, host=host_of,
                                 looks_like_media=looks_like_media)
    def static_url(name: str) -> str:
        # Cache-bust with the file's mtime so UI updates show up without a hard reload.
        return f"/static/{name}?v={int((HERE / 'static' / name).stat().st_mtime)}"

    def trash_count() -> int:
        with db.connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM archived_threads WHERE trashed_at IS NOT NULL").fetchone()[0]

    templates.env.globals.update(event_label=event_label, tone=lambda e: TONE.get(e["event_type"], ""),
                                 static_url=static_url, trash_count=trash_count)
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

    PUBLIC = ("/login", "/static/", "/robots.txt", "/healthz")

    @app.middleware("http")
    async def guard(request: Request, call_next):
        path = request.url.path
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
                resp = RedirectResponse("/login", status_code=303)
        else:
            resp = await call_next(request)
        resp.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive"
        resp.headers["Referrer-Policy"] = "no-referrer"
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

    def acting(request: Request) -> Account | None:
        """The account forms act as: the one picked in the header, else the default."""
        chosen = poster.get(request.session.get("acting_account"))
        return chosen or poster.default()

    def render(request: Request, name: str, **ctx: Any) -> HTMLResponse:
        if request.session.get("auth"):
            ctx.setdefault("accounts", poster.list())
            ctx.setdefault("acting", acting(request))
        return templates.TemplateResponse(request, name, ctx)

    def require_acting(request: Request) -> Account:
        account = acting(request)
        if account is None:
            raise AccountError("Add an account on the Accounts page first.")
        return account

    def flash(request: Request, msg: str, kind: str = "info") -> None:
        request.session.setdefault("flash", []).append([kind, msg])

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

    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request):
        return render(request, "login.html", error=None)

    @app.post("/login")
    def login(request: Request, password: str = Form(...)):
        if hmac.compare_digest(password.encode(), settings.password.encode()):  # type: ignore[union-attr]
            request.session.clear()
            request.session["auth"] = True
            return RedirectResponse("/", status_code=303)
        time.sleep(1.0)
        return render(request, "login.html", error="Wrong password.")

    @app.post("/logout")
    def logout(request: Request):
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    # ---- feed (home) -----------------------------------------------------
    def back(request: Request, default: str) -> str:
        ref = urlparse(request.headers.get("referer") or "")
        if ref.path.startswith("/") and ref.netloc in ("", request.url.netloc):
            return ref.path + (f"?{ref.query}" if ref.query else "")
        return default

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request, sort: str = "new", t: str = "all", unread: int = 0, page: int = 1):
        with db.connect() as conn:
            fp = feed_mod.load_feed(conn, sort=sort, window=t, unread=bool(unread), page=page)
            follows = feed_mod.followed_communities(conn)
        return render(request, "feed.html", feed=fp, follows=follows, sort=sort, window=t, unread=unread,
                      base_url="/", community=None)

    @app.post("/feed/mark-read")
    def mark_read(request: Request, community_id: int | None = Form(None)):
        with db.transaction() as conn:
            feed_mod.mark_read(conn, utcnow(), community_id)
        flash(request, "Marked as read.")
        return RedirectResponse(back(request, "/"), status_code=303)

    # ---- kept (the archive) ---------------------------------------------
    @app.get("/kept", response_class=HTMLResponse)
    def kept(request: Request):
        with db.connect() as conn:
            jobs = conn.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT 8").fetchall()
            rows = conn.execute(
                "SELECT t.*, r.title, o.created_at, o.cur_deleted, o.cur_removed, o.cur_locked, o.cur_missing, "
                "o.revision_count, c.id AS cid, c.name AS cname, c.canonical_ap_id AS c_ap, "
                "(SELECT COUNT(*) FROM objects x WHERE x.thread_id=t.id AND x.object_type='comment') AS n_comments "
                "FROM archived_threads t JOIN objects o ON o.id=t.root_object_id "
                "JOIN communities c ON c.id=t.community_id "
                "JOIN revisions r ON r.object_id=o.id AND r.seq=o.revision_count "
                "WHERE t.retention='manual' AND t.trashed_at IS NULL "
                "ORDER BY c.name, c.canonical_ap_id, COALESCE(t.promoted_at, t.retained_at) DESC"
            ).fetchall()
            changes = recent_changes(conn, limit=20)
        groups: dict[int, dict[str, Any]] = {}
        for r in rows:
            groups.setdefault(r["cid"], {"cid": r["cid"], "name": r["cname"], "ap": r["c_ap"], "threads": []})
            groups[r["cid"]]["threads"].append(r)
        pending = any(j["status"] in ("queued", "running") for j in jobs)
        return render(request, "kept.html", jobs=[dict(j, payload=json.loads(j["payload_json"]),
                                                       result=json.loads(j["result_json"] or "null"))
                                                  for j in jobs],
                      groups=list(groups.values()), total=len(rows), changes=changes, pending=pending)

    def recent_changes(conn: Any, limit: int = 30, community_id: int | None = None,
                       thread_id: int | None = None) -> list[dict[str, Any]]:
        where, args = "", []
        if community_id:
            where, args = " AND o.community_id=?", [community_id]
        if thread_id:
            where, args = " AND o.thread_id=?", [thread_id]
        rows = conn.execute(
            f"""
            SELECT * FROM (
              SELECT e.observed_at AS at, e.event_type, e.attribution, e.metadata_json, e.reason,
                     o.id AS object_id, o.object_type, o.thread_id, NULL AS seq
              FROM state_events e JOIN objects o ON o.id=e.object_id
              JOIN archived_threads th ON th.id=o.thread_id AND th.trashed_at IS NULL WHERE 1=1 {where}
              UNION ALL
              SELECT r.observed_at, 'edited', NULL, '{{}}', NULL, o.id, o.object_type, o.thread_id, r.seq
              FROM revisions r JOIN objects o ON o.id=r.object_id
              JOIN archived_threads th ON th.id=o.thread_id AND th.trashed_at IS NULL WHERE r.seq>1 {where}
            ) AS changes ORDER BY at DESC LIMIT ?""",
            [*args, *args, limit],
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["label"] = "Edited" if r["event_type"] == "edited" else event_label(r)
            d["tone"] = "warn" if r["event_type"] == "edited" else TONE.get(r["event_type"], "")
            d["title"] = thread_title(conn, r["thread_id"])
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
        return render(request, "communities.html", follows=follows, others=others,
                      default_poll=settings.default_follow_poll_minutes,
                      default_days=settings.default_follow_retention_days)

    @app.post("/follow")
    def follow(request: Request, community: str = Form(...), poll_interval_minutes: int = Form(15),
               retention_days: str = Form("30"), backfill: str | None = Form(None)):
        days = None if retention_days.strip().lower() in ("", "forever", "none") else int(retention_days)
        try:
            cid = bouncer.follow_community(community, max(1, poll_interval_minutes), days, bool(backfill))
        except (RemoteError, ValueError) as exc:
            flash(request, f"Could not follow {community}: {exc}", "error")
            return RedirectResponse(back(request, "/communities"), status_code=303)
        flash(request, "Following. Posts will show up in your feed within a minute or so.")
        return RedirectResponse(f"/c/{cid}", status_code=303)

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

    @app.get("/c/{cid}", response_class=HTMLResponse)
    def community(request: Request, cid: int, tab: str = "feed", sort: str = "new", t: str = "all",
                  unread: int = 0, page: int = 1, live_sort: str = "Hot"):
        with db.connect() as conn:
            c = conn.execute("SELECT c.*, i.domain FROM communities c JOIN instances i ON i.id=c.instance_id "
                             "WHERE c.id=?", (cid,)).fetchone()
            if not c:
                raise HTTPException(404)
            follow_row = conn.execute("SELECT * FROM community_follows WHERE community_id=?", (cid,)).fetchone()
            fp = None
            if tab in ("feed", "kept"):
                fp = feed_mod.load_feed(conn, community_id=cid, sort=sort, window=t, unread=bool(unread),
                                        kept_only=tab == "kept", page=page)
            counts = {r["k"]: r["n"] for r in conn.execute(
                "SELECT CASE WHEN trashed_at IS NOT NULL THEN 'trash' ELSE retention END AS k, COUNT(*) n "
                "FROM archived_threads WHERE community_id=? GROUP BY 1", (cid,))}
            cevents = conn.execute("SELECT * FROM state_events WHERE community_id=? ORDER BY id DESC LIMIT 30",
                                   (cid,)).fetchall()
            follows = feed_mod.followed_communities(conn)
        live, live_error = [], None
        if tab == "live":
            try:
                ref = bouncer.community_ref(cid)
                posts = bouncer.adapter_for(ref.domain).list_community_posts(ref, sort=live_sort, page=page,
                                                                           limit=25)
                with db.connect() as conn:
                    for p in posts:
                        th = conn.execute(
                            "SELECT t.id, t.retention, t.expires_at, t.trashed_at FROM archived_threads t "
                            "JOIN objects o ON o.id=t.root_object_id WHERE o.canonical_ap_id=?",
                            (p.ap_id,)).fetchone()
                        live.append({"post": p, "thread": th,
                                     "retain_url": f"https://{ref.domain}/post/{p.local_id}"})
            except RemoteError as exc:
                live_error = str(exc)
        return render(request, "community.html", c=c, follow=follow_row, tab=tab, feed=fp, counts=counts,
                      events=cevents, live=live, live_error=live_error, sort=sort, window=t, unread=unread,
                      page=page, live_sort=live_sort, follows=follows, base_url=f"/c/{cid}",
                      community=c)

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
        inline = ctype.startswith(("image/", "video/")) and ctype != "image/svg+xml"
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
    @app.post("/t/{tid}/keep")
    def keep(request: Request, tid: int):
        bouncer.promote(tid)
        flash(request, "Kept permanently. It won't expire.")
        return RedirectResponse(back(request, f"/t/{tid}"), status_code=303)

    @app.post("/t/{tid}/unkeep")
    def unkeep(request: Request, tid: int):
        outcome = bouncer.unkeep(tid)
        if outcome == "auto":
            flash(request, "No longer kept. It stays in your feed and expires with the community's retention.")
        elif outcome == "trash":
            flash(request, "No longer kept. Moved to the trash; you can restore it from there.")
        return RedirectResponse(back(request, f"/t/{tid}"), status_code=303)

    @app.post("/t/{tid}/trash")
    def trash_thread(request: Request, tid: int):
        bouncer.move_to_trash(tid)
        days = bouncer.trash_days()
        when = f"in {days} day{'s' if days != 1 else ''}" if days is not None else "only when you empty the trash"
        flash(request, f"Moved to trash. It will be permanently deleted {when}; restore it from the Trash page.")
        return RedirectResponse(back(request, f"/t/{tid}"), status_code=303)

    @app.post("/t/{tid}/restore")
    def restore_thread(request: Request, tid: int):
        retention = bouncer.restore_from_trash(tid)
        if retention:
            flash(request, "Restored" + (" and kept permanently." if retention == "manual" else
                                         " as auto-captured. Its original expiry date still applies."))
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
    def accounts_page(request: Request):
        return render(request, "accounts.html")

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

    @app.post("/accounts/act-as")
    def act_as(request: Request, account_id: int = Form(...)):
        if poster.get(account_id):
            request.session["acting_account"] = account_id
        return RedirectResponse(back(request, "/"), status_code=303)

    def account_action(request: Request, fallback: str, fn: Any, ok: str | None = None) -> RedirectResponse:
        """Run a write as the acting account; show errors instead of raising."""
        try:
            result = fn(require_acting(request))
        except AccountError as exc:
            flash(request, str(exc), "error")
            return RedirectResponse(back(request, fallback), status_code=303)
        if ok:
            flash(request, ok)
        return result if isinstance(result, RedirectResponse) else RedirectResponse(
            back(request, fallback), status_code=303)

    @app.post("/t/{tid}/reply")
    def reply(request: Request, tid: int, body: str = Form(...), parent_id: int | None = Form(None)):
        def act(account: Account) -> RedirectResponse:
            oid = poster.reply(account, tid, body, parent_id)
            return RedirectResponse(f"/t/{tid}#o{oid}", status_code=303)
        return account_action(request, f"/t/{tid}", act, "Comment posted.")

    @app.post("/o/{oid}/vote")
    def vote(request: Request, oid: int, score: int = Form(...)):
        return account_action(request, "/", lambda a: poster.vote(a, oid, score))

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
        return account_action(request, "/", lambda a: poster.delete(a, oid, deleted=not undo),
                              "Restored on the server." if undo else
                              "Deleted on the server. The archive keeps what it saw.")

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

    @app.post("/t/{tid}/sync")
    def sync_now(request: Request, tid: int):
        bouncer.enqueue("sync", {"thread_id": tid})
        flash(request, "Re-check queued.")
        return RedirectResponse(f"/t/{tid}", status_code=303)

    @app.get("/t/{tid}", response_class=HTMLResponse)
    def thread(request: Request, tid: int, sort: str | None = None):
        if sort in COMMENT_SORTS:
            request.session["comment_sort"] = sort
        sort = request.session.get("comment_sort", "hot") if sort not in COMMENT_SORTS else sort
        with db.connect() as conn:
            t = conn.execute(
                "SELECT t.*, c.name AS cname, c.canonical_ap_id AS c_ap, c.id AS cid, "
                "f.retention_days FROM archived_threads t JOIN communities c ON c.id=t.community_id "
                "LEFT JOIN community_follows f ON f.community_id=c.id WHERE t.id=?", (tid,)).fetchone()
            if not t:
                raise HTTPException(404)
            objs = conn.execute(
                """SELECT o.*, a.username, a.instance AS a_instance, a.display_name, a.canonical_ap_id AS a_ap,
                          r.title, r.body, r.url, r.metadata_json AS rmeta
                   FROM objects o LEFT JOIN actors a ON a.id=o.author_id
                   JOIN revisions r ON r.object_id=o.id AND r.seq=o.revision_count
                   WHERE o.thread_id=? ORDER BY o.created_at, o.id""", (tid,)).fetchall()
            events: dict[int, list[Any]] = {}
            for e in conn.execute(
                "SELECT e.*, a.username AS actor_name, a.instance AS actor_instance FROM state_events e "
                "LEFT JOIN actors a ON a.id=e.actor_id JOIN objects o ON o.id=e.object_id "
                "WHERE o.thread_id=? AND e.event_type!='discovered' ORDER BY e.observed_at", (tid,)):
                events.setdefault(e["object_id"], []).append(e)
            thread_events = conn.execute("SELECT * FROM state_events WHERE thread_id=? AND object_id IS NULL "
                                         "ORDER BY id", (tid,)).fetchall()
            instance = conn.execute("SELECT * FROM instances WHERE domain=?", (t["source_domain"],)).fetchone()
            since = t["last_viewed_at"]
            conn.execute("UPDATE archived_threads SET prev_viewed_at=last_viewed_at, last_viewed_at=? WHERE id=?",
                         (utcnow(), tid))
        nodes = {o["id"]: {"o": o, "children": [], "events": events.get(o["id"], []),
                           "is_new": bool(since and o["discovered_late"] and o["first_seen_at"] > since),
                           "is_changed": bool(since and o["last_changed_at"] and o["last_changed_at"] > since)}
                 for o in objs}
        root, orphans = None, []
        for oid, n in nodes.items():
            o = n["o"]
            if o["object_type"] == "post":
                root = n
            elif o["parent_id"] in nodes:
                nodes[o["parent_id"]]["children"].append(n)
            else:
                orphans.append(n)
        if root is not None:
            root["children"].extend(orphans)
            sort_tree(root, sort)

        def count_descendants(n: dict[str, Any]) -> int:
            n["descendants"] = sum(1 + count_descendants(c) for c in n["children"])
            return n["descendants"]

        if root is not None:
            count_descendants(root)
        total_comments = sum(1 for o in objs if o["object_type"] == "comment")
        new_count = sum(1 for n in nodes.values() if n["is_new"])
        changed_count = sum(1 for n in nodes.values() if n["is_changed"] and not n["is_new"])
        md, media_for, preview = md_for(list(nodes))
        me = acting(request)
        my_votes = poster.my_votes(me, [o["canonical_ap_id"] for o in objs])
        return render(request, "thread.html", t=t, root=root, total_comments=total_comments, md=md,
                      media_for=media_for, preview=preview, my_votes=my_votes, me=me,
                      new_count=new_count, changed_count=changed_count, comment_sort=sort,
                      comment_sorts=COMMENT_SORTS,
                      thread_events=thread_events, instance=instance, since=since)

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
        with db.connect() as conn:
            rows = recent_changes(conn, limit=200)
        return render(request, "changes.html", changes=rows)

    app.state.bouncer = bouncer
    app.state.db = db
    return app
