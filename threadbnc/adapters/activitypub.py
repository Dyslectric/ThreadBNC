"""Posts from anywhere on the fediverse (Mastodon, GoToSocial, Akkoma,
Misskey...), read the way their servers publish them: the ActivityPub object
at its own address, asked for with ThreadBNC's signature (actor.py).

These are the posts hashtags bring (tags.py), the posts of people you follow
(people.py), and the posts you make on your Mastodon account (accounts.py). A
thread's local id is "<key> <post's ActivityPub id>": what it's filed under,
and where to read it. The key is the hashtag, or the account's address for
someone followed and for your own posts; those are read back through their
server's Mastodon API, which needs no signature. Replies are
read when a post is opened, from the post's own server through the Mastodon
API that Mastodon, GoToSocial, Akkoma and Pleroma share, when its address says
which status it is, or through Pixelfed's or Loops's own (adapters/pixelfed.py,
loops.py) for theirs; elsewhere a post shows no comments, and none are taken
for gone."""

from __future__ import annotations

import html as htmllib
import re
import time
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Callable
from urllib.parse import urlparse

from .. import languages
from ..actor import is_public
from .base import (
    TAG_DOMAIN,
    TAG_PREFIX,
    CommentList,
    CommunityRef,
    NActor,
    NComment,
    NCommunity,
    NPost,
    RemoteAuthError,
    RemoteNotFound,
    ThreadiverseAdapter,
    ThreadRef,
    UnsupportedSoftware,
    host_of,
    is_fedi_account,
    is_fedi_account_ref,
)
from .rss import html_to_markdown

if TYPE_CHECKING:
    from ..actor import Actor

# Where Mastodon-style servers put a status's API id in its address:
# /users/name/statuses/123 (Mastodon, GoToSocial) or /@name/123 (its web page).
_STATUS_ID = [re.compile(r"^/users/[^/]+/statuses/([A-Za-z0-9]+)/?$"), re.compile(r"^/@[^/]+/([A-Za-z0-9]+)/?$")]
_LINK = re.compile(r"<a\s[^>]*>", re.I)
_HREF = re.compile(r'href="([^"]+)"', re.I)
_CLASS = re.compile(r'class="([^"]*)"', re.I)
TITLE_CHARS = 80
PEOPLE = ("Person", "Service", "Application", "Organization")  # actors whose posts can be followed
ACTOR_CACHE_SECONDS = 600.0  # following someone looks them up, then asks for their posts: once will do


def tag_community(tag: str, description: str | None = None) -> NCommunity:
    return NCommunity(ap_id=TAG_PREFIX + tag, name=tag, domain=TAG_DOMAIN, title=None, local_id=tag,
                      description=description)


def account_community(actor: str, description: str | None = None) -> NCommunity:
    """Your Mastodon account, as the community the posts you make on it are
    filed under; or someone else's, for their posts saved from Trending."""
    name = urlparse(actor).path.rstrip("/").rsplit("/", 1)[-1].lstrip("@")
    return NCommunity(ap_id=actor, name=name, domain=TAG_DOMAIN, title=None, local_id=actor,
                      description=description or "The posts you've made on your Mastodon account from ThreadBNC.")


def community_for(key: str) -> NCommunity:
    return account_community(key) if key.startswith("https://") else tag_community(key)


def person_community(doc: dict[str, Any]) -> NCommunity:
    """Someone followed on the fediverse (people.py), from their actor document."""
    actor = _id(doc.get("id")) or ""
    username = doc.get("preferredUsername") if isinstance(doc.get("preferredUsername"), str) else None
    about = plain_text(_text(doc.get("summary"))).strip()
    c = account_community(actor, about or None)
    return replace(c, name=username or c.name, title=(_text(doc.get("name")) or "").strip() or None,
                   description=about or f"Public posts by @{username or c.name}@{host_of(actor)}.")


def own_post(activity: Any, actor: str) -> dict[str, Any] | None:
    """The post a Create by `actor` carries, when it's one of theirs to follow:
    made by them on their own server, public or unlisted, and not a reply.
    Replies belong under the post they answer, not beside it in a feed (and
    their profile leaves them out too)."""
    if not isinstance(activity, dict) or activity.get("type") != "Create" or _id(activity.get("actor")) != actor:
        return None
    obj = activity.get("object")
    if not isinstance(obj, dict) or _id(obj.get("id")) is None or obj.get("type") == "Tombstone":
        return None
    if _id(obj.get("attributedTo")) != actor or host_of(obj["id"]) != host_of(actor):
        return None
    if not is_public(obj) or obj.get("inReplyTo"):
        return None
    return obj


