from __future__ import annotations

from threadbnc import media, storage
from threadbnc.db import utcnow

from .conftest import DOMAIN
from .test_media import GIF, community_with, logged_in, media_rows, tbouncer  # noqa: F401  (fixture)

SMALL_MP4 = 62  # bytes served for /small.mp4 (see test_media.remote_with_big)


def other_community_post(b, body_url: str) -> None:
    """A post in a second community that uses the same file."""
    now = utcnow()
    with b.db.transaction() as conn:
        cid = conn.execute("INSERT INTO communities(canonical_ap_id, name, first_seen_at, last_seen_at) "
                           "VALUES ('https://x.test/c/pics', 'pics', ?, ?)", (now, now)).lastrowid
        oid = conn.execute("INSERT INTO objects(canonical_ap_id, object_type, community_id, first_seen_at, "
                           "last_seen_at) VALUES ('https://x.test/post/9', 'post', ?, ?, ?)",
                           (cid, now, now)).lastrowid
        conn.execute("INSERT INTO revisions(object_id, seq, observed_at, title, body, content_hash) "
                     "VALUES (?, 1, ?, 'café', ?, 'h')", (oid, now, f"![x]({body_url})"))
        media.register(conn, oid, [body_url], now)


def test_breakdown_counts_shared_files_once_in_the_total(server, tbouncer):
    community_with(server, tbouncer, "![a](https://img.test/cat.gif) ![v](https://img.test/small.mp4)")
    other_community_post(tbouncer, "https://img.test/cat.gif")
    tbouncer.media.fetch_pending()
    assert {r["status"] for r in media_rows(tbouncer).values()} == {"ok"}
    with tbouncer.db.connect() as conn:
        u = storage.overview(tbouncer.db, conn, tbouncer.media_dir)

    e = u["everything"]
    assert e.media_files == 2 and e.media_bytes == len(GIF) + SMALL_MP4
    assert e.media["gifs"] == [1, len(GIF)] and e.media["videos"] == [1, SMALL_MP4]
    rows = {c["name"]: t for c, t in u["communities"]}
    assert rows["math"].media_files == 2 and rows["pics"].media["gifs"] == [1, len(GIF)]
    assert u["shared"] == len(GIF)
    # Text is counted in bytes: "café" is 5, and the body is the markdown as stored.
    assert rows["pics"].text == len("café".encode()) + len("![x](https://img.test/cat.gif)") + len("{}")
    assert e.text == rows["math"].text + rows["pics"].text
    assert [c["name"] for c, _t in u["communities"]][0] == "math"  # largest first
    kinds = {k["label"]: k for k in u["kinds"]}
    assert kinds["Posts"]["count"] == 2 and kinds["GIFs"]["bytes"] == len(GIF)
    threads = dict(u["threads"])
    assert threads["Kept"].threads == 1 and threads["Not in a thread"].media_files == 1
    assert u["database"] and u["database"] > 0


def test_trashed_threads_show_separately(server, tbouncer):
    community_with(server, tbouncer, "![a](https://img.test/cat.gif)")
    tbouncer.media.fetch_pending()
    with tbouncer.db.connect() as conn:
        tid = conn.execute("SELECT id FROM archived_threads").fetchone()[0]
    tbouncer.move_to_trash(tid)
    with tbouncer.db.connect() as conn:
        threads = dict(storage.overview(tbouncer.db, conn, tbouncer.media_dir)["threads"])
    assert "Kept" not in threads and threads["In the trash"].threads == 1
    assert threads["In the trash"].media["gifs"] == [1, len(GIF)]


def test_storage_page(settings, server, tbouncer):
    cid = community_with(server, tbouncer, "![a](https://img.test/cat.gif) ![v](https://img.test/small.mp4)")
    tbouncer.media.fetch_pending()
    client = logged_in(settings, tbouncer)
    page = client.get("/storage").text
    assert "<h1>Storage</h1>" in page and f'href="/c/{cid}?tab=media"' in page
    assert "1 GIF (56 bytes) · 1 video (62 bytes)" in page
    assert page.index('href="/storage"') < page.index('href="/trash"')  # in the menu, above Trash
    assert 'aria-label="GIFs: 56 bytes"' in page


def test_storage_page_when_empty(settings, server, tbouncer):
    page = logged_in(settings, tbouncer).get("/storage").text
    assert page.count("Nothing archived yet.") == 3


def test_human_sizes():
    from threadbnc.web import human_size
    assert [human_size(n) for n in (0, 999, 1500, 25_000_000, 1_234_567_890, 12_000_000_000)] == \
        ["0 bytes", "999 bytes", "1.5 KB", "25 MB", "1.2 GB", "12 GB"]
