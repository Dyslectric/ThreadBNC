from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from threadbnc import search as search_mod
from threadbnc.db import Database
from threadbnc.search import Filters, SearchError, parse
from threadbnc.web import create_app

from .conftest import DOMAIN, TEST_PG_URL


@pytest.fixture
def archive(server, bouncer):
    """Two threads: one about backups (with comments), one about cooking."""
    server.add_post("1", "Backups for a homelab", "How do you back up **Docker** volumes?")
    server.add_comment("1", "10", "I use restic with rclone, nightly.")
    server.add_comment("1", "11", "Borg, then an rsync to a friend's NAS.", parent="10")
    server.add_post("2", "Sourdough starter", "Mine smells of acetone, is that normal?")
    server.add_comment("2", "20", "Feed it more often; acetone means it's hungry.")
    return {"backups": bouncer.ingest_url(f"https://{DOMAIN}/post/1"),
            "bread": bouncer.ingest_url(f"https://{DOMAIN}/post/2")}


def find(bouncer, text, **filters):
    with bouncer.db.connect() as conn:
        return search_mod.search(conn, bouncer.db.search_backend, text, Filters(**filters))


def bodies(res):
    return [h["title"] if h["object_type"] == "post" else h["body"] for h in res.hits]


def test_uses_the_full_text_index(bouncer):
    assert bouncer.db.search_backend == ("postgres" if TEST_PG_URL else "fts5")


def test_finds_posts_and_comments_by_their_words(bouncer, archive):
    res = find(bouncer, "restic")
    assert res.total == 1 and res.hits[0]["object_type"] == "comment"
    assert res.hits[0]["thread_id"] == archive["backups"] and res.hits[0]["thread_title"] == "Backups for a homelab"
    assert "<mark>restic</mark>" in res.hits[0]["snippet"]
    post = find(bouncer, "docker").hits[0]  # case doesn't matter; Markdown is flattened for the snippet
    assert post["object_type"] == "post" and "<mark>Docker</mark> volumes" in post["snippet"]
    assert find(bouncer, "homelab").hits[0]["title_html"] == "Backups for a <mark>homelab</mark>"


def test_query_syntax(bouncer, archive):
    assert find(bouncer, "acetone hungry").total == 1  # every word
    assert find(bouncer, '"nightly restic"').total == 0 and find(bouncer, '"restic with rclone"').total == 1
    assert find(bouncer, "sourd*").total == 1  # prefix
    assert sorted(bodies(find(bouncer, "borg OR restic"))) == sorted(
        ["I use restic with rclone, nightly.", "Borg, then an rsync to a friend's NAS."])
    assert find(bouncer, "acetone -hungry").total == 1  # the post, not the comment
    assert find(bouncer, "acetone -hungry").hits[0]["object_type"] == "post"


def test_odd_input_is_harmless(bouncer, archive):
    for text in ['"', "NOT", "AND OR", "a*b", "c++", "restic)", "'; DROP TABLE objects; --", "col:restic", "***"]:
        if parse(text).groups:
            find(bouncer, text)  # no syntax errors from the index
    with pytest.raises(SearchError):
        find(bouncer, "  ")
    with pytest.raises(SearchError):
        find(bouncer, "-restic")
    assert find(bouncer, "restic").total == 1


def test_filters(bouncer, archive, server):
    assert bodies(find(bouncer, "acetone", kind="comment")) == ["Feed it more often; acetone means it's hungry."]
    assert find(bouncer, "acetone", kind="post").total == 1
    assert find(bouncer, "acetone", author="bob@other.test").total == 1
    assert find(bouncer, "acetone", author="alice").total == 1
    assert find(bouncer, "acetone", author="u/nobody").total == 0
    with bouncer.db.connect() as conn:
        bread_c = conn.execute("SELECT community_id FROM archived_threads WHERE id=?", (archive["bread"],)).fetchone()[0]
    assert find(bouncer, "acetone", community_id=bread_c).total == 2
    assert find(bouncer, "acetone", community_id=bread_c + 999).total == 0
    assert find(bouncer, "acetone", kept_only=True).total == 2  # ingested by link: kept


def test_finds_text_that_was_edited_away_or_deleted(bouncer, archive, server):
    server.edit_comment("1", "10", body="I switched to kopia.", updated_at="2026-09-02T00:00:00.000000Z")
    server.edit_comment("1", "11", deleted=True, body="")
    bouncer.sync_thread(archive["backups"], force=True)
    old = find(bouncer, "restic")
    assert old.total == 1 and not old.hits[0]["matched_current"]
    assert old.hits[0]["body"] == "I switched to kopia."  # shows what it says now
    assert "<mark>restic</mark>" in old.hits[0]["earlier"]  # and the version that matched
    assert find(bouncer, "kopia").hits[0]["matched_current"]
    gone = find(bouncer, "borg", only="gone")
    assert gone.total == 1 and gone.hits[0]["cur_deleted"]
    assert find(bouncer, "restic", only="edited").total == 1 and find(bouncer, "acetone", only="edited").total == 0


def test_trash_and_purged_threads_are_left_out(bouncer, archive):
    bouncer.move_to_trash(archive["bread"])
    assert find(bouncer, "acetone").total == 0
    bouncer.restore_from_trash(archive["bread"])
    assert find(bouncer, "acetone").total == 2
    bouncer.move_to_trash(archive["bread"])
    bouncer.delete_from_trash([archive["bread"]])
    assert find(bouncer, "acetone").total == 0 and find(bouncer, "restic").total == 1