def status_id(ap_id: str) -> str | None:
    """A post's id in its server's Mastodon API, when its address says."""
    return next((m.group(1) for p in _STATUS_ID if (m := p.match(urlparse(ap_id).path or ""))), None)


def _text(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):  # contentMap / nameMap: any language will do
        return next((v for v in value.values() if isinstance(v, str)), None)
    return None


def language_of(obj: dict[str, Any]) -> str | None:
    """The language a post says it's in: Lemmy's and PieFed's "language", or
    Mastodon's, which is the one key of its contentMap."""
    lang = obj.get("language")
    if isinstance(lang, dict):
        return languages.normalize(lang.get("identifier"))
    for key in ("contentMap", "nameMap"):
        if isinstance(obj.get(key), dict) and len(obj[key]) == 1:
            return languages.normalize(next(iter(obj[key])))
    return None


def _id(value: Any) -> str | None:
    if isinstance(value, dict):
        value = value.get("id")
    if isinstance(value, list):
        value = next((_id(v) for v in value if _id(v)), None)
    return value if isinstance(value, str) and value.startswith("https://") else None


def _count(value: Any) -> int | None:
    if isinstance(value, dict) and isinstance(value.get("totalItems"), int):
        return value["totalItems"]
    return None


def shared_link(html: str | None) -> str | None:
    """The link a post shares: the first in its text that isn't a mention or
    a hashtag (Mastodon marks those with classes), so it can be read like any
    linked article."""
    for tag in _LINK.findall(html or ""):
        href, cls = _HREF.search(tag), _CLASS.search(tag)
        classes = cls.group(1).split() if cls else []
        if href and "mention" not in classes and "hashtag" not in classes and href.group(1).startswith("http"):
            return href.group(1).replace("&amp;", "&")
    return None


def plain_text(html: str | None) -> str:
    """A post's text without markup, its paragraphs and line breaks kept.
    Tags go without a trace: Mastodon wraps a hashtag's name in a span."""
    text = re.sub(r"<br\s*/?>|</p>", "\n", html or "", flags=re.I)
    return htmllib.unescape(re.sub(r"<[^>]+>", "", text))


def title_from(text: str) -> str:
    """A short title for a post that has none: its first line, cut at a word."""
    line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    if len(line) <= TITLE_CHARS:
        return line or "(no text)"
    return line[:TITLE_CHARS].rsplit(" ", 1)[0].rstrip(",.;:") + "…"


def hashtags(obj: dict[str, Any]) -> list[str]:
    """A post's hashtags, as relays name them (lowercase, no #), in order."""
    out: list[str] = []
    tags = obj.get("tag")
    for t in tags if isinstance(tags, list) else [tags]:
        if isinstance(t, dict) and t.get("type") == "Hashtag" and isinstance(t.get("name"), str):
            name = re.sub(r"[\s-]+", "", t["name"].lstrip("#")).lower()
            if name and name not in out:
                out.append(name)
    return out


def account_avatar(account: dict[str, Any]) -> str | None:
    """A Mastodon API account's picture ("" for none; None when it's not an account)."""
    if not account:
        return None
    url = account.get("avatar_static") or account.get("avatar")
    return url if isinstance(url, str) else ""


def author_of(obj: dict[str, Any]) -> NActor:
    ap_id = _id(obj.get("attributedTo")) or _id(obj.get("actor")) or ""
    page = obj.get("url") if isinstance(obj.get("url"), str) else ""
    m = re.match(r"^/@([^/@]+)", urlparse(page).path or "")
    username = m.group(1) if m else (urlparse(ap_id).path.rstrip("/").rsplit("/", 1)[-1] or "?")
    return NActor(ap_id, username, host_of(ap_id))


