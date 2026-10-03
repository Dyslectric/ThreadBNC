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
THIRD = "third.test"


def api_post(pid, host=HOST, rating="s", general="1girl outdoors", artist="artist:mika", linked=None, day=1, **extra):
    return {"id": pid, "created_at": f"2026-09-{day:02d}T10:00:00Z", "score": 3, "fav_count": 1,
            "source": "https://src.test/a", "md5": f"{host}-{pid}",
            "rating": rating, "image_width": 800, "image_height": 600, "file_ext": "png", "file_size": 204800,
            "parent_id": None, "has_children": False, "tag_string": f"{artist} {general}",
            "tag_string_general": general, "tag_string_artist": artist, "tag_string_copyright": "",
            "tag_string_character": "", "tag_string_meta": "",
            "file_url": linked or f"https://{host}/media/original/ab/cd/{pid}.png",
            "preview_file_url": linked or f"https://{host}/media/thumb/ab/cd/{pid}.webp", **extra}


# shrine.test has booru.test as a peer: it lists booru.test's post 1 too, as its own post 11.
POSTS = {HOST: [api_post(3, rating="e", day=3), api_post(2, linked="https://elsewhere.test/pic.png", day=2),
                api_post(1, general="cat", day=1)],
         OTHER: [api_post(12, OTHER, general="cat shrine", artist="", day=4),
                 api_post(11, OTHER, general="cat", day=1, md5=f"{HOST}-1",
                          file_url=f"https://{HOST}/media/original/ab/cd/1.png",
                          preview_file_url=f"https://{HOST}/media/thumb/ab/cd/1.webp")]}
TAGS = {HOST: [{"id": 1, "name": "1girl", "post_count": 702, "category": 0},
               {"id": 2, "name": "artist:mika", "post_count": 42, "category": 1},
               {"id": 3, "name": "cat", "post_count": 95, "category": 0}],
        OTHER: [{"id": 1, "name": "cat", "post_count": 5, "category": 0},
                {"id": 2, "name": "shrine", "post_count": 1, "category": 0}]}
PEERS = {HOST: [{"domain": OTHER, "name": "Shrine"}, THIRD, "not a host", HOST], OTHER: [HOST]}


class FakeHttp:
    """booru.test, shrine.test and third.test run fedbooru; lemmy.test doesn't; third.test is down."""

    def __init__(self):
        self.asked = []

    def send(self, method, url, *, headers=None, content=None, throttle=True):
        where = urlparse(url)
        host = where.netloc
        params = {k: v[0] for k, v in parse_qs(where.query).items()}
        self.asked.append((host, where.path, params))
        assert throttle is False, "pages are read while you wait"
        if where.path == "/.well-known/nodeinfo":
            return httpx.Response(200, json={"links": [
                {"rel": "http://nodeinfo.diaspora.software/ns/schema/2.1", "href": f"https://{host}/nodeinfo/2.1"}]})
        if where.path == "/nodeinfo/2.1":
            software = "lemmy" if host == "lemmy.test" else "fedbooru"
            return httpx.Response(200, json={"software": {"name": software}, "usage": {"localPosts": 1284},
                                             "metadata": {"nodeName": f"Pictures of {host}", "nsfw": True,
                                                          "peers": PEERS.get(host)}})
        if host == THIRD:
            return httpx.Response(502, text="down")
        # shrine.test's staff added a category, "place", which the API counts as general
        if host == OTHER and where.path == "/static/categories.css":
            return httpx.Response(200, text=":root {\n  --tag-place: #b0471e;\n  --tag-general: #2a5fb0;\n}\n"
                                            "@media (prefers-color-scheme: dark) {\n:root {\n  --tag-place: #f0956d;\n}\n}\n"
                                            ".tag-place a { color: var(--tag-place); }\n")
        if host == OTHER and where.path == "/tags":
            return httpx.Response(200, text='<tr><td class="num">1</td><td class="tag-place"><a href="/posts?tags=shrine">'
                                            'shrine</a></td><td><span class="tag-place">place</span></td></tr>')
        if where.path == "/posts.json":
            query = params.get("tags", "")
            if query == "rating:":
                return httpx.Response(400, text="rating: needs s, q or e")
            if query.startswith("id:"):
                return httpx.Response(200, json=[p for p in POSTS[host] if p["id"] == int(query[3:])])
            return httpx.Response(200, json=[p for p in POSTS[host] if not query or query in p["tag_string"].split()])
        if where.path == "/tags.json":
            like = params.get("search[name_matches]", "*").rstrip("*")
            return httpx.Response(200, json=[t for t in TAGS[host] if t["name"].startswith(like)])
        if where.path == "/pools.json":
            return httpx.Response(200, json=[{"id": 7, "name": f"A set of {host}", "post_ids": [1, 2], "post_count": 2}])
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


