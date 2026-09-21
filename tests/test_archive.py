from __future__ import annotations

import pytest

from threadbnc.adapters import ModAction, RemoteUnavailable, parse_community_ref, parse_thread_url
from threadbnc.db import utcnow

from .conftest import DOMAIN, MOD


def rows(bouncer, sql, *args):
    with bouncer.db.connect() as conn:
        return conn.execute(sql, args).fetchall()


def events(bouncer, ap_id):
    return [r["event_type"] for r in rows(
        bouncer, "SELECT e.event_type FROM state_events e JOIN objects o ON o.id=e.object_id "
                 "WHERE o.canonical_ap_id=? ORDER BY e.id", ap_id)]


def bodies(bouncer, ap_id):
    return [r["body"] for r in rows(
        bouncer, "SELECT r.body FROM revisions r JOIN objects o ON o.id=r.object_id "
                 "WHERE o.canonical_ap_id=? ORDER BY r.seq", ap_id)]


def setup_thread(server, bouncer):
    server.add_post("1", "Question about topology", "Is a donut a mug?")
    server.add_comment("1", "10", "I think the answer is A.")
    server.add_comment("1", "11", "Reply to 10", parent="10")
    server.add_comment("1", "12", "Reply to 11", parent="11")
    return bouncer.ingest_url(f"https://{DOMAIN}/post/1")


# ---- URL parsing -----------------------------------------------------------

@pytest.mark.parametrize("url,kind,lid", [
    ("https://lemmy.world/post/123", "post", "123"),
    ("https://lemmy.world/post/123/456", "post", "123"),
    ("https://lemmy.world/comment/456", "comment", "456"),
    ("https://piefed.social/c/tech@piefed.social/p/99/some-slug", "post", "99"),
    ("https://piefed.social/post/99", "post", "99"),
])
def test_parse_thread_url(url, kind, lid):
    ref = parse_thread_url(url)
    assert (ref.kind, ref.local_id) == (kind, lid)


def test_parse_thread_url_rejects_other_paths():
    with pytest.raises(ValueError):
        parse_thread_url("https://lemmy.world/c/technology")


def test_parse_community_ref_keeps_instances_distinct():
    a = parse_community_ref("!technology@example.org")
    b = parse_community_ref("!technology@example.net")
    assert (a.domain, a.name) == ("example.org", "technology")
    assert a.domain != b.domain
    c = parse_community_ref("https://lemmy.world/c/tech@beehaw.org")
    assert (c.domain, c.name, c.home, c.qualified) == ("lemmy.world", "tech", "beehaw.org", "tech@beehaw.org")


# ---- ingest ----------------------------------------------------------------

def test_ingest_stores_full_tree_and_community(server, bouncer):
    tid = setup_thread(server, bouncer)
    objs = rows(bouncer, "SELECT canonical_ap_id, parent_id, object_type FROM objects WHERE thread_id=? "
                         "ORDER BY id", tid)
    assert len(objs) == 4
    by_ap = {o["canonical_ap_id"]: o for o in rows(bouncer, "SELECT * FROM objects")}
    c12 = by_ap[f"https://{DOMAIN}/comment/12"]
    assert c12["parent_id"] == by_ap[f"https://{DOMAIN}/comment/11"]["id"]
    assert by_ap[f"https://{DOMAIN}/comment/10"]["parent_id"] == by_ap[f"https://{DOMAIN}/post/1"]["id"]
    comms = rows(bouncer, "SELECT * FROM communities")
    assert [c["canonical_ap_id"] for c in comms] == [f"https://{DOMAIN}/c/math"]
    # No 'discovered' events for the initial capture.
    assert events(bouncer, f"https://{DOMAIN}/comment/10") == []


def test_ingest_is_idempotent(server, bouncer):
    tid = setup_thread(server, bouncer)
    assert bouncer.ingest_url(f"https://{DOMAIN}/post/1") == tid
    assert bouncer.ingest_url(f"https://{DOMAIN}/comment/11") == tid
    assert len(rows(bouncer, "SELECT * FROM objects")) == 4
    assert len(rows(bouncer, "SELECT * FROM archived_threads")) == 1


# ---- sync: edits, new comments ----------------------------------------------

def test_edit_creates_revision_and_keeps_old(server, bouncer):
    tid = setup_thread(server, bouncer)
    bouncer.sync_thread(tid)  # unchanged: no new revisions
    assert bodies(bouncer, f"https://{DOMAIN}/comment/10") == ["I think the answer is A."]
    server.edit_comment("1", "10", body="I think the answer is B.", updated_at="2026-09-02T00:00:00Z")
    bouncer.sync_thread(tid)
    assert bodies(bouncer, f"https://{DOMAIN}/comment/10") == ["I think the answer is A.",
                                                               "I think the answer is B."]


