"""Bluesky accounts and custom feeds, followed like communities.

Everything is read from Bluesky's public API (public.api.bsky.app), with no
account: the posts a followed account makes (not its replies or reposts), or
what a custom feed lists; a post's replies when it's opened, as its comments;
and its likes, as its votes, from each check of the account or feed.

Signed in with your own account (an app password; see accounts.py), you can
like, repost, quote, reply, post and delete your own posts; replies, mentions
and quotes of you arrive in the Inbox; your Following timeline can be followed
like a feed; and feeds that are only shown to someone signed in are read as you. Writes go to your account's own server
(its PDS, found from its DID document when you log in), and signed-in reads
go through it to Bluesky's AppView, as the app does. The session (both of its
tokens, and the PDS) is kept encrypted like any account's, as JSON; its short-
lived access token is refreshed before it runs out (`fresh`).

Bluesky posts have no titles. Like a Mastodon post's, one's title is the start
of its text, and its text is shown once (the "untitled" metadata). Pictures
become its gallery, a link card its link, and a quoted post a quote under the
text. Videos come as a stream, not a file, so only their cover picture is kept.

Ids are bsky.app addresses (see base.py): accounts https://bsky.app/profile/<did>,
feeds .../feed/<name>, posts .../post/<rkey>. A post's local id is the account
or feed it arrived through (a followed "community" name) and its at:// address,
so reading it again keeps it where it was.
"""

from __future__ import annotations

import base64
import json
import re
import time
from typing import Any, Callable
from urllib.parse import quote, urlencode, urlparse

from ..db import fmt_ts, parse_ts, utcnow
from ..render import escape_markdown, plain_lines
from .activitypub import title_from
from .base import (
    BSKY_DOMAIN,
    CommentList,
    CommunityRef,
    NActor,
    NComment,
    NCommunity,
    NInboxItem,
    NPost,
    RemoteAuthError,
    RemoteNotFound,
    RemoteRejected,
    RemoteUnavailable,
    ThreadiverseAdapter,
    ThreadRef,
)

API = "public.api.bsky.app"
APPVIEW = "did:web:api.bsky.app#bsky_appview"  # where a PDS sends signed-in reads on (atproto-proxy)
ENTRYWAY = "https://bsky.social"  # signs in accounts hosted by Bluesky when only an email is given
PLC = "https://plc.directory"
LIKE = "app.bsky.feed.like"
REPOST = "app.bsky.feed.repost"
CARD_THUMB = "https://cdn.bsky.app/img/feed_thumbnail/plain/{did}/{cid}@jpeg"  # where an uploaded card picture shows
TIMELINE = "/timeline"  # a followed name ending in this is your own Following timeline
POST_CHARS = 300  # Bluesky's limit, in graphemes; counted here in characters
REFRESH_BEFORE = 300  # seconds before the access token runs out that it's refreshed
_AUTH_ERRORS = {"ExpiredToken", "InvalidToken", "AuthMissing", "AuthenticationRequired", "AuthFactorTokenRequired",
                "AccountTakedown"}
_WRITE_URL = re.compile(r"https?://[^\s<>\"]+[^\s<>\".,;:!?)\]'’]")
_WRITE_MENTION = re.compile(r"(?<![\w@])@([a-zA-Z0-9-]+(?:\.[a-zA-Z0-9-]+)+)")
_WRITE_TAG = re.compile(r"(?<![\w#&])#([^\s#.,;:!?()\[\]{}\"']{1,64})")
POST = "app.bsky.feed.post"
GENERATOR = "app.bsky.feed.generator"
THREAD_VIEW = "app.bsky.feed.defs#threadViewPost"
REPLY_DEPTH = 10  # how deep a post's replies are read when it's opened
# Labels that mean a post's pictures are blurred until tapped, as tiles do for NSFW posts.
NSFW_LABELS = {"porn", "sexual", "nudity", "graphic-media", "gore"}

_AT = re.compile(r"^at://([^/]+)/([^/]+)/([^/]+)$")


def profile_url(did: str) -> str:
    return f"https://{BSKY_DOMAIN}/profile/{did}"


def web_url(uri: str) -> str:
    """A post's or feed's bsky.app address, from its at:// one."""
    m = _AT.match(uri)
    if not m:
        return uri
    did, kind, rkey = m.groups()
    return f"{profile_url(did)}/{'feed' if kind == GENERATOR else 'post'}/{rkey}"


def _link(url: str) -> str:
    return url.replace(" ", "%20").replace("(", "%28").replace(")", "%29").replace("<", "%3C").replace(">", "%3E")


