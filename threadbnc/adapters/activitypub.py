"""Posts from anywhere on the fediverse (Mastodon, GoToSocial, Akkoma,
Misskey...), read the way their servers publish them: the ActivityPub object
at its own address, asked for with ThreadBNC's signature (actor.py).

These are the posts hashtags bring (tags.py), and the posts you make on your
Mastodon account (accounts.py). A thread's local id is "<key> <post's
ActivityPub id>": what it's filed under, and where to read it. The key is the
hashtag, or for your own posts your account's address; those are read back
through their server's Mastodon API, which needs no signature. Replies are
read when a post is opened, from the post's own server through the Mastodon
API that Mastodon, GoToSocial, Akkoma and Pleroma share, when its address says
which status it is; elsewhere a post shows no comments, and none are taken for
gone."""

from __future__ import annotations

import html as htmllib
import re
from typing import TYPE_CHECKING, Any, Callable
from urllib.parse import urlparse

from .. import languages
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


def tag_community(tag: str, description: str | None = None) -> NCommunity:
    return NCommunity(ap_id=TAG_PREFIX + tag, name=tag, domain=TAG_DOMAIN, title=None, local_id=tag,
                      description=description)


def account_community(actor: str) -> NCommunity:
    """Your Mastodon account, as the community the posts you make on it are filed under."""
    name = urlparse(actor).path.rstrip("/").rsplit("/", 1)[-1].lstrip("@")
    return NCommunity(ap_id=actor, name=name, domain=TAG_DOMAIN, title=None, local_id=actor,
                      description="The posts you've made on your Mastodon account from ThreadBNC.")


def community_for(key: str) -> NCommunity:
    return account_community(key) if key.startswith("https://") else tag_community(key)


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


def author_of(obj: dict[str, Any]) -> NActor:
    ap_id = _id(obj.get("attributedTo")) or _id(obj.get("actor")) or ""
    page = obj.get("url") if isinstance(obj.get("url"), str) else ""
    m = re.match(r"^/@([^/@]+)", urlparse(page).path or "")
    username = m.group(1) if m else (urlparse(ap_id).path.rstrip("/").rsplit("/", 1)[-1] or "?")
    return NActor(ap_id, username, host_of(ap_id))


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
        body=html_to_markdown(html, uri) or None,
        url=shared_link(html) or video,
        created_at=s.get("created_at"),
        updated_at=s.get("edited_at"),
        deleted=False, removed=False, locked=False,
        community=community_for(key),
        author=NActor(actor, account.get("username") or "?", host_of(actor), account.get("display_name") or None),
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
        # () -> whether hashtags come from your Mastodon server's public
        # timeline (mastodon_stream.py sets it), which needs no actor either.
        self.mastodon: Callable[[], bool] = lambda: False

    def _need_actor(self) -> Actor:
        if self.actor is None:
            raise RemoteAuthError("Following hashtags on the fediverse needs ThreadBNC's own ActivityPub identity: "
                                  "set THREADBNC_ACTOR_DOMAIN (see README, \"Hashtags\").")
        return self.actor

    # -- communities: a hashtag has nothing to list; its posts are passed on as they're made
    def fetch_community(self, ref: CommunityRef) -> NCommunity:
        timeline = self.mastodon()
        if not self.bluesky and not timeline:
            self._need_actor()
        fediverse = ("your Mastodon server's public timeline" if timeline
                     else "across the fediverse, as a relay passes them on" if self.actor else None)
        where = " and ".join(x for x in (fediverse,
                                         "Bluesky, picked out of everything posted there" if self.bluesky else None) if x)
        return tag_community(ref.name, f"Public posts tagged #{ref.name} from {where}.")

    def list_community_posts(self, ref: CommunityRef, sort: str = "New", page: int = 1,
                             limit: int = 20) -> list[NPost]:
        return []

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

    def post_from(self, obj: dict[str, Any], tag: str, ap_id: str | None = None) -> NPost:
        ap_id = _id(obj.get("id")) or ap_id or ""
        html = _text(obj.get("content")) or _text(obj.get("contentMap"))
        body = html_to_markdown(html, ap_id)
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
            local_id=f"{tag} {ap_id}",
            title=name or warning or title_from(plain_text(html)),
            body=body or None,
            url=shared_link(html) or video,
            created_at=_text(obj.get("published")),
            updated_at=_text(obj.get("updated")),
            deleted=False, removed=False, locked=False,
            community=tag_community(tag),
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
        if key.startswith("https://"):  # one of your own, read back as your server shows anyone
            sid = status_id(ap_id)
            if sid is None or self.http is None:
                raise RemoteNotFound(f"{ap_id} can't be read back")
            return status_post(self.http.get_json(host_of(ap_id), f"/api/v1/statuses/{sid}"), key)
        if self.actor is None:  # from your server's public timeline: read as its own server shows anyone
            sid = status_id(ap_id)
            if sid is None or self.http is None:
                raise UnsupportedSoftware(f"{ap_id} can only be read again with ThreadBNC's own ActivityPub "
                                          "identity (THREADBNC_ACTOR_DOMAIN)")
            return status_post(self.http.get_json(host_of(ap_id), f"/api/v1/statuses/{sid}"), key)
        return self.post_from(self.fetch_object(ap_id), key, ap_id)

    # -- replies -----------------------------------------------------------------
    def fetch_comments(self, post_local_id: str) -> CommentList:
        """The replies the post's own server knows of, through its Mastodon API.
        It's never the complete tree (servers only know the replies that
        reached them), so nothing missing from it is taken for deleted."""
        _, _, ap_id = post_local_id.partition(" ")
        out = CommentList()
        out.complete = False
        status = status_id(ap_id)
        if status is None or self.http is None:
            return out
        try:
            context = self.http.get_json(host_of(ap_id), f"/api/v1/statuses/{status}/context")
        except RemoteNotFound:
            return out
        except RemoteAuthError:  # the server doesn't show replies to visitors
            return out
        replies = context.get("descendants") if isinstance(context, dict) else None
        uris = {status: None}
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
                              account.get("display_name") or None),
                score=s.get("favourites_count"),
                upvotes=s.get("favourites_count"),
                reply_count=s.get("replies_count"),
            ))
        return out
