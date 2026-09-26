"""Pictures in a carousel, hiding people and feeds, and arranging the Following list."""

from __future__ import annotations

import copy
from fastapi.testclient import TestClient

from threadbnc import feed, sidebar
from threadbnc.adapters.base import NCommunity
from threadbnc.db import utcnow
from threadbnc.web import create_app

from .conftest import ALICE, BOB, COMMUNITY, DOMAIN
from .test_articles import abouncer, fake_web  # noqa: F401

PICTURES = ["https://news.test/img/wall.png", "https://news.test/img/own.png"]


def one(b, sql, *args):
    with b.db.connect() as conn:
        return conn.execute(sql, args).fetchone()


def logged_in(settings, bouncer):
    client = TestClient(create_app(settings, bouncer))
    client.post("/login", data={"password": "pw"})
    return client


def follow(server, bouncer, name="math"):
    if name != "math":
        server.communities[name] = NCommunity(ap_id=f"https://{DOMAIN}/c/{name}", name=name, domain=DOMAIN,
                                               title=name.title(), local_id=name)
    cid = bouncer.follow_community(f"!{name}@{DOMAIN}", 10, 7, backfill=True)
    bouncer.poll_follow(cid)
    return cid


# --- pictures in a carousel ----------------------------------------------------------

def test_a_posts_pictures_are_a_carousel_on_its_page_and_in_its_text_opened(server, abouncer, settings):  # noqa: F811
    server.add_post("1", "Two cats", "", created=utcnow())
    server.edit_post("1", gallery=list(PICTURES))
    follow(server, abouncer)
    abouncer.media.fetch_pending()
    tid = one(abouncer, "SELECT id FROM archived_threads")["id"]
    client = logged_in(settings, abouncer)
    page = client.get(f"/t/{tid}").text
    gallery = page.split('class="post-media post-gallery"')[1].split("</article>")[0]
    assert "data-gallery" in gallery and gallery.count('class="gallery-slide"') == 2 and "1</span>/2" in gallery
    # Opened in the feed (what app.js takes from the post's page): its pictures come with it.
    assert 'class="post-media post-gallery"' in client.get(f"/t/{tid}?inline=1&refreshed=1").text
    # A picture-only post can be opened in the list view, for its pictures.
    listed = client.get("/?view=list").text
    assert f'class="act read-post" href="/t/{tid}#post-text" title="Show its 2 pictures here"' in listed
    # Pictures the text shows aren't shown again.
    server.add_post("2", "Inline", f"Look: ![a wall]({PICTURES[0]}) ![mine]({PICTURES[1]})", created=utcnow())
    abouncer.poll_follow(one(abouncer, "SELECT community_id FROM archived_threads WHERE id=?", tid)[0])
    abouncer.media.fetch_pending()
    other = one(abouncer, "SELECT t.id FROM archived_threads t JOIN objects o ON o.id=t.root_object_id "
                          "WHERE o.canonical_ap_id LIKE ?", "%/post/2")["id"]
    assert "post-gallery" not in client.get(f"/t/{other}").text


# --- hiding people and feeds -------------------------------------------------------------

def test_hiding_someone_leaves_their_posts_out_and_unsaved(server, bouncer, settings):
    server.add_post("1", "By Alice", "", created=utcnow())
    server.add_post("2", "By Bob", "", created=utcnow())
    server.edit_post("2", author=copy.deepcopy(BOB))
    cid = follow(server, bouncer)
    client = logged_in(settings, bouncer)
    page = client.get("/?view=list").text
    assert f'<input type="hidden" name="key" value="{ALICE.ap_id}">' in page
    assert 'data-confirm="Hide alice@lemmy.test? Their posts leave all your feeds' in page

    answer = client.post("/hide", data={"key": ALICE.ap_id, "kind": "author", "label": "alice@lemmy.test"},
                         headers={"X-ThreadBNC-Fetch": "1"}).json()
    assert answer["messages"][0]["undo"] == {"action": "/unhide", "fields": {"key": ALICE.ap_id}}
    with bouncer.db.connect() as conn:
        assert [i["title"] for i in feed.load_feed(conn).items] == ["By Bob"]
        assert [i["title"] for i in feed.load_feed(conn, community_id=cid).items] == ["By Bob"]
        assert [(f["unread"], f["total"]) for f in feed.followed_communities(conn)] == [(1, 1)]
    # Nothing new of hers is saved.
    server.add_post("3", "Alice again", "", created=utcnow())
    bouncer.poll_follow(cid)
    assert one(bouncer, "SELECT COUNT(*) AS n FROM objects WHERE canonical_ap_id LIKE ?", "%/post/3")["n"] == 0
    hidden_page = client.get("/hidden").text
    assert "alice@lemmy.test" in hidden_page and "a person" in hidden_page
    assert "Hidden people and feeds (1)" in client.get("/").text
    # Shown again: what's here comes back, and new posts are saved again.
    client.post("/unhide", data={"key": ALICE.ap_id})
    bouncer.poll_follow(cid)
    with bouncer.db.connect() as conn:
        assert sorted(i["title"] for i in feed.load_feed(conn).items) == ["Alice again", "By Alice", "By Bob"]