def rich_markdown(text: str | None, facets: Any = None) -> str | None:
    """A post's text as Markdown that shows as written, its links, mentions
    and hashtags (facets, which point into the text by UTF-8 byte offsets)
    made links."""
    data = (text or "").encode("utf-8")
    spans: list[tuple[int, int, str]] = []
    for f in facets if isinstance(facets, list) else []:
        index = f.get("index") if isinstance(f, dict) else None
        start, end = (index or {}).get("byteStart"), (index or {}).get("byteEnd")
        if not isinstance(start, int) or not isinstance(end, int) or not 0 <= start < end <= len(data):
            continue
        for feature in f.get("features") or []:
            kind = str(feature.get("$type") or "") if isinstance(feature, dict) else ""
            target = None
            if kind.endswith("#link") and str(feature.get("uri") or "").startswith(("http://", "https://")):
                target = feature["uri"]
            elif kind.endswith("#mention") and feature.get("did"):
                target = profile_url(feature["did"])
            elif kind.endswith("#tag") and feature.get("tag"):
                target = f"https://{BSKY_DOMAIN}/hashtag/{quote(str(feature['tag']))}"
            if target:
                spans.append((start, end, target))
                break
    pieces, at = [], 0
    for start, end, target in sorted(spans):
        if start < at:  # overlapping facets: the first wins
            continue
        pieces.append(escape_markdown(data[at:start].decode("utf-8", "replace")))
        label = escape_markdown(data[start:end].decode("utf-8", "replace")).replace("\n", " ")
        pieces.append(f"[{label}]({_link(target)})")
        at = end
    pieces.append(escape_markdown(data[at:].decode("utf-8", "replace")))
    return plain_lines("".join(pieces).strip().split("\n"))


def facets_for(text: str, did_of: Callable[[str], str | None]) -> list[dict[str, Any]]:
    """The links, @mentions and #hashtags in something you write, as Bluesky
    marks them (UTF-8 byte offsets), so they're links there too. A mention of
    a handle that isn't anyone's stays plain text."""
    out: list[dict[str, Any]] = []

    def span(m: re.Match[str], feature: dict[str, Any]) -> None:
        start = len(text[:m.start()].encode("utf-8"))
        out.append({"index": {"byteStart": start, "byteEnd": start + len(m.group(0).encode("utf-8"))},
                    "features": [feature]})

    for m in _WRITE_URL.finditer(text):
        span(m, {"$type": "app.bsky.richtext.facet#link", "uri": m.group(0)})
    links = [(f["index"]["byteStart"], f["index"]["byteEnd"]) for f in out]

    def in_link(m: re.Match[str]) -> bool:
        start = len(text[:m.start()].encode("utf-8"))
        return any(a <= start < b for a, b in links)

    for m in _WRITE_MENTION.finditer(text):
        did = None if in_link(m) else did_of(m.group(1).lower())
        if did:
            span(m, {"$type": "app.bsky.richtext.facet#mention", "did": did})
    for m in _WRITE_TAG.finditer(text):
        if not in_link(m) and not m.group(1).isdigit():
            span(m, {"$type": "app.bsky.richtext.facet#tag", "tag": m.group(1)})
    return sorted(out, key=lambda f: f["index"]["byteStart"])


def _when(value: Any) -> str | None:
    parsed = parse_ts(value) if isinstance(value, str) else None
    return fmt_ts(parsed) if parsed else None


def author_of(profile: dict[str, Any]) -> NActor:
    did = str(profile.get("did") or "")
    return NActor(profile_url(did), str(profile.get("handle") or did or "?"), BSKY_DOMAIN,
                  profile.get("displayName") or None)


class _Content:
    """What a post (or a reply, or a quoted post) says and shows."""

    def __init__(self, record: dict[str, Any], embed: dict[str, Any] | None):
        self.text = str(record.get("text") or "")
        self.body = rich_markdown(self.text, record.get("facets"))
        self.pictures: list[str] = []
        self.alts: list[str] = []
        self.link: str | None = None
        self.link_title: str | None = None
        self.cover: str | None = None  # a link card's picture, or a video's
        self.video = False
        self.quote: dict[str, Any] | None = None
        embed = embed if isinstance(embed, dict) else {}
        kind = str(embed.get("$type") or "")
        media = embed
        if kind.startswith("app.bsky.embed.recordWithMedia"):
            media = embed.get("media") if isinstance(embed.get("media"), dict) else {}
            self.quote = (embed.get("record") or {}).get("record")
        elif kind.startswith("app.bsky.embed.record"):
            self.quote = embed.get("record")
        kind = str(media.get("$type") or "")
        if kind.startswith("app.bsky.embed.images"):
            for image in media.get("images") or []:
                if isinstance(image, dict) and str(image.get("fullsize") or "").startswith("https://"):
                    self.pictures.append(image["fullsize"])
                    self.alts.append(str(image.get("alt") or ""))
        elif kind.startswith("app.bsky.embed.external"):
            card = media.get("external") or {}
            if str(card.get("uri") or "").startswith(("http://", "https://")):
                self.link, self.link_title = card["uri"], card.get("title") or None
                self.cover = card.get("thumb") or None
        elif kind.startswith("app.bsky.embed.video"):
            self.video = True
            self.cover = media.get("thumbnail") or None

    def markdown(self, inline_media: bool) -> str | None:
        """The text, then (for replies, which have no gallery) the pictures and
        link, then any quoted post."""
        parts = [self.body] if self.body else []
        if inline_media:
            for url, alt in zip(self.pictures, self.alts):
                parts.append(f"![{escape_markdown(alt).replace(chr(10), ' ')}]({_link(url)})")
            if self.link:
                parts.append(f"[{escape_markdown(self.link_title or self.link)}]({_link(self.link)})")
            if self.video and self.cover:
                parts.append(f"![Video]({_link(self.cover)})")
        if self.quote is not None:
            parts.append(_quote_markdown(self.quote))
        return "\n\n".join(parts) or None


