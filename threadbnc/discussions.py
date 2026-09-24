"""Where else an article is being talked about: looked for when you open a
post of it, or the article itself, as a browser extension showing "discussions
of this page" would, and never in the background.

Each place is asked once, and not again for FRESH:

- Your own Lemmy (or PieFed) server: its posts of the link, which include every
  post federated to it, from communities you don't follow too. Asking your own
  server costs nobody else anything.
- Reddit, when it's connected: every post of the link, in any subreddit.
- Bluesky: posts linking to the page, most liked first. Bluesky only searches
  for someone signed in, so without your Bluesky account it's left out.
- A blog that federates (WordPress's, Ghost's or WriteFreely's ActivityPub; its
  page says so): the replies to the post, from wherever they were written.
- A page that takes webmentions through webmention.io: the replies and mentions
  it has collected (Bridgy sends Mastodon's and Bluesky's there).

Posts on Lemmy, PieFed, Reddit and Bluesky can be opened here (they're saved
like a post you open from a link, and expire like one); replies and mentions
open where they were written.

Each can be expanded in place (peek): its text, kept from when it was found,
then its replies, read when it's expanded (a Lemmy post from your own server),
not saved, and not read again for PEEK_FRESH.

Links are asked for by every address the article is known by (links.py), up to
MAX_ADDRESSES, since Lemmy and Reddit match links exactly."""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Callable
from urllib.parse import urlparse

import lxml.html
from lxml import etree

from . import articles
from . import links as links_mod
from .actor import AP_ACCEPT
from .adapters import (NComment, RemoteAuthError, RemoteError, RemoteNotFound, RemoteUnavailable, host_of,
                       parse_thread_url)
from .adapters.activitypub import title_from
from .adapters.bluesky import web_url
from .db import Conn, parse_ts, utcnow
from .media import MediaRejected, _assert_public_host

log = logging.getLogger(__name__)

JOB = "discussions"
FRESH = timedelta(hours=1)
MAX_ADDRESSES = 3
MAX_REPLIES = 20  # replies to a federating blog's post read, each from its own server
MAX_PAGES = 2  # pages of its replies collection
# Where each is from, in the order they're listed.
SOURCES = {"lemmy": "your Lemmy server", "reddit": "Reddit", "bluesky": "Bluesky", "activitypub": "the blog's replies",
           "webmention": "its webmentions"}
WEBMENTION_IO = "webmention.io"
OPENABLE = ("lemmy", "reddit", "bluesky")  # posts that can be saved and opened here; replies open where they are
ALL = "all"  # the check of every place at once, in discussion_checks
PEEK_FRESH = 300  # seconds an expanded discussion's replies are shown again without asking
MAX_PEEK_REPLIES = 100  # replies shown when one is expanded; the rest are there when it's opened


@dataclass
class Found:
    url: str
    title: str
    place: str | None = None
    author: str | None = None
    comments: int | None = None
    score: int | None = None
    created_at: str | None = None
    content: str | None = None  # its text, for its panel


def _text(html: str | None) -> str:
    if not html:
        return ""
    try:
        return " ".join(lxml.html.fragment_fromstring(html, create_parent="div").text_content().split())
    except (ValueError, etree.ParserError):
        return ""


def _first_url(value: Any) -> str | None:
    """An ActivityPub url (a string, a Link, or a list of them)."""
    for v in value if isinstance(value, list) else [value]:
        href = v.get("href") if isinstance(v, dict) else v
        if isinstance(href, str) and href.startswith(("http://", "https://")):
            return href
    return None


def _handle(actor_url: Any) -> str | None:
    """@name@server for an actor's address, as well as it can be told from it."""
    url = _first_url(actor_url)
    if not url:
        return None
    parsed = urlparse(url)
    m = re.search(r"/(?:@|users/|u/|author/|profile/)?([^/@]+)/?$", parsed.path)
    return f"@{m.group(1)}@{parsed.hostname}" if m else parsed.hostname


def _count(value: Any) -> int | None:
    n = value.get("totalItems") if isinstance(value, dict) else None
    return n if isinstance(n, int) else None