def test_peers_a_booru_names():
    info = booru.look_up(FakeHttp(), HOST)
    assert info["peers"] == [{"host": OTHER, "name": "Shrine"}, {"host": THIRD, "name": THIRD}]  # not itself
    mine = [{"host": HOST, "name": "Booru", "peers": info["peers"]}, {"host": OTHER, "name": "Shrine", "peers": []}]
    assert booru.suggestions(mine) == [{"host": THIRD, "name": THIRD, "via": "Booru"}]


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
    assert r.status_code == 303 and r.headers["location"] == "/booru"
    r = client.get("/booru")
    page = r.text
    assert f"Pictures of {HOST}" in page and "1,284 posts" in page
    assert "It federates with 2 other boorus you can add from the menu." in page
    assert 'aria-current="page"' in page and ">Boorus</span>" in page  # the Boorus tab is the one lit
    # its pictures come straight from where it says they are, on these pages only
    assert f'src="https://{HOST}/media/thumb/ab/cd/1.webp"' in page and 'src="https://elsewhere.test/pic.png"' in page
    assert "img-src 'self' https:;" in r.headers["content-security-policy"]
    assert "img-src 'self';" in client.get("/forums").headers["content-security-policy"]
    assert 'data-veil="Explicit"' in page and page.count("data-veil") == 1
    assert 'href="/booru?tags=cat"' in page and '<span class="booru-count">95</span>' in page
    assert 'class="tag-artist"' in page
    # a search
    page = client.get("/booru", params={"tags": "cat"}).text
    assert 'href="/booru/1/posts/1"' in page and 'href="/booru/1/posts/3"' not in page
    assert "95 posts" in page and 'value="cat"' in page
    # one the booru can't read: it says why, and the page still comes
    page = client.get("/booru", params={"tags": "rating:"}).text
    assert "rating: needs s, q or e" in page and "Your boorus" in page


def test_post_tags_and_pools(settings, bouncer):
    client = client_for(settings, bouncer)
    client.post("/booru/servers", data={"address": HOST})
    page = client.get("/booru/1/posts/2").text
    assert 'src="https://elsewhere.test/pic.png"' in page and "800×600 · png" in page
    assert f'href="https://{HOST}/posts/2"' in page and "Information" in page
    assert "has no post #99" in client.get("/booru/1/posts/99").text
    assert client.get("/booru/9/posts/1").status_code == 404
    page = client.get("/booru/tags", params={"search": "ca"}).text
    assert ">cat</a>" in page and "1girl" not in page
    assert bouncer.http.asked[-1][2]["search[name_matches]"] == "ca*"
    page = client.get("/booru/pools").text
    assert 'href="/booru?tags=pool%3A7&amp;on=1"' in page and "A set" in page


def test_peers_are_offered_not_added(settings, bouncer):
    client = client_for(settings, bouncer)
    r = client.post("/booru/servers", data={"address": "lemmy.test"}, follow_redirects=False)
    assert r.headers["location"] == "/booru"
    assert "runs lemmy, not fedbooru" in client.get("/booru").text
    client.post("/booru/servers", data={"address": HOST})
    page = client.get("/booru").text
    assert "Boorus they federate with" in page and f'name="address" value="{OTHER}"' in page and "Shrine" in page
    assert page.count('type="checkbox" name="server"') == 1, "its peers aren't browsed until they're added"
    assert not [a for a in bouncer.http.asked if a[0] in (OTHER, THIRD)], "nor asked anything"
    client.post("/booru/servers", data={"address": OTHER})
    page = client.get("/booru").text
    assert page.count('type="checkbox" name="server"') == 2 and f'name="address" value="{OTHER}"' not in page
    assert f'name="address" value="{THIRD}"' in page


