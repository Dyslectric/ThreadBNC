from __future__ import annotations

import json
import zipfile

from fastapi.testclient import TestClient

from threadbnc import portable
from threadbnc.web import create_app

from .conftest import DOMAIN
from .test_media import GIF, community_with, tbouncer  # noqa: F401


def archived(bouncer, server):
    server.add_post("1", "Portable", "A saved discussion")
    return bouncer.ingest_url(f"https://{DOMAIN}/post/1")


def test_portable_export_is_complete_verifiable_and_credential_free(tmp_path, bouncer, server):
    archived(bouncer, server)
    with bouncer.db.transaction() as conn:
        conn.execute("INSERT INTO app_settings(key, value) VALUES ('a_secret', 'do-not-export')")
        conn.execute("INSERT INTO accounts(domain, username, actor_ap_id, token_enc, status, added_at) "
                     "VALUES ('home.test', 'me', 'https://home.test/u/me', 'encrypted-secret', 'ok', 'now')")
    output = tmp_path / "archive.zip"
    manifest = portable.create(bouncer.db, bouncer.media_dir, output)
    assert manifest["format"] == portable.FORMAT and manifest["credentials_included"] is False
    verified = portable.verify(output)
    assert verified["ok"] and verified["checked"] > 5

    with zipfile.ZipFile(output) as zf:
        names = set(zf.namelist())
        assert "data/revisions.jsonl" in names and "subscriptions.opml" in names
        assert "data/accounts.jsonl" not in names and "data/app_settings.jsonl" not in names
        contents = b"".join(zf.read(name) for name in names if name.endswith((".jsonl", ".json", ".txt")))
        assert b"do-not-export" not in contents and b"encrypted-secret" not in contents
        row = json.loads(zf.read("data/revisions.jsonl").splitlines()[0])
        assert row["title"] == "Portable"


def test_portable_verifier_rejects_non_zip(tmp_path):
    bad = tmp_path / "bad.zip"
    bad.write_bytes(b"not a zip")
    checked = portable.verify(bad)
    assert not checked["ok"] and "cannot read ZIP" in checked["issues"][0]


def test_portable_export_reports_missing_media(tmp_path, bouncer, server):
    archived(bouncer, server)
    with bouncer.db.transaction() as conn:
        conn.execute("INSERT INTO media(url, status, size_bytes, sha256, storage_path, first_seen_at) "
                     "VALUES ('https://example.test/lost.jpg', 'ok', 3, 'abc', 'aa/lost.jpg', 'now')")
    output = tmp_path / "incomplete.zip"
    manifest = portable.create(bouncer.db, bouncer.media_dir, output)
    assert manifest["missing_media"] == ["aa/lost.jpg"]
    assert not portable.verify(output)["ok"]


def test_portable_export_download(settings, bouncer, server):
    archived(bouncer, server)
    client = TestClient(create_app(settings, bouncer))
    client.post("/login", data={"password": "pw"})
    response = client.get("/export")
    assert response.status_code == 200 and response.content.startswith(b"PK")
    assert "threadbnc-" in response.headers["content-disposition"]
    assert not list(settings.data_dir.glob("threadbnc-export-*.zip"))


def test_portable_export_includes_archived_media(tmp_path, server, tbouncer):
    community_with(server, tbouncer, "![a](https://img.test/cat.gif)")
    tbouncer.media.fetch_pending()
    output = tmp_path / "with-media.zip"
    portable.create(tbouncer.db, tbouncer.media_dir, output)
    with zipfile.ZipFile(output) as zf:
        media_names = [name for name in zf.namelist() if name.startswith("media/")]
        assert len(media_names) == 1 and zf.read(media_names[0]) == GIF
    assert portable.verify(output)["ok"]
