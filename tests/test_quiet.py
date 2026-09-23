"""What the bouncer doesn't do on its own: read comments, look posts up on other
servers, or fetch linked articles. Those wait until you open or keep a post."""

from __future__ import annotations

import pytest

from .conftest import DOMAIN, FakeAdapter


@pytest.fixture
def calls(monkeypatch):
    seen: list[str] = []
    for name in ("fetch_comments", "fetch_post", "resolve_ap_id"):
        real = getattr(FakeAdapter, name)

        def spy(self, *args, _real=real, _name=name, **kwargs):
            seen.append(f"{_name} {self.domain}")
            return _real(self, *args, **kwargs)

        monkeypatch.setattr(FakeAdapter, name, spy)
    return seen


def one(b, sql, *args):
    with b.db.connect() as conn:
        return conn.execute(sql, args).fetchone()


def test_capturing_a_post_reads_only_the_listing(server, bouncer, calls):
    server.add_post("1", "hello", "b", created="2099-01-01T00:00:00.000000Z")
    server.add_comment("1", "10", "a comment")
    cid = bouncer.follow_community(f"!math@{DOMAIN}", 15, 30)
    assert bouncer.poll_follow(cid) == 1
    assert calls == []  # no comments, no looking it up anywhere else
    t = one(bouncer, "SELECT * FROM archived_threads")
    assert t["last_full_fetch_at"] is None
    assert one(bouncer, "SELECT COUNT(*) FROM objects WHERE object_type='comment'")[0] == 0
    bouncer.open_threads([t["id"]])  # opening it reads them
    assert one(bouncer, "SELECT COUNT(*) FROM objects WHERE object_type='comment'")[0] == 1


def test_the_bouncer_never_rereads_comments_on_its_own(server, bouncer, calls):
    server.add_post("1", "hello", "b")
    tid = bouncer.ingest_url(f"https://{DOMAIN}/post/1")
    with bouncer.db.transaction() as conn:
        conn.execute("UPDATE archived_threads SET next_check_at='2000-01-01T00:00:00.000000Z', "
                     "last_full_fetch_at='2000-01-01T00:00:00.000000Z' WHERE id=?", (tid,))
    calls.clear()
    bouncer.tick()
    assert [c for c in calls if c.startswith("fetch_comments")] == []


def test_linked_articles_wait_to_be_opened(server, bouncer, monkeypatch):
    fetched = []
    monkeypatch.setattr(bouncer.articles, "fetch_one", lambda row: fetched.append(row["url"]))
    server.add_post("1", "news", "")
    server.edit_post("1", url="https://news.test/story")
    cid = bouncer.follow_community(f"!math@{DOMAIN}", 15, 30, backfill=True)
    bouncer.poll_follow(cid)
    bouncer.tick()
    assert fetched == []
    assert one(bouncer, "SELECT status FROM articles")[0] == "pending"
    bouncer.open_threads([one(bouncer, "SELECT id FROM archived_threads")[0]])
    assert fetched == ["https://news.test/story"]