def test_hiding_a_feed_or_channel_says_it_hides_all_of_it(settings, bouncer):
    env = create_app(settings, bouncer).state.templates.env
    env.globals.update(is_youtube=lambda ap: ap.startswith("yt:"), is_rss=lambda ap: ap.startswith("rss:"),
                       chandle=lambda name, ap: name)
    macro = env.get_template("_feed.html").module.hide_button
    channel = str(macro({"c_ap": "yt:UC123", "cname": "Some Channel", "canonical_ap_id": "yt:v1", "author_ap": None}))
    assert "This hides the whole YouTube channel Some Channel, not just this post" in channel
    assert 'name="kind" value="community"' in channel and 'value="yt:UC123"' in channel and "Hide channel" in channel
    rss = str(macro({"c_ap": "rss:https://blog.test/feed", "cname": "A Blog", "canonical_ap_id": "rss:x",
                     "author_ap": "rss:https://blog.test/feed#author=Pat"}))
    assert "This hides the whole feed A Blog" in rss and "Hide feed" in rss
    person = str(macro({"c_ap": "https://bsky.app/profile/x", "cname": "x", "canonical_ap_id": "https://bsky.app/p",
                        "author_ap": "https://bsky.app/profile/did:plc:a", "username": "a.bsky.social",
                        "a_instance": "bsky.app"}))
    assert "Hide @a.bsky.social?" in person and "Hide user" in person
    assert str(macro({"c_ap": "https://x.test/c/a", "cname": "a", "canonical_ap_id": "x", "author_ap": None})).strip() == ""


# --- arranging the Following list ---------------------------------------------------------

def test_arranging_the_following_list(server, bouncer, settings):
    math, art, cats = follow(server, bouncer), follow(server, bouncer, "art"), follow(server, bouncer, "cats")
    client = logged_in(settings, bouncer)
    assert 'href="/following"' in client.get("/").text
    editor = client.get("/following").text
    assert f'name="pos_{math}"' in editor and "Reset" not in editor
    # A folder, added, then filled: cats first in it, then math; art hidden.
    client.post("/following", data={"new_folder": "Pets", "add": "1"})
    with bouncer.db.connect() as conn:
        (folder,) = [e["folder"] for e in sidebar.load(conn)["entries"] if "folder" in e]
    client.post("/following", data={f"folder_name_{folder}": "Pets & sums", f"folder_pos_{folder}": "1",
                                    f"pos_{cats}": "1", f"in_{cats}": folder, f"pos_{math}": "2", f"in_{math}": folder,
                                    f"pos_{art}": "2", f"hide_{art}": "1"})
    with bouncer.db.connect() as conn:
        layout = sidebar.load(conn)
    assert layout == {"entries": [{"folder": folder, "name": "Pets & sums", "open": True, "communities": [cats, math]}],
                      "hidden": [art]}
    page = client.get("/").text
    side = page.split('id="feed-sidebar"')[1].split("</aside>")[0]
    assert f'data-remember="/following/folders/{folder}"' in side and "Pets &amp; sums" in side
    assert side.index(f'href="/c/{cats}"') < side.index(f'href="/c/{math}"') and f'href="/c/{art}"' not in side
    assert "(1 hidden)" in side
    # Folded away, and remembered.
    client.post(f"/following/folders/{folder}", data={"collapsed": "1"})
    side = client.get("/").text.split('id="feed-sidebar"')[1]
    assert f'data-remember="/following/folders/{folder}">' in side  # not open
    assert f'data-remember="/following/folders/{folder}" open>' in client.get(f"/c/{cats}").text  # unless you're in it
    # Followed later: at the end, outside the folder.
    dogs = follow(server, bouncer, "dogs")
    side = client.get("/").text.split('id="feed-sidebar"')[1].split("</aside>")[0]
    assert side.index(f'href="/c/{math}"') < side.index(f'href="/c/{dogs}"')
    # Deleting the folder leaves its communities where it was; reset goes back to alphabetical order.
    client.post("/following", data={f"folder_delete_{folder}": "1", f"folder_pos_{folder}": "1",
                                    f"in_{cats}": folder, f"in_{math}": folder, f"pos_{dogs}": "5"})
    with bouncer.db.connect() as conn:
        assert [e.get("community") for e in sidebar.load(conn)["entries"]] == [cats, math, art, dogs]
    assert "Reset" in client.get("/following").text
    client.post("/following/reset")
    with bouncer.db.connect() as conn:
        assert sidebar.load(conn) is None
    assert COMMUNITY.name == "math"