class Discussions:
    def __init__(self, bouncer: Any, lemmy_servers: Callable[[], list[str]], enabled: bool = True):
        self.bouncer, self.db = bouncer, bouncer.db
        self.lemmy_servers = lemmy_servers  # your own Lemmy/PieFed servers, the first asked
        self.enabled = enabled
        self.check_host = True
        self._peeked: dict[str, tuple[float, dict[str, Any]]] = {}  # url -> (when, what peek read)
        self._peek_lock = threading.Lock()
        bouncer.job_handlers[JOB] = self.run

    # -- asking ---------------------------------------------------------------------
    def request(self, article_id: int) -> int | None:
        """Look for discussions of an article unless that was done lately: the
        job doing it (one already waiting, or a new one), or None."""
        if not self.enabled:
            return None
        payload = json.dumps({"article_id": article_id})
        with self.db.connect() as conn:
            waiting = conn.execute("SELECT id FROM jobs WHERE kind=? AND payload_json=? AND status IN "
                                   "('queued', 'running') ORDER BY id DESC LIMIT 1", (JOB, payload)).fetchone()
            if waiting:
                return waiting["id"]
            last = conn.execute("SELECT MAX(checked_at) FROM discussion_checks WHERE article_id=?",
                                (article_id,)).fetchone()[0]
        if last and parse_ts(utcnow()) - parse_ts(last) < FRESH:  # type: ignore[operator]
            return None
        return self.bouncer.enqueue(JOB, {"article_id": article_id})

    def run(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self.db.connect() as conn:
            a = conn.execute("SELECT * FROM articles WHERE id=?", (payload["article_id"],)).fetchone()
            if a is None:
                return {"found": 0}
            ids = articles.same_page(conn, a["id"])
            same = conn.execute(f"SELECT * FROM articles WHERE id IN ({','.join('?' * len(ids))})", ids).fetchall()
            keys = set(articles.keys_of(conn, a["id"]))
        addresses = self.addresses(a, same)
        found = 0
        for source, look in (("lemmy", self._lemmy), ("reddit", self._reddit), ("bluesky", self._bluesky),
                             ("activitypub", self._activitypub), ("webmention", self._webmention)):
            if self.bouncer._stop.is_set():
                break
            error = None
            try:
                got = look(a, addresses, keys)
            except (RemoteError, MediaRejected) as exc:
                log.info("looking for discussions of %s on %s: %s", a["url"], source, exc)
                got, error = None, str(exc)
            if got is None and error is None:
                continue  # not a place to ask about this one
            self._save(a["id"], source, got, error)
            found += len(got or [])
        self._save(a["id"], ALL, None, None)  # when it was last looked for, even with nowhere to ask
        return {"article_id": a["id"], "found": found}

    @staticmethod
    def addresses(a: Any, same: list[Any]) -> list[str]:
        """The addresses to ask by, up to MAX_ADDRESSES: where the page says it
        lives, then the links it was posted with, then where they led. Lemmy
        and Reddit match links exactly, so the same page by another address
        counts as another."""
        urls = [a["canonical_url"], a["url"], a["fetched_from"]]
        urls += [r[c] for r in same for c in ("canonical_url", "url", "fetched_from")]
        return list(dict.fromkeys(u for u in urls if u and links_mod.key(u)))[:MAX_ADDRESSES]

    def _save(self, article_id: int, source: str, got: list[Found] | None, error: str | None) -> None:
        now = utcnow()
        with self.db.transaction() as conn:
            if got is not None:
                conn.execute("DELETE FROM discussions WHERE article_id=? AND source=?", (article_id, source))
                for f in {f.url: f for f in got}.values():
                    conn.execute(
                        "INSERT INTO discussions(article_id, source, url, title, place, author, comments, score, "
                        "created_at, found_at, content) VALUES (?,?,?,?,?,?,?,?,?,?,?) "
                        "ON CONFLICT(article_id, url) DO NOTHING",
                        (article_id, source, f.url, f.title, f.place, f.author, f.comments, f.score, f.created_at,
                         now, f.content))
            conn.execute("INSERT INTO discussion_checks(article_id, source, checked_at, error) VALUES (?,?,?,?) "
                         "ON CONFLICT(article_id, source) DO UPDATE SET checked_at=excluded.checked_at, "
                         "error=excluded.error", (article_id, source, now, error))

    # -- expanding one ------------------------------------------------------------------
    def peek(self, d: Any) -> dict[str, Any]:
        """A discussion found elsewhere, expanded: its text, then its replies,
        read now if it's a post that can be (see openable) and wasn't lately.
        Nothing is saved. A reply to a blog's post or a mention of the page has
        only its text: its own replies are where it was written."""
        text = {"body": None, "text": d["content"] or None}
        if d["source"] not in OPENABLE or not openable(d["url"]):
            # Found before its text was kept, its title is the start of it.
            return {"body": None, "text": d["content"] or d["title"] or None, "replies": None, "more": 0,
                    "error": None}
        with self._peek_lock:
            hit = self._peeked.get(d["url"])
        if hit and time.monotonic() - hit[0] < PEEK_FRESH:
            return hit[1]
        try:
            adapter, local = self._reader_for(d)
            post = adapter.fetch_post(local)
            comments = adapter.fetch_comments(local)
        except (RemoteError, ValueError) as exc:
            log.info("reading the discussion %s: %s", d["url"], exc)
            return {**text, "replies": None, "more": 0, "error": str(exc)}
        replies, more = reply_tree(comments)
        got = {"body": post.body or None, "text": None if post.body else text["text"], "replies": replies,
               "count": len(comments), "more": more, "error": None}
        with self._peek_lock:
            now = time.monotonic()
            self._peeked = {u: v for u, v in self._peeked.items() if now - v[0] < PEEK_FRESH}
            self._peeked[d["url"]] = (now, got)
        return got

    def _reader_for(self, d: Any) -> tuple[Any, str]:
        """The adapter to read a found post with, and its id there. A Lemmy
        post is read from your own server, where it was found: it has every
        comment federated to it, and asking it costs nobody else anything."""
        if d["source"] == "lemmy":
            servers = self.lemmy_servers()
            if not servers:
                raise RemoteUnavailable("No Lemmy server of your own to read it from")
            adapter = self.bouncer.adapter_for(servers[0])
            local = adapter.resolve_ap_id(d["url"])
            if not local:
                raise RemoteNotFound(f"{servers[0]} doesn't have it any more")
            return adapter, local
        ref = parse_thread_url(d["url"])
        adapter = self.bouncer.adapter_for(ref.domain)
        return adapter, adapter.resolve_url(ref)

    # -- the places -------------------------------------------------------------------
    @staticmethod
    def _matching(posts: list[Any], keys: set[str]) -> list[Any]:
        """Only posts of the article itself, whatever else a search turned up."""
        return [p for p in posts if links_mod.key(p.url) in keys]

    def _lemmy(self, a: Any, addresses: list[str], keys: set[str]) -> list[Found] | None:
        servers = self.lemmy_servers()
        if not servers:
            return None
        adapter = self.bouncer.adapter_for(servers[0])
        posts: dict[str, Any] = {}
        for url in addresses:
            for p in self._matching(adapter.posts_linking(url), keys):
                posts.setdefault(p.ap_id, p)
        return [Found(p.ap_id, p.title, f"!{p.community.name}@{p.community.domain}",
                      f"{p.author.username}@{p.author.domain}", p.comment_count, p.score, p.created_at, p.body)
                for p in posts.values()]

    def _reddit(self, a: Any, addresses: list[str], keys: set[str]) -> list[Found] | None:
        if not self.bouncer.reddit.config():
            return None
        adapter = self.bouncer.reddit_adapter
        posts: dict[str, Any] = {}
        for url in addresses:
            for p in self._matching(adapter.posts_linking(url), keys):
                posts.setdefault(p.ap_id, p)
        return [Found(p.ap_id, p.title, f"r/{p.community.name}", f"u/{p.author.username}", p.comment_count,
                      p.score, p.created_at, p.body) for p in posts.values()]

    def _bluesky(self, a: Any, addresses: list[str], keys: set[str]) -> list[Found] | None:
        if not addresses:
            return None
        adapter = self.bouncer.bluesky_adapter
        try:
            views = adapter.posts_linking(addresses[0])
        except RemoteAuthError:
            if adapter.reading_session and adapter.reading_session():
                raise  # signed in, and still refused: say so
            return None  # Bluesky only searches for someone signed in, and you aren't
        out = []
        for view in views:
            record = view.get("record") if isinstance(view.get("record"), dict) else {}
            author = view.get("author") or {}
            text = str(record.get("text") or "")
            out.append(Found(web_url(view["uri"]), title_from(text), "Bluesky",
                             f"@{author['handle']}" if author.get("handle") else None,
                             view.get("replyCount") if isinstance(view.get("replyCount"), int) else None,
                             view.get("likeCount") if isinstance(view.get("likeCount"), int) else None,
                             record.get("createdAt") or view.get("indexedAt"), text or None))
        return out

    def _ap_get(self, url: str) -> dict[str, Any]:
        """An ActivityPub object, signed by ThreadBNC's own actor when there is one."""
        if self.check_host:
            _assert_public_host(url)
        actor = self.bouncer.actor
        if actor is not None:
            return actor.fetch(url)
        resp = self.bouncer.http.send("GET", url, headers={"Accept": AP_ACCEPT})
        if resp.status_code in (404, 410):
            raise RemoteNotFound(f"{url}: HTTP {resp.status_code}")
        if resp.status_code >= 400:
            raise RemoteUnavailable(f"{url}: HTTP {resp.status_code}")
        try:
            data = resp.json()
        except ValueError as exc:
            raise RemoteUnavailable(f"{url}: not ActivityPub") from exc
        if not isinstance(data, dict):
            raise RemoteUnavailable(f"{url}: not an ActivityPub object")
        return data

    def _activitypub(self, a: Any, addresses: list[str], keys: set[str]) -> list[Found] | None:
        if not a["ap_url"] or not a["ap_url"].startswith("https://"):
            return None
        post = self._ap_get(a["ap_url"])
        replies = post.get("replies")
        items: list[Any] = []
        page: Any = self._ap_get(replies) if isinstance(replies, str) else replies
        for _ in range(MAX_PAGES):
            if not isinstance(page, dict):
                break
            items += page.get("orderedItems") or page.get("items") or []
            nxt = page.get("first") if not items else page.get("next")
            if len(items) >= MAX_REPLIES or not nxt:
                break
            page = self._ap_get(nxt) if isinstance(nxt, str) else nxt
        out = []
        for item in items[:MAX_REPLIES]:
            try:
                note = self._ap_get(item) if isinstance(item, str) else item
            except (RemoteError, MediaRejected):
                continue  # a reply that's gone, or a server that isn't answering: the rest still count
            if not isinstance(note, dict) or note.get("type") not in ("Note", "Article", "Page", "Question"):
                continue
            url = _first_url(note.get("url")) or _first_url(note.get("id"))
            if not url:
                continue
            text = _text(note.get("content")) or note.get("name") or ""
            out.append(Found(url, title_from(text), host_of(url), _handle(note.get("attributedTo")),
                             _count(note.get("replies")), _count(note.get("likes")), note.get("published"),
                             text or None))
        return out

    def _webmention(self, a: Any, addresses: list[str], keys: set[str]) -> list[Found] | None:
        endpoint = a["webmention_url"] or ""
        if urlparse(endpoint).hostname != WEBMENTION_IO or not addresses:
            return None
        data = self.bouncer.http.get_json(WEBMENTION_IO, "/api/mentions.jf2",
                                          {"target": addresses[0], "per-page": 50, "sort-dir": "down"})
        out = []
        for entry in (data or {}).get("children") or []:
            if not isinstance(entry, dict) or entry.get("wm-property") not in ("in-reply-to", "mention-of"):
                continue
            url = entry.get("url") or entry.get("wm-source")
            if not isinstance(url, str) or not url.startswith(("http://", "https://")):
                continue
            content = entry.get("content") or {}
            text = content.get("text") if isinstance(content, dict) else None
            author = entry.get("author") or {}
            out.append(Found(url, title_from(text or entry.get("name") or ""), host_of(url),
                             author.get("name") if isinstance(author, dict) else None,
                             created_at=entry.get("published") or entry.get("wm-received"), content=text or None))
        return out


# --- showing them --------------------------------------------------------------------------

def _tree(replies: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Replies (each with its `key` and its `parent`'s) as a tree, best first,
    the first MAX_PEEK_REPLIES of it, and how many more there are. One whose
    parent isn't there joins the top."""
    by_key = {r["key"]: r for r in replies}
    top: list[dict[str, Any]] = []
    for r in replies:
        r["children"] = []
    for r in replies:
        parent = by_key.get(r["parent"])
        (parent["children"] if parent is not None and parent is not r else top).append(r)
    shown = 0

    def trim(level: list[dict[str, Any]]) -> list[dict[str, Any]]:
        nonlocal shown
        kept = []
        for r in sorted(level, key=lambda r: (-(r["score"] or 0), r["created_at"] or "")):
            if shown >= MAX_PEEK_REPLIES:
                break
            shown += 1
            r["children"] = trim(r["children"])
            kept.append(r)
        return kept

    tree = trim(top)
    return tree, len(replies) - shown


def reply_tree(comments: list[NComment]) -> tuple[list[dict[str, Any]], int]:
    """A post's comments as read from its server, for its panel (see peek)."""
    return _tree([{"key": c.local_id, "parent": c.parent_local_id, "body": c.body, "created_at": c.created_at,
                   "score": c.score,
                   "author": f"{c.author.username}@{c.author.domain}" if c.author.domain else c.author.username,
                   "gone": "deleted" if c.deleted else "removed" if c.removed else None} for c in comments])


def saved_peek(conn: Conn, thread_id: int) -> dict[str, Any]:
    """A post saved here, expanded: its text and its comments as saved (its
    own page reads them again when it's opened). `object_ids`: for the saved
    copies of their pictures."""
    rows = conn.execute(
        "SELECT o.id, o.object_type, o.parent_id, o.created_at, o.score, o.cur_deleted, o.cur_removed, "
        "r.body, a.username, a.instance FROM objects o JOIN revisions r ON r.object_id=o.id "
        "AND r.seq=o.revision_count LEFT JOIN actors a ON a.id=o.author_id WHERE o.thread_id=? "
        "ORDER BY o.created_at, o.id", (thread_id,)).fetchall()
    post = next((r for r in rows if r["object_type"] == "post"), None)
    comments = [r for r in rows if r["object_type"] == "comment"]
    tree, more = _tree([{"key": r["id"], "parent": r["parent_id"], "body": r["body"], "created_at": r["created_at"],
                         "score": r["score"],
                         "author": f"{r['username']}@{r['instance']}" if r["instance"] else r["username"] or "someone",
                         "gone": "deleted" if r["cur_deleted"] else "removed" if r["cur_removed"] else None}
                        for r in comments])
    return {"body": post["body"] if post else None, "text": None, "replies": tree, "count": len(comments),
            "more": more, "error": None, "object_ids": [r["id"] for r in rows]}


def openable(url: str) -> bool:
    """A post that can be opened here (Lemmy, PieFed, Reddit, Bluesky)."""
    try:
        return parse_thread_url(url).kind == "post"
    except ValueError:
        return False


def for_article(conn: Conn, article_id: int, exclude_ap_ids: list[str] | None = None) -> dict[str, Any]:
    """What's known about where an article is discussed, for _discussions.html:
    what was found (by any article that's the same page), each with the thread
    here it's already saved as, and when each place was last asked."""
    ids = articles.same_page(conn, article_id)
    marks = ",".join("?" * len(ids))
    rows = conn.execute(f"SELECT * FROM discussions WHERE article_id IN ({marks}) "
                        f"ORDER BY COALESCE(comments, -1) DESC, COALESCE(score, -1) DESC, id", ids).fetchall()
    skip = set(exclude_ap_ids or [])
    found: dict[str, dict[str, Any]] = {}
    for r in rows:
        if r["url"] not in skip and r["url"] not in found:
            found[r["url"]] = dict(r)
    here: dict[str, int] = {}
    urls = list(found)
    for i in range(0, len(urls), 200):
        chunk = urls[i:i + 200]
        here.update((r["ap_id"], r["id"]) for r in conn.execute(
            f"SELECT o.canonical_ap_id AS ap_id, t.id FROM objects o JOIN archived_threads t ON t.root_object_id=o.id "
            f"AND t.trashed_at IS NULL WHERE o.canonical_ap_id IN ({','.join('?' * len(chunk))})", chunk))
    order = list(SOURCES)
    items = sorted(found.values(), key=lambda d: order.index(d["source"]) if d["source"] in order else 99)
    for d in items:
        d["thread_id"] = here.get(d["url"])
        d["openable"] = d["source"] in OPENABLE and openable(d["url"])
        n, one, many = d["comments"], *(("reply", "replies") if d["source"] in ("bluesky", "activitypub")
                                        else ("comment", "comments"))
        d["count"] = f"{n} {one if n == 1 else many}" if n is not None else None
    checks = conn.execute("SELECT source, MAX(checked_at) AS checked_at, MAX(error) AS error FROM discussion_checks "
                          f"WHERE article_id IN ({marks}) GROUP BY source", ids).fetchall()
    by_source = {c["source"]: dict(c) for c in checks}
    return {"items": items, "checks": {s: by_source[s] for s in SOURCES if s in by_source}}