def quote_markdown(s: dict[str, Any]) -> str | None:
    """The post a Mastodon API status quotes, as a quote under its text (None
    when it quotes nothing). Mastodon 4.5 nests it as {state, quoted_status};
    Fedibird and others put the status itself in `quote`. One that's pending,
    refused or gone shows as unavailable. Quotes of quotes aren't followed."""
    q = s.get("quote")
    if not isinstance(q, dict):
        return None
    inner = q.get("quoted_status") if "quoted_status" in q or "state" in q else q
    if not isinstance(inner, dict) or not inner.get("uri"):
        return "> (A quoted post that isn't available)"
    account = inner.get("account") or {}
    who = account.get("acct") or account.get("username") or "?"
    link = account.get("url") or account.get("uri") or ""
    head = f"**[@{who}]({link})**" if link.startswith("http") else f"**@{who}**"
    head += f" · [quoted post]({inner.get('url') or inner['uri']})"
    body = html_to_markdown(inner.get("content"), inner["uri"])
    if inner.get("spoiler_text"):
        body = f"**CW: {inner['spoiler_text']}**\n\n{body}"
    for m in inner.get("media_attachments") or []:
        if isinstance(m, dict) and m.get("url") and m.get("type") == "image":
            alt = (m.get("description") or "").replace("]", "").replace("\n", " ")
            body += f"\n\n![{alt}]({m['url']})"
    lines = [head, "", *body.split("\n")]
    return "\n".join(("> " + line).rstrip() for line in lines)


def with_quote(body: str | None, s: dict[str, Any]) -> str | None:
    """`body` followed by the post the status quotes, if any."""
    quote = quote_markdown(s)
    if quote is None:
        return body
    return f"{body}\n\n{quote}" if body else quote


def status_post(s: dict[str, Any], key: str) -> NPost:
    """A post as the Mastodon API has it (a status), filed under `key`."""
    uri = str(s["uri"])
    html = s.get("content")
    warning = (s.get("spoiler_text") or "").strip()
    pictures, video = [], None
    for m in s.get("media_attachments") or []:
        if not isinstance(m, dict) or not m.get("url"):
            continue
        if m.get("type") == "image":
            pictures.append(m["url"])
        elif m.get("type") in ("video", "gifv", "audio") and video is None:
            video = m["url"]
    meta: dict[str, Any] = {} if warning else {"untitled": True}
    if s.get("sensitive"):
        meta["nsfw" if not warning else "spoiler"] = True
    if warning:
        meta["content_warning"] = warning
    account = s.get("account") or {}
    actor = account.get("uri") or account.get("url") or ""
    likes = s.get("favourites_count") if isinstance(s.get("favourites_count"), int) else None
    return NPost(
        ap_id=uri,
        local_id=f"{key} {uri}",
        title=warning or title_from(plain_text(html)),
        body=with_quote(html_to_markdown(html, uri) or None, s),
        url=shared_link(html) or video,
        created_at=s.get("created_at"),
        updated_at=s.get("edited_at"),
        deleted=False, removed=False, locked=False,
        community=community_for(key),
        author=NActor(actor, account.get("username") or "?", host_of(actor), account.get("display_name") or None,
                      account_avatar(account)),
        metadata=meta,
        score=likes, upvotes=likes,
        comment_count=s.get("replies_count") if isinstance(s.get("replies_count"), int) else None,
        thumbnail_url=pictures[0] if pictures else None,
        gallery=pictures if len(pictures) > 1 else [],
        language=languages.normalize(s.get("language")),
    )


