"""Following people on Mastodon (and GoToSocial, Akkoma, Misskey... anyone
on the fediverse who posts rather than runs a community).

ThreadBNC's own actor (actor.py) follows them the way any fediverse server's
user would, so their server sends it what they post:

1. Following @alice@example.social looks her up (WebFinger, then her actor),
   and the actor sends her a Follow. Her server accepts it (at once, or once
   she approves it, if she approves her followers).
2. Her server delivers a Create of each post she makes to the actor's inbox,
   signed by her server. The post comes inside it, so nothing is fetched:
   it's captured into her feed, and expires like any other auto-captured
   post unless you keep it. Only her public and unlisted posts are taken,
   and not her replies (they belong under what they answer), nor her boosts,
   as with a Bluesky account's reposts.
3. Edits she makes to a post arrive too, and update it. Replies are read when
   you open a post (adapters/activitypub.py), and deletions are seen then.

Following her starts with the posts on the first page of her outbox (what
her profile shows), when "Start with the posts on the first page" is ticked.
Her server is never checked on a schedule unless you ask for it on her page:
whatever arrives, arrives by push."""

from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import timedelta
from typing import Any

from . import store
from .actor import INBOX_PATH, Actor
from .adapters import TAG_DOMAIN, RemoteError, RemoteNotFound, is_fedi_account
from .adapters.activitypub import ActivityPubAdapter, own_post
from .bouncer import Bouncer
from .db import fmt_ts, parse_ts, utcnow

log = logging.getLogger(__name__)

# A Follow that hasn't been answered is sent again after this. Mastodon takes a
# repeated one as the same request, so someone who approves their followers
# isn't asked twice; it just catches a Follow that went astray.
RESEND_AFTER = timedelta(days=1)
HOUSEKEEPING_EVERY = 1800.0  # seconds
JOB = "person_backfill"
BACKFILL_SINCE = "1970-01-01T00:00:00.000000Z"  # the capture_since of a follow asked to start with what's there


def _id(value: Any) -> str | None:
    if isinstance(value, list) and len(value) == 1:
        value = value[0]
    if isinstance(value, dict):
        value = value.get("id")
    return value if isinstance(value, str) and value.startswith("https://") else None


