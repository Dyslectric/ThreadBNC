from __future__ import annotations

from threadbnc import integrity
from threadbnc.web import create_app

from fastapi.testclient import TestClient

from .test_media import community_with, tbouncer  # noqa: F401


def report(bouncer, deep=False):
    with bouncer.db.connect() as conn:
        return integrity.check(bouncer.db, conn, bouncer.media_dir, deep)


def test_integrity_check_accepts_a_healthy_archive(server, tbouncer):
    community_with(server, tbouncer, "![a](https://img.test/cat.gif)")
    tbouncer.media.fetch_pending()
    checked = report(tbouncer, deep=True)
    assert checked.ok and checked.database_check == "ok"
    assert checked.media_rows == checked.media_files == checked.checked_hashes == 1
    assert checked.issues == []


def test_integrity_finds_missing_and_unreferenced_media(server, tbouncer):
    community_with(server, tbouncer, "![a](https://img.test/cat.gif)")
    tbouncer.media.fetch_pending()
    with tbouncer.db.connect() as conn:
        rel = conn.execute("SELECT storage_path FROM media WHERE status='ok'").fetchone()[0]
    (tbouncer.media_dir / rel).unlink()
    extra = tbouncer.media_dir / "aa" / "bb" / "orphan.bin"
    extra.parent.mkdir(parents=True, exist_ok=True)
    extra.write_bytes(b"orphan")
    checked = report(tbouncer)
    assert not checked.ok
    assert {issue.kind for issue in checked.issues} == {"missing media", "unreferenced media"}


def test_deep_integrity_detects_same_size_corruption(server, tbouncer):
    community_with(server, tbouncer, "![a](https://img.test/cat.gif)")
    tbouncer.media.fetch_pending()
    with tbouncer.db.connect() as conn:
        rel = conn.execute("SELECT storage_path FROM media WHERE status='ok'").fetchone()[0]
    path = tbouncer.media_dir / rel
    original = path.read_bytes()
    path.write_bytes(bytes([original[0] ^ 0xFF]) + original[1:])
    assert report(tbouncer).ok
    checked = report(tbouncer, deep=True)
    assert not checked.ok and [issue.kind for issue in checked.issues] == ["media checksum"]


def test_integrity_page(settings, bouncer):
    client = TestClient(create_app(settings, bouncer))
    client.post("/login", data={"password": "pw"})
    page = client.get("/storage/integrity?deep=1").text
    assert "No integrity errors found" in page and "file checksum" in page