def test_several_at_once(settings, bouncer):
    client = client_for(settings, bouncer)
    for host in (HOST, OTHER):
        client.post("/booru/servers", data={"address": host})
    page = client.get("/booru").text
    assert "All boorus" in page and 'value="1" checked' in page and 'value="2" checked' in page
    # newest first across both; the picture both list (shrine.test shows booru.test's) is there once
    order = [page.index(f'href="/booru/{b}/posts/{p}"') for b, p in ((2, 12), (1, 3), (1, 2), (1, 1))]
    assert order == sorted(order) and 'href="/booru/2/posts/11"' not in page
    assert f'title="Pictures of {OTHER}: cat shrine"' in page  # each says whose it is
    assert '<span class="booru-count">100</span>' in page  # 95 + 5 cats
    page = client.get("/booru/tags").text
    assert page.index(">1girl</a>") < page.index(">cat</a>") < page.index(">shrine</a>") and ">100<" in page
    page = client.get("/booru/pools").text
    assert f"A set of {HOST}" in page and f"A set of {OTHER}" in page and "<th>Booru</th>" in page
    assert 'href="/booru?tags=pool%3A7&amp;on=2"' in page
    # a pool is one booru's: only it is asked, and the ticks stay as they were
    asked = len(bouncer.http.asked)
    page = client.get("/booru", params={"tags": "cat", "on": 2}).text
    assert {a[0] for a in bouncer.http.asked[asked:]} == {OTHER}
    assert f"Only Pictures of {OTHER}" in page and "Back to All boorus" in page
    assert 'href="/booru/2/posts/11"' in page and 'value="1" checked' in page
    # unticking one
    r = client.post("/booru/select", data={"server": "2", "section": "tags"}, follow_redirects=False)
    assert r.headers["location"] == "/booru/tags"
    page = client.get("/booru").text
    assert 'value="2" checked' in page and 'value="1" checked' not in page
    assert 'href="/booru/2/posts/11"' in page and 'href="/booru/1/posts/3"' not in page
    # one added now is ticked too; ticking none is ticking all
    client.post("/booru/select", data={"section": "posts"})
    assert client.get("/booru").text.count(" checked") == 2
    client.post("/booru/2/remove")
    page = client.get("/booru").text
    assert f"Pictures of {OTHER}" not in page.split("Boorus they federate with")[0]
    assert f'name="address" value="{OTHER}"' in page  # offered again


def test_one_that_doesnt_answer(settings, bouncer):
    client = client_for(settings, bouncer)
    for host in (HOST, THIRD):
        client.post("/booru/servers", data={"address": host})
    page = client.get("/booru").text
    assert f"Pictures of {THIRD}: {THIRD} answered HTTP 502." in page
    assert 'href="/booru/1/posts/3"' in page, "the others' posts are still shown"


def test_categories_a_booru_added(settings, bouncer):
    client = client_for(settings, bouncer)
    for host in (HOST, OTHER):
        client.post("/booru/servers", data={"address": host})
    page = client.get("/booru").text
    side = page.split('<aside aria-label="Tags">', 1)[1].split("</aside>", 1)[0]
    assert side.index("<h2>Place</h2>") < side.index("<h2>General</h2>") < side.index("<h2>Artist</h2>")
    assert '<li class="tag-place"><a href="/booru?tags=shrine">shrine</a>' in side
    assert '<link rel="stylesheet" href="/booru/categories.css">' in page
    r = client.get("/booru/categories.css")
    assert r.headers["content-type"].startswith("text/css")
    assert ".tag-place > a { color: light-dark(#b0471e, #f0956d); }" in r.text and "tag-general" not in r.text
    assert '<td class="tag-place"><a href="/booru?tags=shrine">shrine</a>' in client.get("/booru/tags").text
    assert "<h2>Place</h2>" in client.get("/booru/2/posts/12").text