def test_new_comment_is_discovered(server, bouncer):
    tid = setup_thread(server, bouncer)
    server.add_comment("1", "13", "late reply", parent="10")
    res = bouncer.sync_thread(tid)
    assert res.new_objects == 1
    assert events(bouncer, f"https://{DOMAIN}/comment/13") == ["discovered"]


# ---- deletion / removal ------------------------------------------------------

def test_author_deletion_keeps_text(server, bouncer):
    tid = setup_thread(server, bouncer)
    server.edit_comment("1", "10", deleted=True, body="")  # Lemmy blanks content
    bouncer.sync_thread(tid)
    assert bodies(bouncer, f"https://{DOMAIN}/comment/10") == ["I think the answer is A."]
    assert events(bouncer, f"https://{DOMAIN}/comment/10") == ["author_deleted"]
    obj = rows(bouncer, "SELECT cur_deleted FROM objects WHERE canonical_ap_id=?", f"https://{DOMAIN}/comment/10")
    assert obj[0]["cur_deleted"] == 1


def test_account_deletion_marker_is_not_an_edit(server, bouncer):
    tid = setup_thread(server, bouncer)
    server.edit_comment("1", "11", deleted=True, body="*Permanently Deleted*")
    bouncer.sync_thread(tid)
    assert bodies(bouncer, f"https://{DOMAIN}/comment/11") == ["Reply to 10"]


def test_moderator_removal_and_restore_with_modlog(server, bouncer):
    tid = setup_thread(server, bouncer)
    server.edit_comment("1", "10", removed=True, body="")
    server.modlog.append(("comment", "10", ModAction("remove_comment", True, "2026-09-03T00:00:00.000000Z",
                                                     "Off-topic", MOD, "moderator")))
    bouncer.sync_thread(tid)
    ev = rows(bouncer, "SELECT e.* FROM state_events e JOIN objects o ON o.id=e.object_id "
                       "WHERE o.canonical_ap_id=?", f"https://{DOMAIN}/comment/10")
    assert [(e["event_type"], e["attribution"], e["reason"]) for e in ev] == [("removed", "moderator", "Off-topic")]
    server.edit_comment("1", "10", removed=False, body="I think the answer is A.")
    bouncer.sync_thread(tid)
    assert events(bouncer, f"https://{DOMAIN}/comment/10") == ["removed", "restored"]
    assert bodies(bouncer, f"https://{DOMAIN}/comment/10") == ["I think the answer is A."]


def test_removal_without_modlog_records_uncertainty(server, bouncer):
    tid = setup_thread(server, bouncer)
    server.edit_post("1", removed=True, body="")
    bouncer.sync_thread(tid)
    ev = rows(bouncer, "SELECT * FROM state_events WHERE event_type='removed'")
    assert ev[0]["attribution"] == "unknown"
    assert '"modlog": "no_matching_entry"' in ev[0]["metadata_json"]


def test_lock_and_unlock(server, bouncer):
    tid = setup_thread(server, bouncer)
    server.edit_post("1", locked=True)
    bouncer.sync_thread(tid)
    server.edit_post("1", locked=False)
    bouncer.sync_thread(tid)
    assert events(bouncer, f"https://{DOMAIN}/post/1") == ["locked", "unlocked"]


def test_vanished_comment_is_marked_missing_not_deleted(server, bouncer):
    tid = setup_thread(server, bouncer)
    del server.comments["1"]["12"]
    bouncer.sync_thread(tid)
    assert events(bouncer, f"https://{DOMAIN}/comment/12") == ["missing"]
    assert bodies(bouncer, f"https://{DOMAIN}/comment/12") == ["Reply to 11"]


def test_purged_post_marked_missing(server, bouncer):
    tid = setup_thread(server, bouncer)
    del server.posts["1"]
    bouncer.sync_thread(tid)
    assert events(bouncer, f"https://{DOMAIN}/post/1") == ["missing"]
    assert len(rows(bouncer, "SELECT * FROM objects")) == 4