class ActivityPubAdapter(ThreadiverseAdapter):
    software = "activitypub"
    poll_page_size = 20
    max_poll_pages = 1

    def __init__(self, actor: Actor | None, http: Any = None, bluesky: bool = False):
        """`bluesky`: hashtags are also picked out of Bluesky (jetstream.py), so
        they can be followed without an actor of ThreadBNC's own."""
        super().__init__(TAG_DOMAIN)
        self.actor = actor
        self.http = actor.http if actor else http
        self.bluesky = bluesky
        # Public streams can supply hashtags without ThreadBNC's own actor.
        self.mastodon: Callable[[], bool] = lambda: False
        self.fedibuzz: Callable[[], bool] = lambda: False
        self._people: dict[str, tuple[dict[str, Any], float]] = {}  # name or address -> (actor document, when)

    def _need_actor(self, what: str = "hashtags") -> Actor:
        if self.actor is None:
            raise RemoteAuthError(f"Following {what} on the fediverse needs ThreadBNC's own ActivityPub identity: "
                                  "set THREADBNC_ACTOR_DOMAIN (see README, \"Hashtags\").")
        return self.actor

    # -- people ------------------------------------------------------------------
    def person(self, name: str) -> dict[str, Any]:
        """Someone's actor document, by @name@server or their address. A
        profile page's address (https://server/@name) is looked up by
        WebFinger as @name@server, as it's only a web page on some servers."""
        cached = self._people.get(name)
        if cached and time.monotonic() - cached[1] < ACTOR_CACHE_SECONDS:
            return cached[0]
        actor = self._need_actor("people")
        page = re.match(r"^https://([^/]+)/@([^/@]+)$", name)
        handle = name if name.startswith("@") else f"@{page.group(2)}@{page.group(1)}" if page else None
        url: str | None = name if handle is None else None
        if handle:
            user, _, server = handle[1:].partition("@")
            try:
                jrd = self.http.get_json(server, "/.well-known/webfinger", params={"resource": f"acct:{user}@{server}"})
            except RemoteNotFound:
                raise RemoteNotFound(f"{server} doesn't know {handle}") from None
            links = jrd.get("links") if isinstance(jrd, dict) else None
            url = next((link["href"] for link in links or [] if isinstance(link, dict) and link.get("rel") == "self"
                        and "json" in str(link.get("type")) and isinstance(link.get("href"), str)), None)
            if not url:
                raise RemoteNotFound(f"{server} has no fediverse account {handle}")
        doc = actor.fetch(url)  # type: ignore[arg-type]
        ap_id = _id(doc.get("id"))
        if ap_id is None or host_of(ap_id) != host_of(url or ""):
            raise RemoteNotFound(f"{url} answered with someone from elsewhere")
        if doc.get("type") not in PEOPLE or not is_fedi_account(ap_id) or not _id(doc.get("inbox")):
            raise UnsupportedSoftware(f"{handle or url} isn't an account whose posts can be followed here "
                                      f"(it's a {doc.get('type') or 'thing'} at {ap_id})")
        for key in (name, ap_id):
            self._people[key] = (doc, time.monotonic())
        return doc

    def outbox_posts(self, actor_id: str, limit: int = 20) -> list[NPost]:
        """Someone's latest posts, from the first page of their outbox (the
        posts their profile shows, public ones only): read when they're
        followed, and for their page's live view."""
        actor = self._need_actor("people")
        outbox = _id(self.person(actor_id).get("outbox"))
        if outbox is None or host_of(outbox) != host_of(actor_id):
            return []
        box = actor.fetch(outbox)
        first = box.get("first")
        if isinstance(first, str) and first.startswith("https://") and host_of(first) == host_of(actor_id):
            box = actor.fetch(first)
        elif isinstance(first, dict):
            box = first
        items = box.get("orderedItems") or box.get("items") or []
        posts = []
        for item in items if isinstance(items, list) else []:
            obj = own_post(item, actor_id)
            if obj is not None:
                posts.append(self.post_from(obj, actor_id))
            if len(posts) >= limit:
                break
        return posts

    # -- communities: a hashtag has nothing to list; its posts are passed on as they're made
    def fetch_community(self, ref: CommunityRef) -> NCommunity:
        if is_fedi_account_ref(ref):
            return person_community(self.person(ref.name))
        timeline, firehose = self.mastodon(), self.fedibuzz()
        if not self.bluesky and not timeline and not firehose:
            self._need_actor()
        fediverse = ("FediBuzz's public stream" if firehose
                     else "your Mastodon server's public timeline" if timeline
                     else "across the fediverse, as a relay passes them on" if self.actor else None)
        where = " and ".join(x for x in (fediverse,
                                         "Bluesky, picked out of everything posted there" if self.bluesky else None) if x)
        return tag_community(ref.name, f"Public posts tagged #{ref.name} from {where}.")

    def list_community_posts(self, ref: CommunityRef, sort: str = "New", page: int = 1,
                             limit: int = 20) -> list[NPost]:
        """Someone followed: their latest posts (one page, however many are
        asked for). A hashtag: nothing, its posts arrive as they're made."""
        if not is_fedi_account_ref(ref) or page > 1:
            return []
        return self.outbox_posts(_id(self.person(ref.name).get("id")) or "", limit)

    def resolve_url(self, ref: ThreadRef) -> str:
        raise RemoteNotFound("Posts from hashtags arrive by following the hashtag")

    def resolve_ap_id(self, ap_id: str) -> str | None:
        return None

    # -- posts -------------------------------------------------------------------
    def fetch_object(self, ap_id: str) -> dict[str, Any]:
        """The post as its own server has it. It must say it's the post asked
        for, from that same server, or it's not believed."""
        obj = self._need_actor().fetch(ap_id)
        if obj.get("type") == "Tombstone":  # deleted: what 404 and 410 say too
            raise RemoteNotFound(f"{ap_id} was deleted")
        if _id(obj.get("id")) is None or host_of(obj["id"]) != host_of(ap_id):
            raise RemoteNotFound(f"{ap_id} answered with something from elsewhere")
        return obj

    def _quoted(self, obj: dict[str, Any]) -> str | None:
        """The post `obj` quotes (its quote, quoteUrl or quoteUri, or Misskey's
        _misskey_quote), read from its own server, as a quote under the text."""
        target = next((u for k in ("quote", "quoteUrl", "quoteUri", "_misskey_quote") if (u := _id(obj.get(k)))), None)
        if target is None:
            return None
        try:
            got = self.fetch_object(target)
        except (RemoteNotFound, RemoteAuthError, UnsupportedSoftware):
            return "> (A quoted post that isn't available)"
        who = author_of(got)
        page = got.get("url") if isinstance(got.get("url"), str) else target
        head = f"**[@{who.username}]({who.ap_id})** · [quoted post]({page})" if who.ap_id else f"[quoted post]({page})"
        text = html_to_markdown(_text(got.get("content")) or _text(got.get("contentMap")), target)
        return "\n".join(("> " + line).rstrip() for line in [head, "", *text.split("\n")])

    def post_from(self, obj: dict[str, Any], key: str, ap_id: str | None = None) -> NPost:
        """A post filed under `key`: a hashtag, or the account it's followed from."""
        ap_id = _id(obj.get("id")) or ap_id or ""
        html = _text(obj.get("content")) or _text(obj.get("contentMap"))
        quote = self._quoted(obj)
        body = html_to_markdown(html, ap_id)
        if quote:
            body = f"{body}\n\n{quote}" if body else quote
        warning = (_text(obj.get("summary")) or "").strip()  # a content warning
        name = (_text(obj.get("name")) or "").strip()  # Articles and polls have a title of their own
        pictures, video = [], None
        for a in obj.get("attachment") or []:
            if not isinstance(a, dict):
                continue
            url = a.get("url") if isinstance(a.get("url"), str) else _id(a.get("url"))
            kind = str(a.get("mediaType") or "")
            if url and (kind.startswith("image/") or a.get("type") == "Image"):
                pictures.append(url)
            elif url and kind.startswith(("video/", "audio/")) and video is None:
                video = url
        meta: dict[str, Any] = {"untitled": True} if not name and not warning else {}
        if obj.get("sensitive"):
            meta["nsfw" if not warning else "spoiler"] = True
        if warning:
            meta["content_warning"] = warning
        if obj.get("type") != "Note":
            meta["ap_type"] = obj.get("type")
        return NPost(
            ap_id=ap_id,
            local_id=f"{key} {ap_id}",
            title=name or warning or title_from(plain_text(html)),
            body=body or None,
            url=shared_link(html) or video,
            created_at=_text(obj.get("published")),
            updated_at=_text(obj.get("updated")),
            deleted=False, removed=False, locked=False,
            community=community_for(key),
            author=author_of(obj),
            metadata=meta,
            score=_count(obj.get("likes")),
            upvotes=_count(obj.get("likes")),
            comment_count=_count(obj.get("replies")),
            thumbnail_url=pictures[0] if pictures else None,
            gallery=pictures if len(pictures) > 1 else [],
            language=language_of(obj),
        )

    def fetch_post(self, local_id: str) -> NPost:
        key, _, ap_id = local_id.partition(" ")
        if key.startswith("https://"):  # yours, or someone's followed: read back as their server shows anyone
            sid = status_id(ap_id)
            if sid is not None and self.http is not None:
                try:
                    return status_post(self.http.get_json(host_of(ap_id), f"/api/v1/statuses/{sid}"), key)
                except RemoteAuthError:  # its API is for its own users: ask as a server would
                    if self.actor is None:
                        raise
            if self.actor is None:
                raise RemoteNotFound(f"{ap_id} can't be read back")
            return self.post_from(self.fetch_object(ap_id), key, ap_id)
        if self.actor is None:  # from your server's public timeline: read as its own server shows anyone
            sid = status_id(ap_id)
            if sid is None or self.http is None:
                raise UnsupportedSoftware(f"{ap_id} can only be read again with ThreadBNC's own ActivityPub "
                                          "identity (THREADBNC_ACTOR_DOMAIN)")
            return status_post(self.http.get_json(host_of(ap_id), f"/api/v1/statuses/{sid}"), key)
        return self.post_from(self.fetch_object(ap_id), key, ap_id)

    # -- replies -----------------------------------------------------------------
    def fetch_comments(self, post_local_id: str) -> CommentList:
        """The replies the post's own server knows of, through its Mastodon API
        (or Pixelfed's or Loops's). It's never the complete tree (servers only
        know the replies that reached them), so nothing missing from it is
        taken for deleted."""
        _, _, ap_id = post_local_id.partition(" ")
        elsewhere = media_post(ap_id, self.http) if self.http is not None else None
        if elsewhere is not None:
            adapter, local = elsewhere
            try:
                found = adapter.fetch_comments(local)
            except (RemoteNotFound, RemoteAuthError):
                found = CommentList()
            found.complete = False
            return found
        status = status_id(ap_id)
        if status is None or self.http is None:
            return context_comments(None, "")
        try:
            context = self.http.get_json(host_of(ap_id), f"/api/v1/statuses/{status}/context")
        except RemoteNotFound:
            return context_comments(None, status)
        except RemoteAuthError:  # the server doesn't show replies to visitors
            return context_comments(None, status)
        return context_comments(context, status)