def test_sorting_and_paging(bouncer, server):
    for i in range(30):
        server.add_post(str(100 + i), f"Router {i}", "router firmware", created=f"2026-08-{1 + i % 28:02d}T00:00:00.000000Z")
        bouncer.ingest_url(f"https://{DOMAIN}/post/{100 + i}")
    first = find(bouncer, "router", sort="new")
    assert first.total == 30 and first.pages == 2 and len(first.hits) == search_mod.PAGE_SIZE
    dates = [h["created_at"] for h in first.hits]
    assert dates == sorted(dates, reverse=True)
    with bouncer.db.connect() as conn:
        second = search_mod.search(conn, bouncer.db.search_backend, "router", Filters(sort="new"), page=2)
    assert len(second.hits) == 5 and not {h["id"] for h in second.hits} & {h["id"] for h in first.hits}
    oldest = find(bouncer, "router", sort="old").hits[0]["created_at"]
    assert oldest == min(dates + [h["created_at"] for h in second.hits])


def test_titles_rank_above_bodies(bouncer, server):
    server.add_post("50", "Nothing here", "a long text that mentions zigbee once among many other words")
    server.add_post("51", "Zigbee coordinators", "which one?")
    for pid in ("50", "51"):
        bouncer.ingest_url(f"https://{DOMAIN}/post/{pid}")
    assert find(bouncer, "zigbee").hits[0]["title"] == "Zigbee coordinators"


@pytest.mark.skipif(bool(TEST_PG_URL), reason="SQLite's index")
def test_index_is_built_for_an_existing_archive(settings, bouncer, archive):
    with bouncer.db.connect() as conn:  # an archive from before search existed
        conn.executescript("DROP TRIGGER revisions_fts_insert; DROP TRIGGER revisions_fts_delete; "
                           "DROP TRIGGER revisions_fts_update; DROP TABLE revisions_fts;")
    reopened = Database(settings.db_path)
    with reopened.connect() as conn:
        res = search_mod.search(conn, reopened.search_backend, "restic")
    assert reopened.search_backend == "fts5" and res.total == 1


@pytest.mark.skipif(bool(TEST_PG_URL), reason="SQLite only")
def test_substring_fallback_without_fts5(bouncer, archive):
    with bouncer.db.connect() as conn:
        assert search_mod.search(conn, "like", "restic").total == 1
        assert search_mod.search(conn, "like", "acetone -hungry").total == 1
        assert search_mod.search(conn, "like", "borg OR restic").total == 2
        assert search_mod.search(conn, "like", "100%").total == 0


def test_parse():
    q = parse('docker "self hosted" nginx OR caddy back* -windows')
    assert [[(t.text, t.kind) for t in g] for g in q.groups] == [
        [("docker", "word")], [("self hosted", "phrase")], [("nginx", "word"), ("caddy", "word")],
        [("back", "prefix")]]
    assert [(t.text, t.kind) for t in q.exclude] == [("windows", "word")]
    assert search_mod.fts5_match(q) == '("docker" AND "self hosted" AND ("nginx" OR "caddy") AND "back"*) NOT "windows"'
    assert search_mod.fts5_match(parse('say "hi"" there')) == '"say" AND "hi" AND "there"'  # unclosed quote


def test_snippet_centres_on_the_match():
    text = ("word " * 100) + "needle " + ("word " * 100)
    snip = search_mod.snippet(text, search_mod.highlighter(parse("needle")))
    assert snip.startswith("…") and snip.endswith("…") and "<mark>needle</mark>" in snip
    assert "&lt;b&gt;" in search_mod.snippet("<b>needle</b>", search_mod.highlighter(parse("needle")))


def client_for(settings, bouncer):
    client = TestClient(create_app(settings, bouncer))
    client.post("/login", data={"password": "pw"})
    return client


def test_search_page(settings, bouncer, archive):
    client = TestClient(create_app(settings, bouncer))
    assert client.get("/search?q=restic", follow_redirects=False).status_code == 303  # private
    client.post("/login", data={"password": "pw"})
    assert 'action="/search"' in client.get("/").text  # the box in the header
    page = client.get("/search?q=restic").text
    assert "1 result" in page and f'href="/t/{archive["backups"]}#o' in page and "<mark>restic</mark>" in page
    assert "not only ones to leave out" in client.get("/search?q=-restic").text
    assert "Type at least one word" in client.get("/search?q=%22%22").text
    assert "How to search" in client.get("/search").text
    assert "Nothing in the archive matches" in client.get("/search?q=zzzz").text


def test_search_api(settings, bouncer, archive):
    client = TestClient(create_app(settings, bouncer))
    r = client.get("/api/search", params={"q": "acetone", "what": "comment"}, headers={"Authorization": "Bearer tok"})
    data = r.json()
    assert r.status_code == 200 and data["total"] == 1
    hit = data["results"][0]
    assert hit["type"] == "comment" and hit["path"].startswith(f"/t/{archive['bread']}#o")
    assert hit["title"] == "Sourdough starter" and hit["author"] == "bob@other.test" and hit["kept"]
    assert client.get("/api/search", params={"q": ""}, headers={"Authorization": "Bearer tok"}).status_code == 400
    assert client.get("/api/search", params={"q": "x"}).status_code == 401


def test_new_revisions_are_indexed_as_they_arrive(bouncer, archive, server):
    assert find(bouncer, "pumpernickel").total == 0
    server.add_comment("2", "21", "Try pumpernickel next.")
    bouncer.sync_thread(archive["bread"], force=True)
    assert find(bouncer, "pumpernickel").total == 1
