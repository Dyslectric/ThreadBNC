"""Boorus: fedbooru servers browsed through their public API (booru.py)."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient

from threadbnc import booru
from threadbnc.web import create_app

HOST = "booru.test"
OTHER = "shrine.test"


def api_post(pid, rating="s", general="1girl outdoors", artist="artist:mika", linked=None, **extra):
    return {"id": pid, "created_at": "2026-09-30T10:00:00Z", "score": 3, "fav_count": 1, "source": "https://src.test/a",
            "rating": rating, "image_width": 800, "image_height": 600, "file_ext": "png", "file_size": 204800,
            "parent_id": None, "has_children": False, "tag_string": f"{artist} {general}",
            "tag_string_general": general, "tag_string_artist": artist, "tag_string_copyright": "",
            "tag_string_character": "", "tag_string_meta": "",
            "file_url": linked or f"https://{HOST}/media/original/ab/cd/{pid}.png",
            "preview_file_url": linked or f"https://{HOST}/media/thumb/ab/cd/{pid}.webp", **extra}


POSTS = [api_post(3, rating="e"), api_post(2, linked="https://elsewhere.test/pic.png"), api_post(1, general="cat")]
TAGS = [{"id": 1, "name": "1girl", "post_count": 702, "category": 0},
        {"id": 2, "name": "artist:mika", "post_count": 42, "category": 1},
        {"id": 3, "name": "cat", "post_count": 95, "category": 0}]


class FakeHttp:
    """Two servers: booru.test runs fedbooru, lemmy.test doesn't."""

    def __init__(self):
        self.asked = []

    def send(self, method, url, *, headers=None, content=None, throttle=True):
        where = urlparse(url)
        params = {k: v[0] for k, v in parse_qs(where.query).items()}
        self.asked.append((where.netloc, where.path, params))
        assert throttle is False, "pages are read while you wait"
        if where.path == "/.well-known/nodeinfo":
            return httpx.Response(200, json={"links": [
                {"rel": "http://nodeinfo.diaspora.software/ns/schema/2.1", "href": f"https://{where.netloc}/nodeinfo/2.1"}]})
        if where.path == "/nodeinfo/2.1":
            software = "fedbooru" if where.netloc in (HOST, OTHER) else "lemmy"
            return httpx.Response(200, json={"software": {"name": software}, "usage": {"localPosts": 1284},
                                             "metadata": {"nodeName": f"Pictures of {where.netloc}", "nsfw": True}})
        if where.path == "/posts.json":
            query = params.get("tags", "")
            if query == "rating:":
                return httpx.Response(400, text="rating: needs s, q or e")
            if query.startswith("id:"):
                return httpx.Response(200, json=[p for p in POSTS if p["id"] == int(query[3:])])
            return httpx.Response(200, json=[p for p in POSTS if not query or query in p["tag_string"].split()])
        if where.path == "/tags.json":
            like = params.get("search[name_matches]", "*").rstrip("*")
            return httpx.Response(200, json=[t for t in TAGS if t["name"].startswith(like)])
        if where.path == "/pools.json":
            return httpx.Response(200, json=[{"id": 7, "name": "A set", "post_ids": [1, 2], "post_count": 2}])
        return httpx.Response(404, text="not found")


@pytest.mark.parametrize("text, want", [
    ("booru.example.org", "booru.example.org"),
    ("https://Booru.Example.org/posts?tags=cat", "booru.example.org"),
    ("!booru@booru.example.org", "booru.example.org"),
])
def test_addresses(text, want):
    assert booru.parse_host(text) == want


@pytest.mark.parametrize("text", ["", "hello", "ftp://booru.example.org", "https://bad host/"])
def test_addresses_that_arent(text):
    with pytest.raises(booru.BooruError):
        booru.parse_host(text)


