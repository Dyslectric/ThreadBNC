from __future__ import annotations

from fastapi.testclient import TestClient

from threadbnc.web import create_app


def signed_in(settings, bouncer):
    client = TestClient(create_app(settings, bouncer))
    assert client.post("/login", data={"password": "pw"}, follow_redirects=False).status_code == 303
    return client


def test_pages_are_timed_revalidated_and_compressed(settings, bouncer):
    client = signed_in(settings, bouncer)
    first = client.get("/communities", headers={"Accept-Encoding": "gzip"})
    assert first.status_code == 200
    assert "total;dur=" in first.headers["server-timing"] and "database" in first.headers["server-timing"]
    assert first.headers["cache-control"] == "private, no-cache"
    assert first.headers["content-encoding"] == "gzip"
    again = client.get("/communities", headers={"If-None-Match": first.headers["etag"]})
    assert again.status_code == 304 and not again.content


def test_only_pages_that_read_are_prefetched(settings, bouncer):
    client = signed_in(settings, bouncer)
    ahead = client.get("/communities", headers={"X-ThreadBNC-Prefetch": "1"})
    assert ahead.status_code == 200 and "max-age=" in ahead.headers["cache-control"]
    assert client.get("/kept", headers={"X-ThreadBNC-Prefetch": "1"}).status_code == 204
    assert client.get("/t/1", headers={"X-ThreadBNC-Prefetch": "1"}).status_code == 204


def test_settings_are_read_again_after_a_write(settings, bouncer):
    db = bouncer.db
    with db.transaction() as conn:
        conn.execute("INSERT INTO app_settings(key, value) VALUES ('probe', '1')")
    assert db.get_setting("probe") == "1"
    with db.connect() as conn:  # a write that isn't in a transaction() counts too
        conn.execute("UPDATE app_settings SET value='2' WHERE key='probe'")
    assert db.get_setting("probe") == "2"


def test_urgent_jobs_run_without_the_main_loop(bouncer):
    """Comments someone is waiting on aren't queued behind whatever the main loop is busy with."""
    import threading
    import time

    worker = threading.Thread(target=bouncer.run_urgent_forever, daemon=True)
    worker.start()
    try:
        job = bouncer.enqueue("open", {"thread_ids": []})
        deadline = time.monotonic() + 5
        status = None
        while time.monotonic() < deadline:
            with bouncer.db.connect() as conn:
                status = conn.execute("SELECT status FROM jobs WHERE id=?", (job,)).fetchone()["status"]
            if status == "done":
                break
            time.sleep(0.05)
        assert status == "done"
        other = bouncer.enqueue("articles", {"thread_ids": []})  # not urgent: left for the main loop
        time.sleep(0.5)
        with bouncer.db.connect() as conn:
            assert conn.execute("SELECT status FROM jobs WHERE id=?", (other,)).fetchone()["status"] == "queued"
    finally:
        bouncer.stop()
        worker.join(timeout=5)
