"""Following hashtags, through a relay.

ActivityPub has no way to follow a hashtag: a server only hears from the
accounts it follows. Tag relays fill the gap. A relay such as FediBuzz
(https://relay.fedi.buzz/tag/<name>) watches public posts across many servers
and is an actor for each hashtag; follow that actor and it passes on every
public post with the hashtag as it's made, by announcing it.

So following #selfhosted here:

1. ThreadBNC's own actor (actor.py) sends a Follow to the relay's actor for
   #selfhosted, and the relay answers with an Accept.
2. The relay delivers an Announce of each new post to the actor's inbox,
   signed by the relay. Only relays followed here are listened to.
3. Each announced post is read from its own server (a signed request, as any
   server receiving it would make) and captured into the hashtag's feed,
   where it expires like any other auto-captured post unless you keep it.
   Its replies are read when you open it (adapters/activitypub.py).

Relays pass on new posts only: edits and deletions after that are seen when
the post is opened, as for a subreddit.

While you're subscribed to your Mastodon server's public timeline
(mastodon_stream.py), hashtags' posts come from there instead: the relays
are unfollowed, and followed again when you unsubscribe."""

from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import timedelta
from typing import Any, Callable
from urllib.parse import quote, urlparse

from .actor import INBOX_PATH, Actor, is_public
from .adapters import TAG_DOMAIN, RemoteError, RemoteNotFound, is_tag
from .adapters.activitypub import ActivityPubAdapter, hashtags
from .bouncer import Bouncer
from .db import fmt_ts, parse_ts, utcnow

log = logging.getLogger(__name__)

RESEND_AFTER = timedelta(hours=1)  # a Follow the relay hasn't accepted is sent again after this
HOUSEKEEPING_EVERY = 1800.0  # seconds
KEEP_DELIVERIES = timedelta(days=3)
JOB = "relayed"


def _id(value: Any) -> str | None:
    if isinstance(value, list) and len(value) == 1:
        value = value[0]
    if isinstance(value, dict):
        value = value.get("id")
    return value if isinstance(value, str) and value.startswith("https://") else None


