from __future__ import annotations

from fastapi.testclient import TestClient

from threadbnc.web import create_app

from .conftest import DOMAIN


def make_client(settings, bouncer):
    return TestClient(create_app(settings, bouncer))


def login(client):
    r = client.post("/login", data={"password": "pw"}, follow_redirects=False)
    assert r.status_code == 303


def test_everything_requires_auth(settings, bouncer):
    client = make_client(settings, bouncer)
    for path in ["/", "/communities", "/t/1", "/o/1/history", "/changes", "/c/1"]:
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login", path
    r = client.post("/archive", json={"url": "https://x/post/1"})
    assert r.status_code == 401
    assert client.get("/robots.txt").text.strip().endswith("Disallow: /")
    assert "noindex" in client.get("/login").headers["x-robots-tag"]


def test_wrong_password(settings, bouncer):
    client = make_client(settings, bouncer)
    r = client.post("/login", data={"password": "nope"})
    assert "Wrong password" in r.text
    assert client.get("/", follow_redirects=False).status_code == 303


def test_api_token_archive(settings, bouncer, server):
    server.add_post("1", "Hello", "body")
    client = make_client(settings, bouncer)
    r = client.post("/archive", json={"url": f"https://{DOMAIN}/post/1"},
                    headers={"Authorization": "Bearer tok"})
    assert r.status_code == 202
    bouncer.run_one_job()
    r = client.get(f"/api/jobs/{r.json()['job_id']}", headers={"Authorization": "Bearer tok"})
    assert r.json()["status"] == "done"


def test_pages_render_with_history(settings, bouncer, server):
    server.add_post("1", "Question about topology", "Is a donut a mug?")
    server.add_comment("1", "10", "I think the answer is A.")
    tid = bouncer.ingest_url(f"https://{DOMAIN}/post/1")
    server.edit_comment("1", "10", body="I think the answer is B.")
    server.add_comment("1", "11", "<script>alert(1)</script>", parent="10")
    bouncer.sync_thread(tid)
    server.edit_comment("1", "10", deleted=True, body="")
    bouncer.sync_thread(tid)

    client = make_client(settings, bouncer)
    login(client)
    kept = client.get("/kept")
    assert "Question about topology" in kept.text and "Deleted by author" in kept.text
    comms = client.get("/communities")
    assert "!math" in comms.text
    page = client.get(f"/t/{tid}")
    assert page.status_code == 200
    assert "I think the answer is B." in page.text
    assert "Edited 1 time" in page.text
    assert "Deleted by author" in page.text
    assert "<script>alert(1)</script>" not in page.text and "&lt;script&gt;" in page.text
    with bouncer.db.connect() as conn:
        oid = conn.execute("SELECT id FROM objects WHERE canonical_ap_id=?",
                           (f"https://{DOMAIN}/comment/10",)).fetchone()[0]
    hist = client.get(f"/o/{oid}/history")
    assert "I think the answer is A." in hist.text and "Version 2" in hist.text
    cpage = client.get("/c/1?tab=kept")
    assert cpage.status_code == 200 and "Question about topology" in cpage.text


def test_follow_flow_and_live_tab(settings, bouncer, server):
    server.add_post("1", "Live post", "b")
    client = make_client(settings, bouncer)
    login(client)
    r = client.post("/follow", data={"community": f"!math@{DOMAIN}", "poll_interval_minutes": "5",
                                     "retention_days": "7"}, follow_redirects=False)
    assert r.status_code == 303
    cid = r.headers["location"].rsplit("/", 1)[1]
    page = client.get(f"/c/{cid}")
    assert "Following" in page.text and 'value="7"' in page.text
    live = client.get(f"/c/{cid}?tab=live")
    assert "Live post" in live.text and "Keep" in live.text
    r = client.post(f"/c/{cid}/follow-settings", data={"poll_interval_minutes": "60",
                                                       "retention_days": "forever"})
    assert r.status_code == 200
    assert 'value="forever"' in client.get("/communities").text


def test_comment_sorting(settings, bouncer, server):
    server.add_post("1", "Sorting", "b")
    server.add_comment("1", "10", "OLDEST low score")
    server.edit_comment("1", "10", created_at="2026-09-01T01:00:00.000000Z", score=1, upvotes=1, downvotes=0)
    server.add_comment("1", "11", "MIDDLE top score")
    server.edit_comment("1", "11", created_at="2026-09-02T01:00:00.000000Z", score=50, upvotes=60, downvotes=10)
    server.add_comment("1", "12", "NEWEST controversial")
    server.edit_comment("1", "12", created_at="2026-09-03T01:00:00.000000Z", score=0, upvotes=30, downvotes=30)
    tid = bouncer.ingest_url(f"https://{DOMAIN}/post/1")
    client = make_client(settings, bouncer)
    login(client)

    def order(sort=None):
        html = client.get(f"/t/{tid}" + (f"?sort={sort}" if sort else "")).text
        names = ["OLDEST", "MIDDLE", "NEWEST"]
        return sorted(names, key=html.index)

    assert order("old") == ["OLDEST", "MIDDLE", "NEWEST"]
    assert order("new") == ["NEWEST", "MIDDLE", "OLDEST"]
    assert order("top") == ["MIDDLE", "OLDEST", "NEWEST"]
    assert order("controversial")[0] == "NEWEST"
    assert order("old") == ["OLDEST", "MIDDLE", "NEWEST"]
    assert order() == ["OLDEST", "MIDDLE", "NEWEST"]  # remembered choice
