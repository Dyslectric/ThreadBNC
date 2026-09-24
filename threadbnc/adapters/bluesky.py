"""Bluesky accounts and custom feeds, followed like communities.

Everything is read from Bluesky's public API (public.api.bsky.app), with no
account: the posts a followed account makes (not its replies or reposts), or
what a custom feed lists; a post's replies when it's opened, as its comments;
and its likes, as its votes, from each check of the account or feed. Nothing
can be posted, replied to or liked from here.

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

import re
from typing import Any
from urllib.parse import quote

from ..db import fmt_ts, parse_ts
from ..render import escape_markdown, plain_lines
from .activitypub import title_from
from .base import (
    BSKY_DOMAIN,
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
)

API = "public.api.bsky.app"
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

    def _get(self, method: str, **params: Any) -> Any:
        data = self.http.get_json(API, f"/xrpc/{method}", params)
        if not isinstance(data, dict):
            raise RemoteNotFound(f"{API}: {method} answered with something unexpected")
        return data

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
                self._get("app.bsky.feed.getFeed", feed=uri, limit=1)
            except RemoteAuthError as exc:
                raise RemoteNotFound(f"The Bluesky feed {web_url(uri)} is only shown to someone signed in "
                                     "to Bluesky, which ThreadBNC isn't") from exc
            creator = (view.get("creator") or {}).get("handle")
            about = "\n\n".join(x for x in (view.get("description"), f"A feed by @{creator}" if creator else None) if x)
            c = NCommunity(ap_id=web_url(uri), name=str(view.get("displayName") or rkey), domain=BSKY_DOMAIN,
                           title=view.get("displayName") or None, local_id=f"{did}/feed/{rkey}",
                           description=about or None)
        self._communities[c.local_id or ref.name] = c
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
        if rkey:
            data = self._get("app.bsky.feed.getFeed", feed=f"at://{actor}/{GENERATOR}/{rkey}",
                             limit=min(limit, 100), cursor=cursor)
        else:
            data = self._get("app.bsky.feed.getAuthorFeed", actor=actor, filter="posts_no_replies",
                             limit=min(limit, 100), cursor=cursor)
        if isinstance(data.get("cursor"), str):
            self._cursors[(name, page + 1)] = data["cursor"]
        posts = []
        for item in data.get("feed") or []:
            if not isinstance(item, dict) or not isinstance(item.get("post"), dict):
                continue
            if not rkey and item.get("reason"):  # a repost, on an account's own page
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
