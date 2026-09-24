from __future__ import annotations

from fastapi.testclient import TestClient

from threadbnc import opml
from threadbnc.web import create_app

from .test_rss import FEED, logged_in, web  # noqa: F401


def test_parse_nested_opml_deduplicates_and_rejects_non_feeds():
    data = b"""<?xml version='1.0'?>
    <opml version='2.0'><body><outline text='News'>
      <outline text='One' xmlUrl='https://one.example/feed'/>
      <outline title='Again' xmlUrl='https://one.example/feed'/>
      <outline text='Local' xmlUrl='file:///tmp/feed'/>
      <outline text='Two' url='http://two.example/rss'/>
    </outline></body></opml>"""
    assert [(f.title, f.url) for f in opml.parse(data)] == [
        ("One", "https://one.example/feed"), ("Two", "http://two.example/rss")]


def test_parse_opml_errors_are_clear():
    for data, message in ((b"<html/>", "not an OPML"), (b"<opml><body/></opml>", "No RSS"),
                          (b"<!DOCTYPE opml><opml/>", "types and entities")):
        try:
            opml.parse(data)
        except opml.OpmlError as exc:
            assert message in str(exc)
        else:
            raise AssertionError("bad OPML was accepted")


def test_export_contains_only_portable_feed_urls():
    rows = [
        {"name": "blog", "title": "A & B", "canonical_ap_id": "rss:https://example.test/feed.xml"},
        {"name": "videos", "title": "Videos", "canonical_ap_id": "rss:yt:channel:UC123"},
        {"name": "tech", "title": "Tech", "canonical_ap_id": "https://lemmy.test/c/tech"},
    ]
    data = opml.export(rows)
    subscriptions = opml.parse(data)
    assert subscriptions == [opml.Subscription("A & B", "https://example.test/feed.xml")]


def test_opml_web_import_and_export(settings, bouncer, web):
    client = logged_in(settings, bouncer)
    document = f"<opml version='2.0'><body><outline text='Blog' xmlUrl='{FEED}'/></body></opml>"
    response = client.post("/communities/opml", files={"file": ("feeds.opml", document, "text/x-opml")},
                           data={"retention_days": "90", "backfill": "1"})
    assert "Imported 1 of 1 feed" in response.text
    with bouncer.db.connect() as conn:
        follow = conn.execute("SELECT retention_days, polling FROM community_follows").fetchone()
    assert tuple(follow) == (90, 1)

    response = client.get("/communities.opml")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/x-opml")
    assert opml.parse(response.content)[0].url == FEED


def test_opml_routes_require_login(settings, bouncer):
    client = TestClient(create_app(settings, bouncer))
    assert client.get("/communities.opml", follow_redirects=False).status_code == 303


def test_opml_import_validates_settings(settings, bouncer):
    client = logged_in(settings, bouncer)
    document = "<opml version='2.0'><body><outline xmlUrl='https://example.test/feed'/></body></opml>"
    response = client.post("/communities/opml", files={"file": ("feeds.opml", document)},
                           data={"retention_days": "zero", "poll_interval_minutes": "1"})
    assert "retention of at least 1 day" in response.text
    with bouncer.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM community_follows").fetchone()[0] == 0


def test_opml_import_leaves_feeds_already_followed_as_they_are(settings, bouncer, web):
    client = logged_in(settings, bouncer)
    document = f"<opml version='2.0'><body><outline text='Blog' xmlUrl='{FEED}'/></body></opml>"
    client.post("/communities/opml", files={"file": ("feeds.opml", document)}, data={"retention_days": "90"})
    with bouncer.db.connect() as conn:
        before = tuple(conn.execute("SELECT retention_days, capture_since FROM community_follows").fetchone())
    response = client.post("/communities/opml", files={"file": ("feeds.opml", document)},
                           data={"retention_days": "7", "backfill": "1"})
    assert "Imported 0 of 1 feed" in response.text and "1 was already followed" in response.text
    with bouncer.db.connect() as conn:
        assert tuple(conn.execute("SELECT retention_days, capture_since FROM community_follows").fetchone()) == before
