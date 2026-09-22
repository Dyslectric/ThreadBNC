"""Append-only persistence of observations.

Rules enforced here:
  * An object is identified by its canonical ActivityPub id, never a server-local id.
  * A new revision is written only when meaningful content/metadata changed.
  * Blank content on a deleted/removed object is treated as the remote *withholding*
    content, not as an edit; the last observed text is kept.
  * State transitions (delete/remove/lock/missing/...) become StateEvents.
  * Nothing here deletes rows, except purge_thread(), which only touches
    expired auto-captured threads and threads the user put in the trash (and
    media that nothing else references).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import dupes, media
from .adapters import NActor, NComment, NCommunity, NPost, host_of
from .db import Conn, dumps

# Placeholder strings some servers substitute for withheld content.
REDACTION_MARKERS = {"", "*permanently deleted*", "*removed*", "*deleted*", "[deleted]", "[removed]"}
ALWAYS_REDACTION = {"*permanently deleted*"}  # Lemmy account deletion overwrite


@dataclass
class PendingModLookup:
    event_id: int
    object_id: int
    object_type: str
    local_id: str
    event_type: str


@dataclass
class ApplyResult:
    object_ids: dict[str, int] = field(default_factory=dict)  # ap_id -> object id
    new_objects: int = 0
    new_revisions: int = 0
    new_events: int = 0
    mod_lookups: list[PendingModLookup] = field(default_factory=list)


# --- basic upserts ---------------------------------------------------------

def upsert_instance(conn: Conn, domain: str, now: str, *,
                    software: str | None = None, version: str | None = None) -> int:
    row = conn.execute("SELECT id FROM instances WHERE domain=?", (domain,)).fetchone()
    if row:
        conn.execute("UPDATE instances SET last_seen_at=? WHERE id=?", (now, row["id"]))
        if software:
            conn.execute(
                "UPDATE instances SET software=?, software_version=?, software_checked_at=? WHERE id=?",
                (software, version, now, row["id"]),
            )
        return row["id"]
    cur = conn.execute(
        "INSERT INTO instances(domain, software, software_version, first_seen_at, last_seen_at, "
        "software_checked_at) VALUES (?,?,?,?,?,?)",
        (domain, software, version, now, now, now if software else None),
    )
    return cur.lastrowid


def record_instance_contact(conn: Conn, domain: str, now: str, ok: bool,
                            error: str | None = None) -> None:
    iid = upsert_instance(conn, domain, now)
    row = conn.execute("SELECT available FROM instances WHERE id=?", (iid,)).fetchone()
    if ok:
        conn.execute(
            "UPDATE instances SET last_successful_contact_at=?, available=1, last_error=NULL WHERE id=?",
            (now, iid),
        )
        if row["available"] == 0:
            add_event(conn, "instance_recovered", now, instance_id=iid)
    else:
        conn.execute(
            "UPDATE instances SET last_failure_at=?, last_error=?, available=0 WHERE id=?",
            (now, error, iid),
        )
        if row["available"] == 1:
            add_event(conn, "instance_unavailable", now, instance_id=iid, reason=error)


def upsert_actor(conn: Conn, actor: NActor, now: str) -> int | None:
    if not actor.ap_id:
        return None
    row = conn.execute("SELECT id FROM actors WHERE canonical_ap_id=?", (actor.ap_id,)).fetchone()
    if row:
        conn.execute(
            "UPDATE actors SET last_seen_at=?, display_name=COALESCE(?, display_name) WHERE id=?",
            (now, actor.display_name, row["id"]),
        )
        return row["id"]
    cur = conn.execute(
        "INSERT INTO actors(canonical_ap_id, username, instance, display_name, first_seen_at, last_seen_at) "
        "VALUES (?,?,?,?,?,?)",
        (actor.ap_id, actor.username, actor.domain, actor.display_name, now, now),
    )
    return cur.lastrowid


def add_event(conn: Conn, event_type: str, now: str, *, object_id: int | None = None,
              thread_id: int | None = None, community_id: int | None = None,
              instance_id: int | None = None, attribution: str | None = None,
              actor_id: int | None = None, remote_timestamp: str | None = None,
              reason: str | None = None, metadata: dict[str, Any] | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO state_events(object_id, thread_id, community_id, instance_id, event_type, attribution, "
        "actor_id, observed_at, remote_timestamp, reason, metadata_json) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (object_id, thread_id, community_id, instance_id, event_type, attribution, actor_id, now,
         remote_timestamp, reason, dumps(metadata)),
    )
    return cur.lastrowid


def upsert_community(conn: Conn, c: NCommunity, now: str) -> int:
    row = conn.execute("SELECT * FROM communities WHERE canonical_ap_id=?", (c.ap_id,)).fetchone()
    iid = upsert_instance(conn, c.domain or host_of(c.ap_id), now)
    if not row:
        cur = conn.execute(
            "INSERT INTO communities(canonical_ap_id, name, title, instance_id, first_seen_at, last_seen_at, "
            "cur_removed, cur_deleted) VALUES (?,?,?,?,?,?,?,?)",
            (c.ap_id, c.name, c.title, iid, now, now, int(c.removed), int(c.deleted)),
        )
        cid = cur.lastrowid
        observe_moderators(conn, cid, None, c.moderator_ap_ids, now)
        if c.removed:
            add_event(conn, "community_removed", now, community_id=cid, attribution="unknown",
                      metadata={"state_at_first_observation": True})
        if c.deleted:
            add_event(conn, "community_deleted", now, community_id=cid,
                      metadata={"state_at_first_observation": True})
        return cid
    cid = row["id"]
    conn.execute("UPDATE communities SET last_seen_at=?, title=COALESCE(?, title) WHERE id=?",
                 (now, c.title, cid))
    observe_moderators(conn, cid, row["moderators_json"], c.moderator_ap_ids, now)
    if bool(row["cur_removed"]) != c.removed:
        add_event(conn, "community_removed" if c.removed else "community_restored", now,
                  community_id=cid, attribution="unknown")
    if bool(row["cur_deleted"]) != c.deleted:
        add_event(conn, "community_deleted" if c.deleted else "community_undeleted", now, community_id=cid)
    conn.execute("UPDATE communities SET cur_removed=?, cur_deleted=? WHERE id=?",
                 (int(c.removed), int(c.deleted), cid))
    return cid


def observe_moderators(conn: Conn, community_id: int, stored_json: str | None,
                        seen: list[str] | None, now: str) -> None:
    """Keep the community's current moderator list, and record additions and
    removals as history once we have a previous list to compare against."""
    if seen is None:
        return  # this response didn't include moderators
    current = sorted({m for m in seen if m})
    if stored_json is not None:
        before = set(json.loads(stored_json))
        for ap_id in sorted(set(current) - before):
            add_event(conn, "moderator_added", now, community_id=community_id, metadata={"moderator": ap_id})
        for ap_id in sorted(before - set(current)):
            add_event(conn, "moderator_removed", now, community_id=community_id, metadata={"moderator": ap_id})
    conn.execute("UPDATE communities SET moderators_json=? WHERE id=?", (json.dumps(current), community_id))


# --- revisions -------------------------------------------------------------

def content_hash(title: str | None, body: str | None, url: str | None, meta: dict[str, Any]) -> str:
    payload = json.dumps({"title": title, "body": body, "url": url, "meta": meta},
                         sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _is_redacted(value: str | None, hidden: bool) -> bool:
    norm = (value or "").strip().lower()
    if norm in ALWAYS_REDACTION:
        return True
    return hidden and norm in REDACTION_MARKERS


def record_revision(conn: Conn, object_id: int, now: str, *, title: str | None,
                    body: str | None, url: str | None, meta: dict[str, Any],
                    remote_updated_at: str | None, hidden: bool) -> tuple[bool, list[str]]:
    """Append a revision if content changed. Returns (created, withheld_fields)."""
    prev = conn.execute(
        "SELECT * FROM revisions WHERE object_id=? ORDER BY seq DESC LIMIT 1", (object_id,)
    ).fetchone()
    # A copy older than what we already have (e.g. a server that hasn't received
    # an edit yet) is stale, not a revert: an edited object always carries an
    # edit timestamp, so no timestamp or an earlier one means "not caught up".
    if prev is not None and prev["remote_updated_at"] and (
            not remote_updated_at or remote_updated_at < prev["remote_updated_at"]):
        return False, []
    withheld: list[str] = []
    values = {"title": title, "body": body, "url": url}
    for name in ("title", "body", "url"):
        if _is_redacted(values[name], hidden):
            if prev is not None and prev[name]:
                values[name] = prev[name]  # keep last observed text; remote is withholding it
                withheld.append(name)
            elif values[name]:
                withheld.append(name)  # e.g. first seen already as "*Permanently Deleted*"
    h = content_hash(values["title"], values["body"], values["url"], meta)
    if prev is not None and prev["content_hash"] == h:
        return False, withheld
    rev_meta = dict(meta)
    if prev is None and withheld:
        rev_meta["_first_observed_withheld"] = withheld
    seq = (prev["seq"] + 1) if prev is not None else 1
    conn.execute(
        "INSERT INTO revisions(object_id, seq, observed_at, remote_updated_at, title, body, url, "
        "metadata_json, content_hash) VALUES (?,?,?,?,?,?,?,?,?)",
        (object_id, seq, now, remote_updated_at, values["title"], values["body"], values["url"],
         dumps(rev_meta), h),
    )
    media.register(conn, object_id, media.media_candidates(values["title"], values["body"], values["url"]), now)
    if seq == 1:
        conn.execute("UPDATE objects SET revision_count=1 WHERE id=?", (object_id,))
    else:
        conn.execute("UPDATE objects SET revision_count=?, last_changed_at=? WHERE id=?",
                     (seq, now, object_id))
    if title is not None:  # a post: its link or text may now match other posts
        dupes.refresh_key(conn, object_id)
    return True, withheld


# --- object state ----------------------------------------------------------

_FLAG_EVENTS = {
    "deleted": ("author_deleted", "author_restored"),
    "removed": ("removed", "restored"),
    "locked": ("locked", "unlocked"),
    "featured": ("pinned", "unpinned"),
}


def _apply_flags(conn: Conn, obj: Any | None, object_id: int, thread_id: int,
                 object_type: str, local_id: str, flags: dict[str, bool], now: str,
                 withheld: list[str], result: ApplyResult) -> None:
    for flag, value in flags.items():
        before = bool(obj[f"cur_{flag}"]) if obj is not None else False
        first = obj is None
        if value == before:
            continue
        on_event, off_event = _FLAG_EVENTS[flag]
        etype = on_event if value else off_event
        meta: dict[str, Any] = {}
        if first:
            meta["state_at_first_observation"] = True
        if value and withheld and flag in ("deleted", "removed"):
            meta["remote_content_withheld"] = withheld
        attribution = None
        if flag == "deleted":
            attribution = "author"
        elif flag in ("removed", "locked"):
            attribution = "unknown"
            meta["modlog"] = "pending"
        eid = add_event(conn, etype, now, object_id=object_id, thread_id=thread_id,
                        attribution=attribution, metadata=meta)
        result.new_events += 1
        if flag in ("removed", "locked"):
            result.mod_lookups.append(PendingModLookup(eid, object_id, object_type, local_id, etype))
        conn.execute(f"UPDATE objects SET cur_{flag}=?, last_changed_at=? WHERE id=?",
                     (int(value), now, object_id))


def _set_local_id(conn: Conn, object_id: int, domain: str, local_id: str) -> None:
    conn.execute(
        "INSERT INTO object_local_ids(object_id, domain, local_id) VALUES (?,?,?) "
        "ON CONFLICT(domain, local_id) DO UPDATE SET object_id=excluded.object_id",
        (object_id, domain, local_id),
    )


def _upsert_object(conn: Conn, *, ap_id: str, object_type: str, thread_id: int,
                   parent_id: int | None, root_post_id: int | None, author_id: int | None,
                   community_id: int, created_at: str | None, updated_at: str | None,
                   score: int | None, reply_count: int | None, now: str, initial: bool,
                   result: ApplyResult, upvotes: int | None = None,
                   downvotes: int | None = None) -> tuple[int, Any | None]:
    obj = conn.execute("SELECT * FROM objects WHERE canonical_ap_id=?", (ap_id,)).fetchone()
    if obj is None:
        cur = conn.execute(
            "INSERT INTO objects(canonical_ap_id, object_type, parent_id, root_post_id, thread_id, author_id, "
            "community_id, created_at, remote_updated_at, first_seen_at, last_seen_at, discovered_late, "
            "score, reply_count, upvotes, downvotes) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ap_id, object_type, parent_id, root_post_id, thread_id, author_id, community_id, created_at,
             updated_at, now, now, int(not initial), score, reply_count, upvotes, downvotes),
        )
        oid = cur.lastrowid
        result.new_objects += 1
        if not initial:
            add_event(conn, "discovered", now, object_id=oid, thread_id=thread_id,
                      remote_timestamp=created_at)
            result.new_events += 1
        return oid, None
    oid = obj["id"]
    conn.execute(
        "UPDATE objects SET last_seen_at=?, remote_updated_at=COALESCE(?, remote_updated_at), score=?, "
        "reply_count=?, upvotes=?, downvotes=?, parent_id=COALESCE(parent_id, ?), "
        "thread_id=COALESCE(thread_id, ?) WHERE id=?",
        (now, updated_at, score, reply_count, upvotes, downvotes, parent_id, thread_id, oid),
    )
    if obj["cur_missing"]:
        conn.execute("UPDATE objects SET cur_missing=0, last_changed_at=? WHERE id=?", (now, oid))
        add_event(conn, "reappeared", now, object_id=oid, thread_id=thread_id)
        result.new_events += 1
    return oid, obj


def set_local_id(conn: Conn, object_id: int, domain: str, local_id: str) -> None:
    _set_local_id(conn, object_id, domain, local_id)


def apply_post(conn: Conn, thread_id: int, post: NPost, source_domain: str, now: str,
               initial: bool, result: ApplyResult) -> int:
    cid = upsert_community(conn, post.community, now)
    aid = upsert_actor(conn, post.author, now)
    oid, obj = _upsert_object(
        conn, ap_id=post.ap_id, object_type="post", thread_id=thread_id, parent_id=None,
        root_post_id=None, author_id=aid, community_id=cid, created_at=post.created_at,
        updated_at=post.updated_at, score=post.score, reply_count=post.comment_count, now=now,
        initial=initial, result=result, upvotes=post.upvotes, downvotes=post.downvotes,
    )
    conn.execute("UPDATE objects SET root_post_id=? WHERE id=?", (oid, oid))
    thumb = post.thumbnail_url if (post.thumbnail_url or "").startswith(("http://", "https://")) else None
    if thumb:
        conn.execute("UPDATE objects SET thumbnail_url=? WHERE id=?", (thumb, oid))
        media.register(conn, oid, [thumb], now)
    media.register(conn, oid, post.gallery, now)
    _set_local_id(conn, oid, source_domain, post.local_id)
    hidden = post.deleted or post.removed
    created, withheld = record_revision(
        conn, oid, now, title=post.title, body=post.body, url=post.url, meta=post.metadata,
        remote_updated_at=post.updated_at, hidden=hidden,
    )
    result.new_revisions += int(created)
    _apply_flags(conn, obj, oid, thread_id, "post", post.local_id,
                 {"deleted": post.deleted, "removed": post.removed, "locked": post.locked,
                  "featured": post.featured},
                 now, withheld, result)
    result.object_ids[post.ap_id] = oid
    return oid


def apply_comments(conn: Conn, thread_id: int, root_id: int, community_id: int,
                   comments: list[NComment], source_domain: str, now: str, initial: bool,
                   complete: bool, result: ApplyResult) -> None:
    by_local = {c.local_id: c for c in comments}

    def depth(c: NComment) -> int:
        d, cur, seen = 0, c, set()
        while cur.parent_local_id and cur.parent_local_id in by_local and cur.local_id not in seen:
            seen.add(cur.local_id)
            cur = by_local[cur.parent_local_id]
            d += 1
        return d

    local_to_oid: dict[str, int] = {}
    for c in sorted(comments, key=depth):  # parents before children
        parent_id = root_id
        if c.parent_local_id:
            parent_id = local_to_oid.get(c.parent_local_id)
            if parent_id is None:
                row = conn.execute(
                    "SELECT object_id FROM object_local_ids WHERE domain=? AND local_id=?",
                    (source_domain, c.parent_local_id),
                ).fetchone()
                parent_id = row["object_id"] if row else None  # orphan: parent unknown so far
        local_to_oid[c.local_id] = apply_comment(conn, thread_id, root_id, community_id, c, parent_id,
                                                 source_domain, now, initial, result)

    if complete:
        # Only objects previously seen on *this* server can go missing from it.
        # (Your own comment recorded from your account's server may simply not
        # have federated here yet.)
        seen = set(result.object_ids.values())
        for row in conn.execute(
            "SELECT o.id FROM objects o JOIN object_local_ids l ON l.object_id=o.id AND l.domain=? "
            "WHERE o.thread_id=? AND o.object_type='comment' AND o.cur_missing=0",
            (source_domain, thread_id),
        ).fetchall():
            if row["id"] not in seen:
                mark_missing(conn, row["id"], thread_id, now, result)


def apply_comment(conn: Conn, thread_id: int, root_id: int, community_id: int, c: NComment,
                  parent_id: int | None, source_domain: str, now: str, initial: bool,
                  result: ApplyResult) -> int:
    """Record one observed comment whose parent object is already known."""
    aid = upsert_actor(conn, c.author, now)
    oid, obj = _upsert_object(
        conn, ap_id=c.ap_id, object_type="comment", thread_id=thread_id, parent_id=parent_id,
        root_post_id=root_id, author_id=aid, community_id=community_id, created_at=c.created_at,
        updated_at=c.updated_at, score=c.score, reply_count=c.reply_count, now=now,
        initial=initial, result=result, upvotes=c.upvotes, downvotes=c.downvotes,
    )
    _set_local_id(conn, oid, source_domain, c.local_id)
    created, withheld = record_revision(
        conn, oid, now, title=None, body=c.body, url=None, meta=c.metadata,
        remote_updated_at=c.updated_at, hidden=c.deleted or c.removed,
    )
    result.new_revisions += int(created)
    _apply_flags(conn, obj, oid, thread_id, "comment", c.local_id,
                 {"deleted": c.deleted, "removed": c.removed}, now, withheld, result)
    result.object_ids[c.ap_id] = oid
    return oid


def mark_missing(conn: Conn, object_id: int, thread_id: int, now: str,
                 result: ApplyResult | None = None) -> None:
    row = conn.execute("SELECT cur_missing FROM objects WHERE id=?", (object_id,)).fetchone()
    if row is None or row["cur_missing"]:
        return
    conn.execute("UPDATE objects SET cur_missing=1, last_changed_at=? WHERE id=?", (now, object_id))
    add_event(conn, "missing", now, object_id=object_id, thread_id=thread_id, attribution="unknown",
              reason="No longer returned by the source server. Cause unknown: possibly purged, "
                     "account removal, or a federation gap.")
    if result:
        result.new_events += 1


# --- purge (the single destructive path) -----------------------------------

def purge_thread(conn: Conn, thread_id: int, now: str, media_dir: Path | None = None,
                 reason: str = "auto_capture_expired") -> list[Path]:
    """Delete an expired auto-captured thread. Returns media files that are no
    longer referenced by anything, for the caller to unlink after commit."""
    t = conn.execute("SELECT * FROM archived_threads WHERE id=?", (thread_id,)).fetchone()
    if t is None:
        return []
    if t["retention"] != "auto" and not t["trashed_at"]:
        raise PermissionError("Refusing to purge a kept thread that is not in the trash")
    root = conn.execute("SELECT canonical_ap_id FROM objects WHERE id=?", (t["root_object_id"],)).fetchone()
    ids = [r["id"] for r in conn.execute("SELECT id FROM objects WHERE thread_id=?", (thread_id,))]
    conn.execute("UPDATE archived_threads SET root_object_id=NULL WHERE id=?", (thread_id,))
    conn.execute("DELETE FROM state_events WHERE thread_id=?", (thread_id,))
    if ids:
        marks = ",".join("?" * len(ids))
        conn.execute(f"DELETE FROM state_events WHERE object_id IN ({marks})", ids)
        conn.execute(f"DELETE FROM revisions WHERE object_id IN ({marks})", ids)
        conn.execute(f"DELETE FROM object_local_ids WHERE object_id IN ({marks})", ids)
        conn.execute(f"DELETE FROM media_refs WHERE object_id IN ({marks})", ids)
        conn.execute(f"UPDATE objects SET parent_id=NULL, root_post_id=NULL WHERE id IN ({marks})", ids)
        conn.execute(f"DELETE FROM objects WHERE id IN ({marks})", ids)
    conn.execute("DELETE FROM archived_threads WHERE id=?", (thread_id,))
    add_event(conn, reason, now, community_id=t["community_id"],
              metadata={"root_ap_id": root["canonical_ap_id"] if root else None, "retention": t["retention"],
                        "captured_at": t["retained_at"], "objects_purged": len(ids)})
    return media.collect_orphans(conn, media_dir) if media_dir else []