def test_instance_outage_does_not_imply_deletion(server, bouncer):
    tid = setup_thread(server, bouncer)
    server.down = True
    with pytest.raises(RemoteUnavailable):
        bouncer.sync_thread(tid)
    t = rows(bouncer, "SELECT * FROM archived_threads WHERE id=?", tid)[0]
    assert t["consecutive_failures"] == 1 and t["next_check_at"] > utcnow()
    assert rows(bouncer, "SELECT event_type FROM state_events WHERE instance_id IS NOT NULL")[0][0] == \
        "instance_unavailable"
    assert rows(bouncer, "SELECT COUNT(*) FROM state_events WHERE object_id IS NOT NULL")[0][0] == 0
    server.down = False
    bouncer.sync_thread(tid)
    assert [r[0] for r in rows(bouncer, "SELECT event_type FROM state_events WHERE instance_id IS NOT NULL")] \
        == ["instance_unavailable", "instance_recovered"]


# ---- follows, auto-capture and expiry ---------------------------------------

def test_follow_autocaptures_new_posts_only(server, bouncer):
    server.add_post("1", "old post", "x", created="2020-01-01T00:00:00.000000Z")
    cid = bouncer.follow_community(f"!math@{DOMAIN}", 10, 7)
    server.add_post("2", "new post", "y", created=utcnow())
    assert bouncer.poll_follow(cid) == 1
    t = rows(bouncer, "SELECT * FROM archived_threads")
    assert len(t) == 1 and t[0]["retention"] == "auto" and t[0]["expires_at"] is not None


def test_backfill_captures_current_page(server, bouncer):
    server.add_post("1", "old post", "x", created="2020-01-01T00:00:00.000000Z")
    cid = bouncer.follow_community(f"!math@{DOMAIN}", 10, 7, backfill=True)
    assert bouncer.poll_follow(cid) == 1


def _expire_all(bouncer):
    with bouncer.db.transaction() as conn:
        conn.execute("UPDATE archived_threads SET expires_at='2000-01-01T00:00:00.000000Z' "
                     "WHERE retention='auto'")


def test_expired_auto_threads_are_purged_but_manual_never(server, bouncer):
    server.add_post("1", "kept", "x", created=utcnow())
    manual = bouncer.ingest_url(f"https://{DOMAIN}/post/1")
    cid = bouncer.follow_community(f"!math@{DOMAIN}", 10, 1)
    server.add_post("2", "auto", "y", created=utcnow())
    server.add_comment("2", "20", "comment on auto")
    bouncer.poll_follow(cid)
    assert len(rows(bouncer, "SELECT * FROM archived_threads")) == 2
    # Force every thread's expiry into the past, including (wrongly) the manual one.
    with bouncer.db.transaction() as conn:
        conn.execute("UPDATE archived_threads SET expires_at='2000-01-01T00:00:00.000000Z'")
    assert bouncer.purge_expired() == 1
    left = rows(bouncer, "SELECT id, retention FROM archived_threads")
    assert [(r["id"], r["retention"]) for r in left] == [(manual, "manual")]
    assert rows(bouncer, "SELECT COUNT(*) FROM objects WHERE thread_id=?", manual)[0][0] == 1
    assert rows(bouncer, "SELECT COUNT(*) FROM objects")[0][0] == 1
    assert rows(bouncer, "SELECT event_type FROM state_events WHERE event_type='auto_capture_expired'")


def test_keeping_an_auto_thread_protects_it(server, bouncer):
    cid = bouncer.follow_community(f"!math@{DOMAIN}", 10, 1)
    server.add_post("2", "auto", "y", created=utcnow())
    bouncer.poll_follow(cid)
    tid = bouncer.ingest_url(f"https://{DOMAIN}/post/2")  # manual archive of the same post
    t = rows(bouncer, "SELECT * FROM archived_threads WHERE id=?", tid)[0]
    assert t["retention"] == "manual" and t["expires_at"] is None and t["promoted_at"]
    _expire_all(bouncer)
    assert bouncer.purge_expired() == 0


def test_changing_retention_recomputes_expiry(server, bouncer):
    cid = bouncer.follow_community(f"!math@{DOMAIN}", 10, 1)
    server.add_post("2", "auto", "y", created=utcnow())
    bouncer.poll_follow(cid)
    bouncer.update_follow(cid, 10, None)
    assert rows(bouncer, "SELECT expires_at FROM archived_threads")[0][0] is None
    bouncer.update_follow(cid, 10, 3)
    assert rows(bouncer, "SELECT expires_at FROM archived_threads")[0][0] is not None


def test_job_queue_ingests(server, bouncer):
    server.add_post("1", "t", "b")
    jid = bouncer.enqueue("ingest", {"url": f"https://{DOMAIN}/post/1"})
    assert bouncer.run_one_job()
    job = rows(bouncer, "SELECT * FROM jobs WHERE id=?", jid)[0]
    assert job["status"] == "done"