def media_post(ap_id: str, http: Any) -> tuple[ThreadiverseAdapter, str] | None:
    """A Pixelfed post's (/p/<name>/<id>) or Loops video's (/ap/users/<id>/video/<id>)
    reader on its own server, and its local id there, when its address is one."""
    from .loops import LoopsAdapter
    from .pixelfed import PixelfedAdapter

    domain = urlparse(ap_id).netloc.lower()
    for adapter in (PixelfedAdapter(domain, http), LoopsAdapter(domain, http)):
        local = adapter.resolve_ap_id(ap_id)
        if local:
            return adapter, local
    return None


def context_comments(context: Any, status: str) -> CommentList:
    """The replies in a Mastodon API status context (/api/v1/statuses/<id>/context)
    of the status `status`, as comments. Never the complete tree: a server
    only knows the replies that reached it, so nothing missing is taken for deleted."""
    out = CommentList()
    out.complete = False
    replies = context.get("descendants") if isinstance(context, dict) else None
    uris: dict[str, str | None] = {status: None}
    for s in replies or []:
        if isinstance(s, dict) and s.get("id") and s.get("uri"):
            uris[str(s["id"])] = s["uri"]
    for s in replies or []:
        if not isinstance(s, dict) or not s.get("uri"):
            continue
        account = s.get("account") or {}
        body = html_to_markdown(s.get("content"), s["uri"])
        if s.get("spoiler_text"):
            body = f"**CW: {s['spoiler_text']}**\n\n{body}"
        for m in s.get("media_attachments") or []:
            if isinstance(m, dict) and m.get("url"):
                alt = (m.get("description") or "").replace("]", "")
                body += f"\n\n![{alt}]({m['url']})" if m.get("type") == "image" else f"\n\n[{m['type']}]({m['url']})"
        body = with_quote(body, s) or ""
        acct_uri = account.get("uri") or account.get("url") or ""
        out.append(NComment(
            ap_id=s["uri"],
            local_id=s["uri"],
            parent_local_id=uris.get(str(s.get("in_reply_to_id"))),
            body=body or None,
            created_at=s.get("created_at"),
            updated_at=s.get("edited_at"),
            deleted=False, removed=False,
            author=NActor(acct_uri, account.get("username") or "?", host_of(acct_uri),
                          account.get("display_name") or None, account_avatar(account)),
            score=s.get("favourites_count"),
            upvotes=s.get("favourites_count"),
            reply_count=s.get("replies_count"),
        ))
    return out