class TagRelays:
    def __init__(self, bouncer: Bouncer, actor: Actor, relay_template: str,
                 timeline: Callable[[], bool] | None = None):
        """`timeline`: () -> whether hashtags come from your Mastodon server's
        public timeline instead, so the relays aren't followed."""
        self.bouncer, self.db, self.actor = bouncer, bouncer.db, actor
        self.template = relay_template
        self.timeline = timeline or (lambda: False)
        self._housekept = 0.0
        bouncer.follow_hooks.append(self.subscribe)
        bouncer.unfollow_hooks.append(self.unsubscribe)
        bouncer.hooks.append(self.housekeeping)
        bouncer.job_handlers[JOB] = self.capture

    @property
    def adapter(self) -> ActivityPubAdapter:
        return self.bouncer.adapter_for(TAG_DOMAIN)  # type: ignore[return-value]

    def relay_actor(self, tag: str) -> str:
        return self.template.replace("{tag}", quote(tag))

    def _follow_row(self, community_id: int) -> Any:
        with self.db.connect() as conn:
            return conn.execute("SELECT f.*, c.canonical_ap_id, c.name FROM community_follows f "
                                "JOIN communities c ON c.id=f.community_id WHERE f.community_id=?",
                                (community_id,)).fetchone()

    # -- following -----------------------------------------------------------------
    def subscribe(self, community_id: int) -> str | None:
        """Follow a hashtag's relay actor (a follow hook). Returns 'pending'
        once the Follow is sent, or None when it couldn't be."""
        row = self._follow_row(community_id)
        if row is None or not row["active"] or not is_tag(row["canonical_ap_id"]) or self.timeline():
            return None
        relay = self.relay_actor(row["name"])
        follow_id = f"{self.actor.id}#follows/{uuid.uuid4()}"
        try:
            # The relay's own name for the hashtag's actor: FediBuzz, for one,
            # transliterates (#café is .../tag/cafe), and signs with that.
            doc = self.actor.fetch(relay)
            relay, inbox = _id(doc.get("id")) or relay, _id(doc.get("inbox"))
            if inbox is None:
                raise RemoteNotFound(f"{relay} has no inbox")
            # Recorded before it's sent: the relay may accept before sending returns.
            with self.db.transaction() as conn:
                conn.execute("UPDATE community_follows SET push_domain=?, push_state='pending', push_error=NULL, "
                             "push_changed_at=?, push_actor=?, push_follow_id=? WHERE community_id=?",
                             (TAG_DOMAIN, utcnow(), relay, follow_id, community_id))
            self.actor.deliver(inbox, {"@context": "https://www.w3.org/ns/activitystreams", "id": follow_id,
                                       "type": "Follow", "actor": self.actor.id, "object": relay})
        except RemoteError as exc:
            log.warning("following %s failed: %s", relay, exc)
            with self.db.transaction() as conn:
                conn.execute("UPDATE community_follows SET push_domain=?, push_state=NULL, push_error=?, "
                             "push_changed_at=? WHERE community_id=?",
                             (TAG_DOMAIN, str(exc)[:500], utcnow(), community_id))
            return None
        return "pending"

    def unsubscribe(self, community_id: int) -> None:
        """Stop following a hashtag's relay (an unfollow hook, and while
        hashtags come from your Mastodon server's public timeline)."""
        row = self._follow_row(community_id)
        if row is None or not is_tag(row["canonical_ap_id"]):
            return
        if not row["push_actor"]:
            if row["push_state"] or row["push_error"]:  # never followed: nothing to undo
                with self.db.transaction() as conn:
                    conn.execute("UPDATE community_follows SET push_domain=NULL, push_state=NULL, push_error=NULL, "
                                 "push_changed_at=? WHERE community_id=?", (utcnow(), community_id))
            return
        undo = {"@context": "https://www.w3.org/ns/activitystreams",
                "id": f"{self.actor.id}#undo/{uuid.uuid4()}", "type": "Undo", "actor": self.actor.id,
                "object": {"id": row["push_follow_id"], "type": "Follow", "actor": self.actor.id,
                           "object": row["push_actor"]}}
        try:
            self.actor.deliver(self.actor.inbox_of(row["push_actor"]), undo)
        except RemoteError as exc:  # it may keep sending; deliveries from it are ignored now
            log.warning("unfollowing %s failed: %s", row["push_actor"], exc)
        with self.db.transaction() as conn:
            conn.execute("UPDATE community_follows SET push_domain=NULL, push_state=NULL, push_error=NULL, "
                         "push_changed_at=?, push_actor=NULL, push_follow_id=NULL WHERE community_id=?",
                         (utcnow(), community_id))

    def housekeeping(self, now: bool = False) -> None:
        """Now and then (a bouncer hook), or `now`: send again the Follows a
        relay hasn't accepted, or that couldn't be sent, and tidy old
        deliveries. While hashtags come from your Mastodon server's public
        timeline, unfollow the relays instead."""
        if not now and time.monotonic() - self._housekept < HOUSEKEEPING_EVERY:
            return
        self._housekept = time.monotonic()
        stale = fmt_ts(parse_ts(utcnow()) - RESEND_AFTER)  # type: ignore[operator]
        timeline = self.timeline()
        with self.db.connect() as conn:
            rows = conn.execute("SELECT f.community_id, f.push_state, f.push_changed_at, f.push_actor, f.push_error, "
                                "c.canonical_ap_id FROM community_follows f JOIN communities c ON c.id=f.community_id "
                                "WHERE f.active=1 AND c.canonical_ap_id LIKE 'tag:%'").fetchall()
        for r in rows:
            if timeline:
                if r["push_actor"] or r["push_state"] or r["push_error"]:
                    self.unsubscribe(r["community_id"])
            elif r["push_state"] is None or (r["push_state"] == "pending" and (r["push_changed_at"] or "") < stale):
                self.subscribe(r["community_id"])
        old = fmt_ts(parse_ts(utcnow()) - KEEP_DELIVERIES)  # type: ignore[operator]
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM ap_inbox WHERE path=? AND received_at<?", (INBOX_PATH, old))

    # -- the inbox -----------------------------------------------------------------
    def _relays(self) -> dict[str, int]:
        """Relay actors listened to -> their hashtag community."""
        with self.db.connect() as conn:
            return {r["push_actor"]: r["community_id"] for r in conn.execute(
                "SELECT community_id, push_actor FROM community_follows WHERE active=1 AND push_actor IS NOT NULL "
                "AND push_domain=?", (TAG_DOMAIN,))}

    def expects(self, activity: dict[str, Any]) -> bool:
        """Whether a delivery comes from a relay followed here (checked before
        its signature, so nobody else can make ThreadBNC fetch anything)."""
        return _id(activity.get("actor")) in self._relays()

    def receive(self, activity: dict[str, Any], signer: str) -> str:
        """A delivery from a followed relay, its signature checked."""
        relay = _id(activity.get("actor")) or ""
        community_id = self._relays().get(relay)
        kind = str(activity.get("type") or "")
        obj = activity.get("object")
        if community_id is None:
            outcome, status = "not from a followed relay", "skipped"
        elif kind in ("Accept", "Reject"):
            accepted = kind == "Accept"
            with self.db.transaction() as conn:
                conn.execute("UPDATE community_follows SET push_state=?, push_error=?, push_changed_at=? "
                             "WHERE community_id=?", ("subscribed" if accepted else None,
                                                      None if accepted else f"{urlparse(relay).hostname} refused",
                                                      utcnow(), community_id))
            outcome, status = ("following" if accepted else "the relay refused the follow"), "done"
        elif kind in ("Announce", "Create") and _id(obj):
            post = _id(obj) or ""
            if self.bouncer._existing_thread(post):
                outcome, status = "already saved", "skipped"
            else:
                self.bouncer.enqueue(JOB, {"object": post, "relay": relay, "community_id": community_id})
                outcome, status = "reading it from its server", "done"
            self._arriving(community_id)
        else:
            outcome, status = f"{kind or 'unknown'} activity", "skipped"
        now = utcnow()
        with self.db.transaction() as conn:
            conn.execute("INSERT INTO ap_inbox(domain, path, activity_id, activity_type, body, received_at, status, "
                         "outcome, attempts, processed_at) VALUES (?,?,?,?,?,?,?,?,1,?) "
                         "ON CONFLICT(activity_id) DO NOTHING",
                         (self.actor.domain, INBOX_PATH, _id(activity.get("id")), kind[:40],
                          json.dumps(activity)[:20000], now, status, outcome, now))
        return outcome

    def _arriving(self, community_id: int) -> None:
        """Posts are coming from the relay: it's following, whatever became of its Accept."""
        now = utcnow()
        with self.db.transaction() as conn:
            conn.execute("UPDATE community_follows SET last_push_at=? WHERE community_id=?", (now, community_id))
            conn.execute("UPDATE community_follows SET push_state='subscribed', push_error=NULL, push_changed_at=? "
                         "WHERE community_id=? AND push_state='pending'", (now, community_id))

    # -- capturing -----------------------------------------------------------------
    def capture(self, payload: dict[str, Any]) -> dict[str, Any]:
        """A job: read an announced post from its own server and capture it
        into the hashtag's feed. When it carries several hashtags followed
        here, it goes to the first of them it lists."""
        ap_id = payload["object"]
        existing = self.bouncer._existing_thread(ap_id)
        if existing:
            return {"thread_id": existing["id"]}
        obj = self.adapter.fetch_object(ap_id)
        if not is_public(obj):
            return {"skipped": "not public"}
        with self.db.connect() as conn:
            followed = {r["name"]: r for r in conn.execute(
                "SELECT c.name, f.community_id, f.capture_since FROM community_follows f "
                "JOIN communities c ON c.id=f.community_id WHERE f.active=1 AND c.canonical_ap_id LIKE 'tag:%'")}
        tag = next((t for t in hashtags(obj) if t in followed), None)
        if tag is None:
            by_id = {r["community_id"]: name for name, r in followed.items()}
            tag = by_id.get(payload.get("community_id"))
        if tag is None:
            return {"skipped": "no longer following its hashtag"}
        post = self.adapter.post_from(obj, tag, ap_id)
        created, since = parse_ts(post.created_at), parse_ts(followed[tag]["capture_since"])
        if created and since and created < since:
            return {"skipped": "older than the follow"}
        tid = self.bouncer._ingest_post(post, TAG_DOMAIN, post.local_id, self.adapter, capture=True,
                                        source_url=post.ap_id, retention="auto")
        return {"thread_id": tid}