def _quote_markdown(rec: dict[str, Any]) -> str:
    if not isinstance(rec, dict) or not isinstance(rec.get("value"), dict) or not rec.get("uri"):
        return "> (A quoted post that isn't available)"
    author = author_of(rec.get("author") or {})
    embeds = rec.get("embeds") or []
    inner = _Content(rec["value"], embeds[0] if embeds else None)
    inner.quote = None  # quotes of quotes aren't followed
    lines = [f"**[@{escape_markdown(author.username)}]({_link(author.ap_id)})** · [quoted post]({_link(web_url(rec['uri']))})"]
    lines += ["", *(inner.markdown(True) or "").split("\n")]
    return "\n".join(("> " + line).rstrip() for line in lines)


class BlueskyAdapter(ThreadiverseAdapter):
    software = "bluesky"
    poll_page_size = 50
    max_poll_pages = 3

    def __init__(self, http: Any):
        super().__init__(BSKY_DOMAIN)
        self.http = http
        self._dids: dict[str, str] = {}  # handle -> DID
        self._communities: dict[str, NCommunity] = {}  # followed name -> the account or feed
        self._cursors: dict[tuple[str, int], str] = {}  # (name, page) -> where that page starts
        # Your signed-in session, fresh, for feeds only shown to someone signed
        # in (accounts.py sets it); None when you haven't signed in.
        self.reading_session: Callable[[], str | None] | None = None

    def _get(self, method: str, **params: Any) -> Any:
        data = self.http.get_json(API, f"/xrpc/{method}", params)
        if not isinstance(data, dict):
            raise RemoteNotFound(f"{API}: {method} answered with something unexpected")
        return data

    # -- as your account ---------------------------------------------------------
    def _xrpc(self, base: str, method: str, *, bearer: str | None = None, params: dict[str, Any] | None = None,
              body: dict[str, Any] | None = None, appview: bool = False,
              raw: tuple[bytes, str] | None = None) -> Any:
        """One call to a PDS (or the entryway): a query with `params`, or a
        procedure (POST) with `body`, or with `raw` (bytes, their type) for an
        upload. Bluesky's errors come as {error, message}."""
        url = f"{base.rstrip('/')}/xrpc/{method}"
        if params:
            url += "?" + urlencode({k: v for k, v in params.items() if v is not None}, doseq=True)
        headers = {"Accept": "application/json"}
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"
        if appview:
            headers["atproto-proxy"] = APPVIEW
        content = None
        if raw is not None:
            content, headers["Content-Type"] = raw
            body = {}  # a procedure, like any other POST
        elif body:
            # An empty body is a procedure that takes no input (refreshSession,
            # deleteSession): Bluesky refuses one sent anyway, even `{}`.
            # Bluesky refuses a null where it expects a value: leave unset fields out.
            headers["Content-Type"] = "application/json"
            content = json.dumps({k: v for k, v in body.items() if v is not None}).encode()
        # Asked for while you wait (signing in, liking, replying), so not spaced out
        # like background reads; a server that asked us to wait is still left alone.
        resp = self.http.send("POST" if body is not None else "GET", url, headers=headers, content=content,
                              throttle=False)
        try:
            data = resp.json() if resp.content else {}
        except ValueError:
            data = {}
        if resp.status_code == 200:
            return data
        error = str(data.get("error") or "") if isinstance(data, dict) else ""
        message = str(data.get("message") or error or f"HTTP {resp.status_code}") if isinstance(data, dict) else ""
        host = urlparse(url).hostname
        if resp.status_code == 401 or error in _AUTH_ERRORS:
            raise RemoteAuthError(f"{host}: {message}", error or "AuthenticationRequired")
        if resp.status_code == 404 or error in ("NotFound", "RecordNotFound", "PostNotFound"):
            raise RemoteNotFound(f"{host}: {message}")
        if resp.status_code >= 500:
            raise RemoteUnavailable(f"{host}: {message}")
        if body is not None:
            raise RemoteRejected(f"Bluesky refused: {message}", error)
        raise RemoteUnavailable(f"{host}: {message}")

    @staticmethod
    def _session(token: str) -> dict[str, str]:
        try:
            s = json.loads(token)
            assert s["did"] and s["pds"] and s["access"] and s["refresh"]
        except (ValueError, KeyError, TypeError, AssertionError) as exc:
            raise RemoteAuthError("The saved Bluesky session isn't readable; log in again", "InvalidToken") from exc
        return s

    def _pds_of(self, did: str) -> str:
        """Where an account lives: its PDS, from its DID document."""
        if did.startswith("did:plc:"):
            resp = self.http.send("GET", f"{PLC}/{did}", headers={"Accept": "application/json"})
        elif did.startswith("did:web:"):
            resp = self.http.send("GET", f"https://{did[len('did:web:'):]}/.well-known/did.json",
                                  headers={"Accept": "application/json"})
        else:
            raise RemoteNotFound(f"{did} isn't a kind of account ThreadBNC knows")
        if resp.status_code != 200:
            raise RemoteUnavailable(f"Couldn't read where {did} lives: HTTP {resp.status_code}")
        for service in (resp.json().get("service") or []):
            if str(service.get("id", "")).endswith("#atproto_pds"):
                return str(service["serviceEndpoint"]).rstrip("/")
        raise RemoteNotFound(f"{did} has no server listed")

    def login(self, username: str, password: str, totp: str | None = None) -> str:
        """Sign in with a handle (or the email of an account Bluesky hosts) and
        an app password, at the account's own PDS. `totp`: the code Bluesky
        emails when a main password is used with two-factor sign-in on."""
        who = username.strip().lstrip("@")
        pds = ENTRYWAY if "@" in who else self._pds_of(self._did(who.lower()))
        got = self._xrpc(pds, "com.atproto.server.createSession",
                         body={"identifier": who, "password": password, "authFactorToken": totp or None})
        doc_pds = next((str(s.get("serviceEndpoint")) for s in (got.get("didDoc") or {}).get("service") or []
                        if str(s.get("id", "")).endswith("#atproto_pds")), None)
        return json.dumps({"did": got["did"], "handle": got.get("handle") or who, "pds": (doc_pds or pds).rstrip("/"),
                           "access": got["accessJwt"], "refresh": got["refreshJwt"]})

    @staticmethod
    def _expires(jwt: str) -> float:
        try:
            payload = jwt.split(".")[1]
            return float(json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))["exp"])
        except (IndexError, ValueError, KeyError, TypeError):
            return 0.0

    def fresh(self, token: str) -> str:
        """The session, its access token refreshed if it's about to run out.
        The caller keeps what comes back when it differs."""
        s = self._session(token)
        if self._expires(s["access"]) - time.time() > REFRESH_BEFORE:
            return token
        got = self._xrpc(s["pds"], "com.atproto.server.refreshSession", bearer=s["refresh"], body={})
        return json.dumps({**s, "handle": got.get("handle") or s["handle"],
                           "access": got["accessJwt"], "refresh": got["refreshJwt"]})

    def whoami(self, token: str) -> NActor:
        s = self._session(token)
        try:
            p = self._get("app.bsky.actor.getProfile", actor=s["did"])
        except RemoteNotFound:
            p = {}
        return NActor(profile_url(s["did"]), str(p.get("handle") or s["handle"]), BSKY_DOMAIN,
                      p.get("displayName") or None)

    def my_roles(self, token: str) -> dict[str, bool]:
        return {}

    def logout(self, token: str) -> None:
        s = self._session(token)
        self._xrpc(s["pds"], "com.atproto.server.deleteSession", bearer=s["refresh"], body={})

    def _as_me(self, token: str, method: str, appview: bool = False, **params: Any) -> Any:
        s = self._session(token)
        return self._xrpc(s["pds"], method, bearer=s["access"], params=params, appview=appview)

    def _write(self, token: str, method: str, body: dict[str, Any]) -> Any:
        s = self._session(token)
        return self._xrpc(s["pds"], method, bearer=s["access"], body={"repo": s["did"], **body})

    def _signed_in_feed(self, uri: str, limit: int, cursor: str | None) -> Any:
        """A feed only shown to someone signed in, read as you, if you are."""
        token = self._reading_token(f"The Bluesky feed {web_url(uri)}")
        return self._as_me(token, "app.bsky.feed.getFeed", appview=True, feed=uri, limit=limit, cursor=cursor)

    def _feed(self, uri: str, limit: int, cursor: str | None = None) -> Any:
        try:
            return self._get("app.bsky.feed.getFeed", feed=uri, limit=limit, cursor=cursor)
        except RemoteAuthError:
            return self._signed_in_feed(uri, limit, cursor)

    def _did(self, actor: str) -> str:
        if actor.startswith("did:"):
            return actor
        if actor not in self._dids:
            did = self._get("com.atproto.identity.resolveHandle", handle=actor).get("did")
            if not isinstance(did, str) or not did.startswith("did:"):
                raise RemoteNotFound(f"No Bluesky account is called @{actor}")
            self._dids[actor] = did
        return self._dids[actor]

    # -- accounts and feeds ----------------------------------------------------
    def fetch_community(self, ref: CommunityRef) -> NCommunity:
        """The account or feed `ref` names (by handle or DID). Its local id is
        the name it's followed by from then on: the account's DID, or
        "<DID>/feed/<name>"."""
        if ref.name.endswith(TIMELINE):
            return self._timeline_community(ref.name)
        actor, _, rkey = ref.name.partition("/feed/")
        did = self._did(actor)
        if not rkey:
            p = self._get("app.bsky.actor.getProfile", actor=did)
            handle = str(p.get("handle") or did)
            c = NCommunity(ap_id=profile_url(did), name=handle, domain=BSKY_DOMAIN,
                           title=p.get("displayName") or None, local_id=did, description=p.get("description"))
        else:
            uri = f"at://{did}/{GENERATOR}/{rkey}"
            got = self._get("app.bsky.feed.getFeedGenerator", feed=uri)
            view = got.get("view") or {}
            if got.get("isOnline") is False or got.get("isValid") is False:
                raise RemoteNotFound(f"The Bluesky feed {web_url(uri)} isn't working at the moment")
            try:  # some feeds are only served to someone signed in: say so now, not at every check
                self._feed(uri, limit=1)
            except RemoteAuthError as exc:
                raise RemoteNotFound(str(exc)) from exc
            creator = (view.get("creator") or {}).get("handle")
            about = "\n\n".join(x for x in (view.get("description"), f"A feed by @{creator}" if creator else None) if x)
            c = NCommunity(ap_id=web_url(uri), name=str(view.get("displayName") or rkey), domain=BSKY_DOMAIN,
                           title=view.get("displayName") or None, local_id=f"{did}/feed/{rkey}",
                           description=about or None)
        self._communities[c.local_id or ref.name] = c
        return c

    def _reading_token(self, what: str) -> str:
        token = self.reading_session() if self.reading_session else None
        if not token:
            raise RemoteAuthError(f"{what} is only shown to someone signed in: log in to Bluesky on the "
                                  "Accounts page", "AuthMissing")
        return token

    def _timeline_community(self, name: str) -> NCommunity:
        """Your Following timeline, as a community. Only your own can be read."""
        did = self._did(name[: -len(TIMELINE)])
        me = self._session(self._reading_token("Your Following timeline"))
        if me["did"] != did:
            raise RemoteNotFound("Only your own Following timeline can be followed: sign in as that account")
        c = NCommunity(ap_id=profile_url(did) + TIMELINE, name="Following", domain=BSKY_DOMAIN,
                       title=f"@{me['handle']}'s timeline", local_id=f"{did}{TIMELINE}",
                       description="Posts from the accounts you follow on Bluesky.")
        self._communities[c.local_id] = c  # type: ignore[index]
        return c

    def _community(self, name: str) -> NCommunity:
        return self._communities.get(name) or self.fetch_community(CommunityRef(BSKY_DOMAIN, name, BSKY_DOMAIN))

    def list_community_posts(self, ref: CommunityRef, sort: str = "New", page: int = 1,
                             limit: int = 20) -> list[NPost]:
        """An account's posts (not replies or reposts), or a feed's, newest
        first for an account and in the feed's own order for a feed. Later
        pages carry on from where the one before ended."""
        name = ref.name
        cursor = self._cursors.get((name, page)) if page > 1 else None
        if page > 1 and cursor is None:
            return []
        community = self._community(name)
        actor, _, rkey = name.partition("/feed/")
        if name.endswith(TIMELINE):
            data = self._as_me(self._reading_token("Your Following timeline"), "app.bsky.feed.getTimeline",
                               appview=True, limit=min(limit, 100), cursor=cursor)
        elif rkey:
            data = self._feed(f"at://{actor}/{GENERATOR}/{rkey}", limit=min(limit, 100), cursor=cursor)
        else:
            data = self._get("app.bsky.feed.getAuthorFeed", actor=actor, filter="posts_no_replies",
                             limit=min(limit, 100), cursor=cursor)
        if isinstance(data.get("cursor"), str):
            self._cursors[(name, page + 1)] = data["cursor"]
        posts = []
        for item in data.get("feed") or []:
            if not isinstance(item, dict) or not isinstance(item.get("post"), dict):
                continue
            if not rkey and item.get("reason"):  # a repost, on an account's page or your timeline
                continue
            posts.append(self._post(item["post"], community, name))
        return posts

    # -- posts -----------------------------------------------------------------
    def _post(self, view: dict[str, Any], community: NCommunity, name: str) -> NPost:
        record = view.get("record") if isinstance(view.get("record"), dict) else {}
        content = _Content(record, view.get("embed"))
        labels = {str(x.get("val")) for x in view.get("labels") or [] if isinstance(x, dict)}
        meta: dict[str, Any] = {"untitled": True}
        if labels & NSFW_LABELS:
            meta["nsfw"] = True
        fallback = content.link_title or next((a for a in content.alts if a), None) or \
            ("Video" if content.video else "Pictures" if content.pictures else None)
        likes = view.get("likeCount") if isinstance(view.get("likeCount"), int) else None
        return NPost(
            ap_id=web_url(view["uri"]),
            local_id=f"{name} {view['uri']}",
            title=title_from(content.text) if content.text.strip() or not fallback else title_from(fallback),
            body=content.markdown(inline_media=False),
            url=content.link,
            created_at=_when(record.get("createdAt")) or _when(view.get("indexedAt")),
            updated_at=None,
            deleted=False, removed=False, locked=False,
            community=community,
            author=author_of(view.get("author") or {}),
            metadata=meta,
            score=likes, upvotes=likes,
            comment_count=view.get("replyCount") if isinstance(view.get("replyCount"), int) else None,
            thumbnail_url=content.pictures[0] if content.pictures else content.cover,
            gallery=content.pictures if len(content.pictures) > 1 else [],
        )

    def posts_linking(self, url: str, limit: int = 25) -> list[dict[str, Any]]:
        """Posts that link to a page (in their text or a link card), most liked
        first, as post views (discussions.py). Bluesky only searches for
        someone signed in, so it's asked as you when you are."""
        params = {"q": url, "url": url, "sort": "top", "limit": limit}
        try:
            data = self._get("app.bsky.feed.searchPosts", **params)
        except (RemoteAuthError, RemoteUnavailable):
            token = self.reading_session() if self.reading_session else None
            if not token:
                raise RemoteAuthError("Bluesky only searches for someone signed in: log in to Bluesky on the "
                                      "Accounts page", "AuthMissing")
            data = self._as_me(token, "app.bsky.feed.searchPosts", appview=True, **params)
        return [p for p in data.get("posts") or [] if isinstance(p, dict) and p.get("uri")]

    def resolve_url(self, ref: ThreadRef) -> str:
        """A bsky.app post link's local id: the post, from its author's account."""
        actor, _, rkey = ref.local_id.partition("/")
        did = self._did(actor)
        return f"{did} at://{did}/{POST}/{rkey}"

    def resolve_ap_id(self, ap_id: str) -> str | None:
        return None

    def fetch_post(self, local_id: str) -> NPost:
        name, _, uri = local_id.partition(" ")
        posts = self._get("app.bsky.feed.getPosts", uris=[uri]).get("posts") or []
        view = next((p for p in posts if isinstance(p, dict) and p.get("uri") == uri), None)
        if view is None:
            raise RemoteNotFound(f"{web_url(uri)} was deleted")
        return self._post(view, self._community(name), name)

    def fetch_comments(self, post_local_id: str) -> CommentList:
        """The post's replies, REPLY_DEPTH deep. It's the whole of them unless
        some were cut off there or are hidden (blocked, or deleted with
        replies of their own under them)."""
        _, _, uri = post_local_id.partition(" ")
        thread = self._get("app.bsky.feed.getPostThread", uri=uri, depth=REPLY_DEPTH, parentHeight=0).get("thread")
        if not isinstance(thread, dict) or thread.get("$type") != THREAD_VIEW:
            raise RemoteNotFound(f"{web_url(uri)} was deleted")
        out = CommentList()

        def walk(node: dict[str, Any], parent: str | None) -> None:
            replies = node.get("replies")
            if replies is None and node["post"].get("replyCount"):
                out.complete = False  # deeper than was read
            for r in replies or []:
                if not isinstance(r, dict) or r.get("$type") != THREAD_VIEW or not isinstance(r.get("post"), dict):
                    out.complete = False
                    continue
                p = r["post"]
                record = p.get("record") if isinstance(p.get("record"), dict) else {}
                likes = p.get("likeCount") if isinstance(p.get("likeCount"), int) else None
                out.append(NComment(
                    ap_id=web_url(p["uri"]), local_id=p["uri"], parent_local_id=parent,
                    body=_Content(record, p.get("embed")).markdown(inline_media=True),
                    created_at=_when(record.get("createdAt")) or _when(p.get("indexedAt")), updated_at=None,
                    deleted=False, removed=False, author=author_of(p.get("author") or {}),
                    score=likes, upvotes=likes,
                    reply_count=p.get("replyCount") if isinstance(p.get("replyCount"), int) else None))
                walk(r, p["uri"])

        walk(thread, None)
        return out

    # -- writing, as your account ---------------------------------------------------
    def _mention_did(self, handle: str) -> str | None:
        try:
            return self._did(handle)
        except (RemoteNotFound, RemoteUnavailable):
            return None

    def _record(self, text: str) -> dict[str, Any]:
        text = text.strip()
        if len(text) > POST_CHARS:
            raise RemoteRejected(f"Bluesky posts are at most {POST_CHARS} characters; this is {len(text)}.",
                                 "TooLong")
        record: dict[str, Any] = {"$type": POST, "text": text, "createdAt": utcnow()}
        facets = facets_for(text, self._mention_did)
        if facets:
            record["facets"] = facets
        return record

    def _ref(self, local: str) -> dict[str, str]:
        """A strong reference (at:// address and CID) to what a like or reply
        points at, from any local id the archive has for it: "<uri> <cid>"
        (resolve_as), "<followed name> <uri>" (a post) or "<uri>" (a reply).
        Without a CID, the post is read for it."""
        parts = local.split()
        uri = next((p for p in parts if p.startswith("at://")), None)
        if uri is None:
            raise RemoteNotFound(f"{local}: not a Bluesky post")
        after = parts[parts.index(uri) + 1:]
        if after:
            return {"uri": uri, "cid": after[0]}
        posts = self._get("app.bsky.feed.getPosts", uris=[uri]).get("posts") or []
        cid = next((p.get("cid") for p in posts if isinstance(p, dict) and p.get("uri") == uri), None)
        if not cid:
            raise RemoteNotFound(f"{web_url(uri)} was deleted")
        return {"uri": uri, "cid": cid}

    def resolve_as(self, token: str, ap_id: str) -> dict[str, str]:
        """A post's or reply's local id for writing: its at:// address and the
        version (CID) a like or reply points at. Your own account's: its DID."""
        m = re.match(r"^https://bsky\.app/profile/([^/]+)(?:/post/([^/?#]+))?$", ap_id)
        if not m:
            return {}
        if not m.group(2):
            return {"community": m.group(1)}
        try:
            ref = self._ref(f"at://{m.group(1)}/{POST}/{m.group(2)}")
        except RemoteNotFound:
            return {}
        return {"post": f"{ref['uri']} {ref['cid']}", "comment": f"{ref['uri']} {ref['cid']}"}

    def create_comment(self, token: str, post_local_id: str, body: str,
                       parent_local_id: str | None = None) -> NComment:
        """Reply to a post, or to a reply in its thread."""
        s = self._session(token)
        root = self._ref(post_local_id)
        parent = self._ref(parent_local_id) if parent_local_id else root
        record = {**self._record(body), "reply": {"root": root, "parent": parent}}
        got = self._write(token, "com.atproto.repo.createRecord", {"collection": POST, "record": record})
        return NComment(ap_id=web_url(got["uri"]), local_id=got["uri"],
                        parent_local_id=parent["uri"] if parent_local_id else None,
                        body=rich_markdown(record["text"], record.get("facets")), created_at=record["createdAt"],
                        updated_at=None, deleted=False, removed=False,
                        author=NActor(profile_url(s["did"]), s["handle"], BSKY_DOMAIN), score=0, upvotes=0)

    def _publish(self, token: str, record: dict[str, Any], view_embed: dict[str, Any] | None) -> NPost:
        """Post a record on your account, and it as a post of your account's."""
        s = self._session(token)
        got = self._write(token, "com.atproto.repo.createRecord", {"collection": POST, "record": record})
        view = {"uri": got["uri"], "cid": got.get("cid"), "record": record, "embed": view_embed,
                "author": {"did": s["did"], "handle": s["handle"]}, "likeCount": 0, "replyCount": 0}
        return self._post(view, self._community(s["did"]), s["did"])

    def _upload(self, token: str, data: bytes, kind: str) -> dict[str, Any]:
        s = self._session(token)
        return self._xrpc(s["pds"], "com.atproto.repo.uploadBlob", bearer=s["access"], raw=(data, kind))["blob"]

    def create_post(self, token: str, community_local_id: str, title: str, body: str | None,
                    url: str | None, card: dict[str, Any] | None = None) -> NPost:
        """A new post on your own account. Bluesky posts have no title: the
        title and text go together, and a link becomes its card: `card` has
        the page's title, description and picture (bytes, type), when they
        could be read (accounts.py), else the card is just the site's name."""
        s = self._session(token)
        if community_local_id != s["did"]:
            raise RemoteRejected("On Bluesky you can only post to your own account.", "NotYours")
        record = self._record("\n\n".join(x.strip() for x in (title, body or "") if x and x.strip()))
        view_embed = None
        if url:
            card = card or {}
            external = {"uri": url, "title": (card.get("title") or urlparse(url).hostname or url)[:300],
                        "description": (card.get("description") or "")[:1000]}
            shown = dict(external)
            if card.get("image"):
                blob = self._upload(token, *card["image"])
                external["thumb"] = blob
                shown["thumb"] = CARD_THUMB.format(did=s["did"], cid=(blob.get("ref") or {}).get("$link", ""))
            record["embed"] = {"$type": "app.bsky.embed.external", "external": external}
            view_embed = {"$type": "app.bsky.embed.external#view", "external": shown}
        return self._publish(token, record, view_embed)

    def quote(self, token: str, local_id: str, text: str) -> NPost:
        """A new post on your account quoting a post or reply."""
        ref = self._ref(local_id)
        record = {**self._record(text), "embed": {"$type": "app.bsky.embed.record", "record": ref}}
        posts = self._get("app.bsky.feed.getPosts", uris=[ref["uri"]]).get("posts") or []
        q = next((p for p in posts if isinstance(p, dict) and p.get("uri") == ref["uri"]), None)
        view_embed = {"$type": "app.bsky.embed.record#view", "record": {
            "$type": "app.bsky.embed.record#viewRecord", "uri": q["uri"], "cid": q.get("cid"),
            "author": q.get("author") or {}, "value": q.get("record") or {},
            "embeds": [q["embed"]] if q.get("embed") else []}} if q else None
        return self._publish(token, record, view_embed)

    def _viewer(self, token: str, uri: str) -> dict[str, Any]:
        """What you've done to this post: your like and repost of it, if any (the AppView knows)."""
        posts = self._as_me(token, "app.bsky.feed.getPosts", appview=True, uris=[uri]).get("posts") or []
        view = next((p for p in posts if isinstance(p, dict) and p.get("uri") == uri), None)
        return (view or {}).get("viewer") or {}

    def _toggle(self, token: str, collection: str, ref: dict[str, str], mine: str | None, on: bool) -> None:
        if on and not mine:
            self._write(token, "com.atproto.repo.createRecord",
                        {"collection": collection, "record": {"$type": collection, "subject": ref, "createdAt": utcnow()}})
        elif not on and mine:
            self._write(token, "com.atproto.repo.deleteRecord", {"collection": collection, "rkey": mine.rsplit("/", 1)[-1]})

    def repost(self, token: str, local_id: str, on: bool = True) -> None:
        """Repost a post or reply to your followers, or undo it."""
        ref = self._ref(local_id)
        self._toggle(token, REPOST, ref, self._viewer(token, ref["uri"]).get("repost"), on)

    def _vote(self, token: str, local_id: str, score: int) -> None:
        ref = self._ref(local_id)
        if score == -1:
            raise RemoteRejected("Bluesky has likes, not downvotes.", "NoDownvotes")
        self._toggle(token, LIKE, ref, self._viewer(token, ref["uri"]).get("like"), score == 1)

    def vote_post(self, token: str, local_id: str, score: int) -> None:
        self._vote(token, local_id, score)

    def vote_comment(self, token: str, local_id: str, score: int) -> None:
        self._vote(token, local_id, score)

    def _delete(self, token: str, local_id: str, deleted: bool) -> None:
        if not deleted:
            raise RemoteRejected("Bluesky can't bring back a deleted post.", "NoUndelete")
        uri = next(p for p in local_id.split() if p.startswith("at://"))
        self._write(token, "com.atproto.repo.deleteRecord", {"collection": POST, "rkey": uri.rsplit("/", 1)[-1]})

    def delete_post(self, token: str, local_id: str, deleted: bool = True) -> None:
        self._delete(token, local_id, deleted)

    def delete_comment(self, token: str, local_id: str, deleted: bool = True) -> None:
        self._delete(token, local_id, deleted)

    def edit_post(self, *_a: Any, **_k: Any) -> Any:
        raise RemoteRejected("Bluesky posts can't be edited: delete it and post again.", "NoEdits")

    edit_comment = edit_post

    # -- notifications: your Inbox ----------------------------------------------------
    def inbox(self, token: str, me_ap_id: str) -> list[NInboxItem]:
        """Replies to you, mentions of you and quotes of your posts, newest
        first (likes, reposts and follows aren't Inbox things)."""
        got = self._as_me(token, "app.bsky.notification.listNotifications", appview=True, limit=50)
        out = []
        for n in got.get("notifications") or []:
            reason, record = n.get("reason"), n.get("record")
            if reason not in ("reply", "mention", "quote") or not isinstance(record, dict) or not n.get("uri"):
                continue
            uri, cid = n["uri"], n.get("cid")
            root = (record.get("reply") or {}).get("root") or {}
            content = _Content(record, None)
            author = n.get("author") or {}
            out.append(NInboxItem(
                kind="reply" if reason == "reply" else "mention", remote_id=uri, unread=not n.get("isRead"),
                author=author_of(author), body=content.markdown(inline_media=False),
                created_at=_when(record.get("createdAt")) or _when(n.get("indexedAt")),
                object_type="comment" if root.get("uri") else "post",
                object_ap_id=web_url(uri), object_local_id=f"{uri} {cid}", author_local_id=author.get("did"),
                subject="Quoted your post" if reason == "quote" else None,
                post_ap_id=web_url(root["uri"]) if root.get("uri") else web_url(uri),
                post_local_id=f"{root['uri']} {root.get('cid')}" if root.get("uri") else f"{uri} {cid}",
                post_title=None if root.get("uri") else title_from(content.text or "")))
        return out

    def mark_inbox_read(self, token: str, kind: str, remote_id: str, read: bool) -> None:
        """Bluesky only knows when you last looked at all of them (mark_all_inbox_read):
        one item is marked read here only."""

    def mark_all_inbox_read(self, token: str, pairs: list[tuple[str, str]]) -> None:
        s = self._session(token)
        self._xrpc(s["pds"], "app.bsky.notification.updateSeen", bearer=s["access"], appview=True,
                   body={"seenAt": utcnow()})