class PeopleFollows:
    def __init__(self, bouncer: Bouncer, actor: Actor):
        self.bouncer, self.db, self.actor = bouncer, bouncer.db, actor
        self._housekept = float("-inf")  # monotonic time counts from boot: the first round runs at once
        bouncer.follow_hooks.append(self.followed)
        bouncer.unfollow_hooks.append(self.unsubscribe)
        bouncer.hooks.append(self.housekeeping)
        bouncer.job_handlers[JOB] = self.backfill

    @property
    def adapter(self) -> ActivityPubAdapter:
        return self.bouncer.tag_adapter

    def _follow_row(self, community_id: int) -> Any:
        with self.db.connect() as conn:
            return conn.execute("SELECT f.*, c.canonical_ap_id, c.name FROM community_follows f "
                                "JOIN communities c ON c.id=f.community_id WHERE f.community_id=?",
                                (community_id,)).fetchone()

    # -- following -----------------------------------------------------------------
    def followed(self, community_id: int) -> str | None:
        """A follow hook: follow someone, and when the follow asked to start
        with what's there, read their latest posts (the first time)."""
        row = self._follow_row(community_id)
        if row is None or row["source_domain"] != TAG_DOMAIN or not row["active"] or not is_fedi_account(row["canonical_ap_id"]):
            return None
        if row["capture_since"] == BACKFILL_SINCE and not row["last_polled_at"]:
            self.bouncer.enqueue(JOB, {"community_id": community_id})
        return self.subscribe(community_id)

    def subscribe(self, community_id: int) -> str | None:
        """Send someone followed a Follow. Returns 'pending' once it's sent,
        or None when it couldn't be."""
        row = self._follow_row(community_id)
        if row is None or row["source_domain"] != TAG_DOMAIN or not row["active"] or not is_fedi_account(row["canonical_ap_id"]):
            return None
        person = row["canonical_ap_id"]
        follow_id = f"{self.actor.id}#follows/{uuid.uuid4()}"
        try:
            inbox = _id(self.adapter.person(person).get("inbox"))
            if inbox is None:
                raise RemoteNotFound(f"{person} has no inbox")
            # Recorded before it's sent: their server may accept before sending returns.
            with self.db.transaction() as conn:
                conn.execute("UPDATE community_follows SET push_domain=?, push_state='pending', push_error=NULL, "
                             "push_changed_at=?, push_actor=?, push_follow_id=? WHERE community_id=?",
                             (TAG_DOMAIN, utcnow(), person, follow_id, community_id))
            self.actor.deliver(inbox, {"@context": "https://www.w3.org/ns/activitystreams", "id": follow_id,
                                       "type": "Follow", "actor": self.actor.id, "object": person})
        except RemoteError as exc:
            log.warning("following %s failed: %s", person, exc)
            with self.db.transaction() as conn:
                conn.execute("UPDATE community_follows SET push_domain=?, push_state=NULL, push_error=?, "
                             "push_changed_at=? WHERE community_id=?",
                             (TAG_DOMAIN, str(exc)[:500], utcnow(), community_id))
            return None
        return "pending"

    def unsubscribe(self, community_id: int) -> None:
        """Stop following someone (an unfollow hook)."""
        row = self._follow_row(community_id)
        if row is None or row["source_domain"] != TAG_DOMAIN or not is_fedi_account(row["canonical_ap_id"]):
            return
        if row["push_actor"]:
            undo = {"@context": "https://www.w3.org/ns/activitystreams",
                    "id": f"{self.actor.id}#undo/{uuid.uuid4()}", "type": "Undo", "actor": self.actor.id,
                    "object": {"id": row["push_follow_id"], "type": "Follow", "actor": self.actor.id,
                               "object": row["push_actor"]}}
            try:
                self.actor.deliver(self.actor.inbox_of(row["push_actor"]), undo)
            except RemoteError as exc:  # they may keep sending; deliveries from them are ignored now
                log.warning("unfollowing %s failed: %s", row["push_actor"], exc)
        with self.db.transaction() as conn:
            conn.execute("UPDATE community_follows SET push_domain=NULL, push_state=NULL, push_error=NULL, "
                         "push_changed_at=?, push_actor=NULL, push_follow_id=NULL WHERE community_id=?",
                         (utcnow(), community_id))

    def housekeeping(self, now: bool = False) -> None:
        """Now and then (a bouncer hook), or `now`: send again the Follows
        nobody has answered for a day, or that couldn't be sent."""
        if not now and time.monotonic() - self._housekept < HOUSEKEEPING_EVERY:
            return
        self._housekept = time.monotonic()
        stale = fmt_ts(parse_ts(utcnow()) - RESEND_AFTER)  # type: ignore[operator]
        with self.db.connect() as conn:
            rows = conn.execute("SELECT f.community_id, f.push_state, f.push_changed_at, c.canonical_ap_id "
                                "FROM community_follows f JOIN communities c ON c.id=f.community_id "
                                "WHERE f.active=1 AND f.source_domain=? AND c.canonical_ap_id LIKE 'https://%'",
                                (TAG_DOMAIN,)).fetchall()
        for r in rows:
            if not is_fedi_account(r["canonical_ap_id"]):
                continue
            if r["push_state"] is None or (r["push_state"] == "pending" and (r["push_changed_at"] or "") < stale):
                self.subscribe(r["community_id"])

    def backfill(self, payload: dict[str, Any]) -> dict[str, Any]:
        """A job: start a new follow with the posts on the first page of their
        outbox, read as their page's live view reads them."""
        return {"captured": self.bouncer.poll_follow(payload["community_id"])}

    # -- the inbox -----------------------------------------------------------------
    def _people(self) -> dict[str, int]:
        """People followed -> their community."""
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT f.community_id, f.push_actor FROM community_follows f JOIN communities c "
                "ON c.id=f.community_id WHERE f.active=1 AND f.push_actor IS NOT NULL AND f.push_domain=? "
                "AND c.canonical_ap_id=f.push_actor", (TAG_DOMAIN,)).fetchall()
        return {r["push_actor"]: r["community_id"] for r in rows}

    def expects(self, activity: dict[str, Any]) -> bool:
        """Whether a delivery comes from someone followed here (checked before
        its signature, so nobody else can make ThreadBNC fetch anything)."""
        return _id(activity.get("actor")) in self._people()

    def receive(self, activity: dict[str, Any], signer: str) -> str:
        """A delivery from someone followed, its signature checked (by their server)."""
        person = _id(activity.get("actor")) or ""
        community_id = self._people().get(person)
        kind = str(activity.get("type") or "")
        if community_id is None:
            outcome, status = "not from anyone followed", "skipped"
        elif kind in ("Accept", "Reject"):
            accepted = kind == "Accept"
            with self.db.transaction() as conn:
                conn.execute("UPDATE community_follows SET push_state=?, push_error=?, push_changed_at=? "
                             "WHERE community_id=?", ("subscribed" if accepted else None,
                                                      None if accepted else "they refused the follow",
                                                      utcnow(), community_id))
            outcome, status = ("following" if accepted else "they refused the follow"), "done"
        elif kind == "Create":
            outcome, status = self._create(activity, person, community_id)
        elif kind == "Update":
            outcome, status = self._update(activity, person, community_id)
        else:  # boosts, deletions (seen when a post is opened), profile changes...
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
        """Their posts are coming: it's following, whatever became of the Accept."""
        now = utcnow()
        with self.db.transaction() as conn:
            conn.execute("UPDATE community_follows SET last_push_at=? WHERE community_id=?", (now, community_id))
            conn.execute("UPDATE community_follows SET push_state='subscribed', push_error=NULL, push_changed_at=? "
                         "WHERE community_id=? AND push_state='pending'", (now, community_id))

    def _create(self, activity: dict[str, Any], person: str, community_id: int) -> tuple[str, str]:
        """A post they made, captured as it came: their server signed it."""
        obj = own_post(activity, person)
        if obj is None:
            return "not a public post of theirs (a reply, or followers-only)", "skipped"
        self._arriving(community_id)
        post = self.adapter.post_from(obj, person)
        if self.bouncer._existing_thread(post.ap_id):
            return "already saved", "skipped"
        with self.db.connect() as conn:
            since = parse_ts(conn.execute("SELECT capture_since FROM community_follows WHERE community_id=?",
                                          (community_id,)).fetchone()["capture_since"])
        created = parse_ts(post.created_at)
        if created and since and created < since:
            return "older than the follow", "skipped"
        if self.bouncer.hidden(post):
            return "by someone you've hidden", "skipped"
        tid = self.bouncer._ingest_post(post, TAG_DOMAIN, post.local_id, self.adapter, capture=True,
                                        source_url=post.ap_id, retention="auto")
        return f"captured as thread {tid}", "done"

    def _update(self, activity: dict[str, Any], person: str, community_id: int) -> tuple[str, str]:
        """An edit to a post of theirs saved in their feed: kept as its new version."""
        obj = own_post({**activity, "type": "Create"}, person)
        if obj is None:
            return "not a public post of theirs", "skipped"
        thread = self.bouncer._existing_thread(obj["id"])
        if thread is None or thread["community_id"] != community_id or thread["trashed_at"]:
            return "not a post saved in their feed", "skipped"
        post = self.adapter.post_from(obj, person)
        result = store.ApplyResult()
        with self.db.transaction() as conn:  # counts are only as the edit carried them: keep the stored ones
            store.apply_post(conn, thread["id"], post, TAG_DOMAIN, utcnow(), False, result, keep_counts=True)
        self._arriving(community_id)
        return f"{result.new_revisions} new version(s)", "done"