def test_tags_beside_posts():
    http = FakeHttp()
    shown = booru.posts(http, HOST)
    booru._index.clear()
    groups = booru.tag_groups(shown, booru.tag_index(http, HOST))
    assert [c for c, _ in groups] == ["artist", "general"]
    assert groups[0][1] == [{"name": "artist:mika", "category": "artist", "count": 42}]
    # the most common among the posts shown first; one the server's list doesn't have has no count
    assert [(t["name"], t["count"]) for t in groups[1][1]] == [("1girl", 702), ("outdoors", None), ("cat", 95)]


def client_for(settings, bouncer):
    bouncer.http = FakeHttp()
    booru._index.clear()
    client = TestClient(create_app(settings, bouncer))
    client.post("/login", data={"password": "pw"})
    return client


def test_add_and_browse(settings, bouncer):
    client = client_for(settings, bouncer)
    assert "Add a booru" in client.get("/booru").text
    r = client.post("/booru/servers", data={"address": f"https://{HOST}/posts"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/booru/1"
    r = client.get("/booru/1")
    page = r.text
    assert f"Pictures of {HOST}" in page and "1,284 posts" in page
    assert 'aria-current="page"' in page and ">Boorus</span>" in page  # the Boorus tab is the one lit
    # its pictures come straight from where it says they are, on these pages only
    assert f'src="https://{HOST}/media/thumb/ab/cd/1.webp"' in page and 'src="https://elsewhere.test/pic.png"' in page
    assert "img-src 'self' https:;" in r.headers["content-security-policy"]
    assert "img-src 'self';" in client.get("/forums").headers["content-security-policy"]
    assert 'data-veil="Explicit"' in page and page.count("data-veil") == 1
    assert 'href="/booru/1?tags=cat"' in page and '<span class="booru-count">95</span>' in page
    assert 'class="tag-artist"' in page
    # a search
    page = client.get("/booru/1", params={"tags": "cat"}).text
    assert 'href="/booru/1/posts/1"' in page and 'href="/booru/1/posts/3"' not in page
    assert "95 posts" in page and 'value="cat"' in page
    # one the booru can't read: it says why, and the page still comes
    page = client.get("/booru/1", params={"tags": "rating:"}).text
    assert "rating: needs s, q or e" in page and "Your boorus" in page


def test_post_tags_and_pools(settings, bouncer):
    client = client_for(settings, bouncer)
    client.post("/booru/servers", data={"address": HOST})
    page = client.get("/booru/1/posts/2").text
    assert 'src="https://elsewhere.test/pic.png"' in page and "800×600 · png" in page
    assert f'href="https://{HOST}/posts/2"' in page and "Information" in page
    assert "has no post #99" in client.get("/booru/1/posts/99").text
    page = client.get("/booru/1/tags", params={"search": "ca"}).text
    assert ">cat</a>" in page and "1girl" not in page
    assert bouncer.http.asked[-1][2]["search[name_matches]"] == "ca*"
    page = client.get("/booru/1/pools").text
    assert 'href="/booru/1?tags=pool%3A7"' in page and "A set" in page


def test_choosing_between_servers(settings, bouncer):
    client = client_for(settings, bouncer)
    r = client.post("/booru/servers", data={"address": "lemmy.test"}, follow_redirects=False)
    assert r.headers["location"] == "/booru"
    assert "runs lemmy, not fedbooru" in client.get("/booru").text
    client.post("/booru/servers", data={"address": HOST})
    client.post("/booru/servers", data={"address": OTHER})
    page = client.get("/booru/2/tags").text
    assert page.count('type="radio"') == 2 and 'value="2" checked' in page and 'value="1" checked' not in page
    assert 'name="section" value="tags"' in page
    # picking another goes to the same section of it, and it's the one opened next time
    r = client.get("/booru/go", params={"server": 1, "section": "tags"}, follow_redirects=False)
    assert r.headers["location"] == "/booru/1/tags"
    client.get("/booru/1/tags")
    assert client.get("/booru", follow_redirects=False).headers["location"] == "/booru/1"
    client.post("/booru/1/remove")
    assert client.get("/booru", follow_redirects=False).headers["location"] == "/booru/2"
    assert client.get("/booru/1", follow_redirects=False).status_code == 404
