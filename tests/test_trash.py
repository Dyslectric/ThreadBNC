from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from threadbnc import store
from threadbnc.db import utcnow
from threadbnc.web import create_app

from .conftest import DOMAIN


def one(b, sql, *args):
    with b.db.connect() as conn:
        return conn.execute(sql, args).fetchone()


def kept_thread(server, bouncer, pid="1"):
    server.add_post(pid, "kept", "body", created=utcnow())
    server.add_comment(pid, pid + "0", "a comment")
    return bouncer.ingest_url(f"https://{DOMAIN}/post/{pid}")


def test_trash_and_restore_kept_thread(server, bouncer):
    tid = kept_thread(server, bouncer)
    bouncer.move_to_trash(tid)
    t = one(bouncer, "SELECT * FROM archived_threads WHERE id=?", tid)
    assert t["trashed_at"] and t["trash_expires_at"] and t["active"] == 0 and t["retention"] == "manual"
    assert bouncer.restore_from_trash(tid) == "manual"
    t = one(bouncer, "SELECT * FROM archived_threads WHERE id=?", tid)
    assert t["trashed_at"] is None and t["active"] == 1


def test_trash_period_expiry_purges_kept_thread(server, bouncer):
    tid = kept_thread(server, bouncer)
    other = kept_thread(server, bouncer, "2")
    bouncer.move_to_trash(tid)
    assert bouncer.purge_expired() == 0  # not yet due
    with bouncer.db.transaction() as conn:
        conn.execute("UPDATE archived_threads SET trash_expires_at='2000-01-01T00:00:00.000000Z' WHERE id=?", (tid,))
    assert bouncer.purge_expired() == 1
    assert one(bouncer, "SELECT COUNT(*) FROM archived_threads WHERE id=?", tid)[0] == 0
    assert one(bouncer, "SELECT COUNT(*) FROM objects WHERE thread_id=?", other)[0] == 2
    assert one(bouncer, "SELECT event_type FROM state_events WHERE event_type='trash_expired'")


def test_kept_thread_outside_trash_still_protected(server, bouncer):
    tid = kept_thread(server, bouncer)
    with bouncer.db.transaction() as conn:
        with pytest.raises(PermissionError):
            store.purge_thread(conn, tid, utcnow())


def test_forever_trash_never_expires(server, bouncer):
    tid = kept_thread(server, bouncer)
    bouncer.set_trash_days(None)
    bouncer.move_to_trash(tid)
    assert one(bouncer, "SELECT trash_expires_at FROM archived_threads WHERE id=?", tid)[0] is None
    bouncer.set_trash_days(3)  # recomputes for already-trashed threads
    assert one(bouncer, "SELECT trash_expires_at FROM archived_threads WHERE id=?", tid)[0] is not None
    assert bouncer.delete_from_trash() == 1
    assert one(bouncer, "SELECT COUNT(*) FROM objects")[0] == 0


def test_rearchiving_url_restores_as_kept(server, bouncer):
    tid = kept_thread(server, bouncer)
    bouncer.move_to_trash(tid)
    assert bouncer.ingest_url(f"https://{DOMAIN}/post/1") == tid
    assert one(bouncer, "SELECT trashed_at FROM archived_threads WHERE id=?", tid)[0] is None


def test_restoring_lapsed_auto_thread_keeps_it(server, bouncer):
    cid = bouncer.follow_community(f"!math@{DOMAIN}", 10, 1)
    server.add_post("5", "auto", "x", created=utcnow())
    bouncer.poll_follow(cid)
    tid = one(bouncer, "SELECT id FROM archived_threads")[0]
    bouncer.move_to_trash(tid)
    with bouncer.db.transaction() as conn:  # its auto window ran out while trashed
        conn.execute("UPDATE archived_threads SET retained_at='2000-01-01T00:00:00.000000Z'")
    assert bouncer.purge_expired() == 0  # trash period governs while trashed
    assert bouncer.restore_from_trash(tid) == "manual"
    assert one(bouncer, "SELECT expires_at FROM archived_threads")[0] is None


def test_trashed_auto_post_not_recaptured(server, bouncer):
    cid = bouncer.follow_community(f"!math@{DOMAIN}", 10, 1)
    server.add_post("5", "auto", "x", created=utcnow())
    bouncer.poll_follow(cid)
    tid = one(bouncer, "SELECT id FROM archived_threads")[0]
    bouncer.move_to_trash(tid)
    assert bouncer.poll_follow(cid) == 0


def test_trash_ui_flow(settings, server, bouncer):
    tid = kept_thread(server, bouncer)
    client = TestClient(create_app(settings, bouncer))
    client.post("/login", data={"password": "pw"})
    client.post(f"/t/{tid}/trash")
    assert "0 kept threads" in client.get("/kept").text
    page = client.get("/trash").text
    assert 'aria-label="1 in the trash"' in page and "was kept" in page
    assert "In the trash" in client.get(f"/t/{tid}").text
    r = client.post(f"/t/{tid}/delete", data={"confirm": "nope"}, follow_redirects=False)
    assert one(bouncer, "SELECT COUNT(*) FROM archived_threads")[0] == 1
    assert "Delete permanently?" in client.get(f"/t/{tid}/delete").text
    client.post(f"/t/{tid}/delete", data={"confirm": "delete"})
    assert one(bouncer, "SELECT COUNT(*) FROM archived_threads")[0] == 0
    assert "The trash is empty" in client.get("/trash").text
