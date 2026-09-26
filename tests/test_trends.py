"""Trending: links, replies and likes counted from Bluesky's and Mastodon's
streams, ranked with the archive's own links; the most posted articles read."""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from urllib.parse import quote

from threadbnc import articles, jetstream, mastodon_stream, trends
from threadbnc.db import fmt_ts
from threadbnc.jetstream import BlueskyStream
from threadbnc.web import create_app

from .conftest import DOMAIN
from .test_articles import abouncer, fake_web  # noqa: F401
from .test_bluesky import ALICE, bsky, post_view, signed_in, thread  # noqa: F401
from .test_mastodon import HOME, TOKEN, WithHome, fedi, sign_in, web  # noqa: F401
from .test_tags import client, follow, one, run_jobs, tagged  # noqa: F401

POST, LIKE = "app.bsky.feed.post", "app.bsky.feed.like"
_TID = "234567abcdefghijklmnopqrstuvwxyz"


def tid(at: datetime | None = None, clock: int = 7) -> str:
    """A Bluesky record key made at `at` (now)."""
    n = (int((at or datetime.now(timezone.utc)).timestamp() * 1_000_000) << 10) | clock
    out = ""
    for _ in range(13):
        out = _TID[n % 32] + out
        n //= 32
    return out


def at(rkey: str, did: str = ALICE) -> str:
    return f"at://{did}/{POST}/{rkey}"


def stamp(**delta: float) -> str:
    return fmt_ts(datetime.now(timezone.utc) + timedelta(**delta))


def event(collection: str, record: dict, rkey: str | None = None, did: str = "did:plc:someone",
          time_us: int | None = None) -> str:
    return json.dumps({"did": did, "time_us": time_us or time.time_ns() // 1000, "kind": "commit",
                       "commit": {"rev": "r", "operation": "create", "collection": collection,
                                  "rkey": rkey or tid(clock=int(time.time_ns()) % 1000), "record": record,
                                  "cid": "c"}})


def post(text: str = "", link: str | None = None, title: str | None = None, facet_links=(), reply_to=None,
         quote=None, tags=()) -> dict:
    record: dict = {"$type": POST, "text": text, "createdAt": stamp()}
    if tags:
        record["tags"] = list(tags)
    if link:
        record["embed"] = {"$type": "app.bsky.embed.external", "external": {"uri": link, "title": title or "",
                                                                            "description": "About it"}}
    if quote:
        record["embed"] = {"$type": "app.bsky.embed.record", "record": {"uri": quote, "cid": "q"}}
    if facet_links:
        record["facets"] = [{"index": {"byteStart": 0, "byteEnd": 1},
                             "features": [{"$type": "app.bsky.richtext.facet#link", "uri": u}]} for u in facet_links]
    if reply_to:
        record["reply"] = {"root": {"uri": reply_to, "cid": "r"}, "parent": {"uri": reply_to, "cid": "r"}}
    return record


def like(uri: str) -> dict:
    return {"$type": LIKE, "subject": {"uri": uri, "cid": "c"}, "createdAt": stamp()}


@pytest.fixture
def listening(bouncer, bsky):  # noqa: F811
    listener = BlueskyStream(bouncer, "wss://jetstream.test/subscribe", "test")
    listener.dictionary = None
    return listener


def rows(b, sql, *args):
    with b.db.connect() as conn:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]


# --- reading posts ---------------------------------------------------------------

def test_a_record_key_says_when_it_was_made():
    made = datetime(2026, 9, 25, 12, 30, 5, 123456, tzinfo=timezone.utc)
    assert trends.tid_time(tid(made)) == fmt_ts(made)
    assert trends.post_time(at(tid(made))) == fmt_ts(made)
    assert trends.tid_time("self") is None and trends.tid_time("zzzzzzzzzzzzz") is None  # not a TID
    assert trends.post_time(f"at://{ALICE}/app.bsky.feed.generator/{tid(made)}") is None


def test_only_links_to_pages_are_counted():
    assert trends.countable("https://news.test/story?utm_source=bsky") == trends.countable("https://www.news.test/story")
    for url in ("https://www.youtube.com/watch?v=abc", "https://bsky.app/profile/a/post/b",
                "https://masto.test/@alice/111", "https://masto.test/@alice", "https://news.test/",
                "https://i.imgur.com/cat.jpg", "mailto:x@y.test", None):
        assert trends.countable(url) is None, url


# --- counting from Jetstream -----------------------------------------------------------

def test_jetstream_counts_links_replies_quotes_and_likes(bouncer, listening):  # noqa: F811
    rooted = at(tid(datetime.now(timezone.utc) - timedelta(hours=1)))
    old = at(tid(datetime.now(timezone.utc) - timedelta(days=9)))
    when = time.time_ns() // 1000
    events = [
        event(POST, post("Read this #Harbours", link="https://news.test/story?utm_source=bsky", title="Harbour wall",
                         tags=["Harbours", "news"]), did="did:plc:ann", time_us=when),
        # The same page twice in one post counts once; an AMP copy of it is the same page.
        event(POST, post("Again", link="https://news.test/story", facet_links=["https://news.test/story/amp"]),
              did="did:plc:ben", time_us=when + 1),
        event(POST, post("Nice", reply_to=rooted), time_us=when + 2),
        event(POST, post("Also nice", reply_to=rooted), time_us=when + 3),
        event(POST, post("Quoting", quote=rooted), time_us=when + 4),
        event(POST, post("Too old to count", reply_to=old), time_us=when + 5),
        event(LIKE, like(rooted), time_us=when + 6),
        # One account posting it again within the hour isn't counted again.
        event(POST, post("And again #harbours", link="https://news.test/story", tags=["harbours"]), did="did:plc:ann",
              time_us=when + 7),
        event(POST, post("Me too", tags=["harbours"]), did="did:plc:ben", time_us=when + 8),
    ]
    for e in events:
        listening._take(e, set(), True)
    listening._take(events[0], set(), True)  # read again after connecting again: not counted twice
    listening.tally.flush(bouncer.db)
    key = trends.countable("https://news.test/story")
    assert rows(bouncer, "SELECT source, posts FROM link_counts WHERE key=?", key) == [
        {"source": "bluesky", "posts": 2}]
    seen = rows(bouncer, "SELECT url, title, posts FROM links_seen WHERE key=?", key)[0]
    assert seen == {"url": "https://news.test/story?utm_source=bsky", "title": "Harbour wall", "posts": 2}
    # Hashtags too, each account once an hour.
    assert rows(bouncer, "SELECT tag, posts FROM tag_counts ORDER BY tag") == [
        {"tag": "harbours", "posts": 2}, {"tag": "news", "posts": 1}]
    got = rows(bouncer, "SELECT ref, replies_seen, quotes_seen, likes_seen, created_at FROM stream_posts")
    assert [(r["ref"], r["replies_seen"], r["quotes_seen"], r["likes_seen"]) for r in got] == [(rooted, 2, 1, 1)]
    assert got[0]["created_at"] == trends.post_time(rooted)


def test_likes_are_streamed_only_when_chosen(bouncer, listening, monkeypatch):  # noqa: F811
    from .test_jetstream import FakeStream

    urls = []

    def connect(url, **kw):
        urls.append(url)
        return FakeStream([], listening)

    monkeypatch.setattr(jetstream, "connect", connect)
    listening.run_forever()  # no hashtag followed, but Bluesky is counted: it listens
    trends.save_settings(bouncer.db, bluesky_likes="stream")
    listening._stop.clear()
    listening.run_forever()
    assert urls == ["wss://jetstream.test/subscribe?wantedCollections=app.bsky.feed.post",
                    "wss://jetstream.test/subscribe?wantedCollections=app.bsky.feed.post"
                    "&wantedCollections=app.bsky.feed.like"]
    trends.save_settings(bouncer.db, bluesky=False)
    assert not listening.listening() and listening.wanted() == (set(), False, False)


# --- ranking articles ------------------------------------------------------------------

def count_links(b, source: str, *urls: str, times: int = 1, title: str | None = None) -> None:
    tally = trends.Tally(source)
    for _ in range(times):
        for url in urls:
            tally.post_links([(url, title, None)])
    tally.flush(b.db)


def test_articles_are_ranked_by_posts_everywhere(server, abouncer):  # noqa: F811
    # Two posts in the archive link the same page by two addresses, one of them a redirect.
    for local_id, url in (("1", "https://news.test/moved"), ("2", "https://news.test/story")):
        server.add_post(local_id, "Harbour", "", created=stamp(hours=-1))
        server.edit_post(local_id, url=url)
        abouncer.ingest_url(f"https://{DOMAIN}/post/{local_id}", "auto")
    abouncer.articles.fetch_pending()
    count_links(abouncer, "bluesky", "https://news.test/story?utm_source=bsky", times=3)
    count_links(abouncer, "mastodon", "https://news.test/moved")  # the redirect's address: the same page
    count_links(abouncer, "bluesky", "https://other.test/2026/09/big-news", times=5, title="Big news")
    count_links(abouncer, "mastodon", "https://other.test/2026/09/small-news")
    with abouncer.db.connect() as conn:
        ranked = trends.ranked_articles(conn, "day")
    assert [(a["title"], a["bluesky"], a["mastodon"], a["archive"], a["total"]) for a in ranked] == [
        ("Harbour wall to be rebuilt", 3, 1, 2, 6),
        ("Big news", 5, 0, 0, 5),  # not read: known by its card's title
        (None, 0, 1, 0, 1),
    ]
    assert ranked[1]["id"] is None and ranked[1]["url"] == "https://other.test/2026/09/big-news"
    # Posts on Bluesky that are in the archive too aren't counted twice when Bluesky is counted.
    with abouncer.db.connect() as conn:
        assert trends.ranked_articles(conn, "day", archive_skips=("bsky.app",))[0]["archive"] == 2


def test_the_most_posted_are_read_and_kept_while_they_trend(bouncer, abouncer, monkeypatch):  # noqa: F811
    count_links(abouncer, "bluesky", "https://news.test/teaser/2026-story", times=9)  # read, it's not an article
    count_links(abouncer, "bluesky", "https://news.test/story?utm_source=bsky", times=4)
    count_links(abouncer, "mastodon", "https://news.test/2026/09/other-story-here")
    monkeypatch.setattr(trends, "TOP_CACHED", 2)
    upkeep = trends.Trends(abouncer)
    wanted = upkeep.cache_articles()
    got = {r["url"]: r for r in rows(abouncer, "SELECT * FROM articles")}
    # The one that wasn't an article made way for the next, which was read in the next round.
    assert got.pop("https://news.test/teaser/2026-story")["status"] == "skipped"
    assert set(got) == {"https://news.test/story?utm_source=bsky", "https://news.test/2026/09/other-story-here"}
    story = got["https://news.test/story?utm_source=bsky"]
    assert story["status"] == "ok" and story["title"] == "Harbour wall to be rebuilt" and story["trending_at"]
    assert set(r["id"] for r in got.values()) <= set(wanted)
    # Read once: the link it was posted with and where it was read from are one page now.
    with abouncer.db.connect() as conn:
        ranked = trends.ranked_articles(conn, "week")
    assert [a["id"] for a in ranked] == [story["id"], got["https://news.test/2026/09/other-story-here"]["id"]]
    # Once it's no longer among the most posted, it goes like any article nothing links to.
    with abouncer.db.transaction() as conn:
        assert articles.collect_orphans(conn, stamp(hours=1)) == 0
        assert articles.collect_orphans(conn, stamp(hours=7)) == 3
    assert rows(abouncer, "SELECT id FROM articles") == []


def test_counts_are_tidied(bouncer):
    old, now = datetime.now(timezone.utc) - timedelta(days=3), datetime.now(timezone.utc)
    with bouncer.db.transaction() as conn:
        for key, posts, first in (("https://a.test/once", 1, old), ("https://a.test/often", 9, old),
                                  ("https://a.test/today", 1, now)):
            conn.execute("INSERT INTO links_seen(key, url, first_seen_at, last_seen_at, posts) VALUES (?,?,?,?,?)",
                         (key, key, fmt_ts(first), fmt_ts(first), posts))
            for h in range(3):
                conn.execute("INSERT INTO link_counts(key, hour, source, posts) VALUES (?,?,?,?)",
                             (key, (first + timedelta(hours=h)).strftime("%Y-%m-%dT%H"), "bluesky", 3))
        trends.tidy(conn)
    assert {r["key"] for r in rows(bouncer, "SELECT key FROM links_seen")} == {"https://a.test/often",
                                                                              "https://a.test/today"}
    often = rows(bouncer, "SELECT hour, posts FROM link_counts WHERE key='https://a.test/often' ORDER BY hour")
    day = old.strftime("%Y-%m-%d")
    assert sum(r["posts"] for r in often) == 9 and all(r["hour"].startswith(day) for r in often)
    assert len(often) <= 2  # added up by the day (a count at midnight is already that day's)
    assert len(rows(bouncer, "SELECT * FROM link_counts WHERE key='https://a.test/today'")) == 3


# --- ranking hashtags -------------------------------------------------------------------

def test_hashtags_used_most_are_listed(settings, bouncer):
    for source, tags in (("bluesky", ["cats"] * 5 + ["dogs"] * 2), ("mastodon", ["cats", "birds", "dogs"])):
        tally = trends.Tally(source)
        for tag in tags:
            tally.post_tags([tag])
        tally.flush(bouncer.db)
    with bouncer.db.connect() as conn:
        tags, more = trends.trending_tags(conn, "day", per_page=2)
    assert not more or [t["tag"] for t in tags] == ["cats", "dogs"]
    assert [(t["tag"], t["bluesky"], t["mastodon"], t["total"]) for t in tags] == [("cats", 5, 1, 6), ("dogs", 2, 1, 3)]
    assert more
    web_client = TestClient(create_app(settings, bouncer))
    web_client.post("/login", data={"password": "pw"})
    page = web_client.get("/trending/tags").text
    assert "#cats" in page and "6 posts" in page and "Bluesky 5" in page and "#birds" in page
    assert '<input type="hidden" name="community" value="#cats">' in page
    cid = bouncer.follow_community("#cats", None, 30, False)
    page = web_client.get("/trending/tags").text
    assert f'<a href="/c/{cid}">#cats</a>' in page and "Following" in page
    assert '<input type="hidden" name="community" value="#cats">' not in page


# --- ranking posts ---------------------------------------------------------------------

def test_bluesky_posts_talked_about_have_their_totals_read(bouncer, bsky):  # noqa: F811
    busy, quiet, gone = tid(), tid(clock=8), tid(clock=9)
    bsky.author_feed = [{"post": post_view(busy, "Big thread", likes=40, replies=12, created=stamp(hours=-1))},
                        {"post": post_view(quiet, "Well liked", likes=90, replies=1, created=stamp(hours=-2))}]
    tally = trends.Tally("bluesky")
    for _ in range(5):
        tally.reply(at(busy), trends.post_time(at(busy)))
    tally.reply(at(quiet), trends.post_time(at(quiet)))
    tally.quote(at(gone), trends.post_time(at(gone)))
    tally.flush(bouncer.db)
    upkeep = trends.Trends(bouncer)
    assert upkeep.check_bluesky() == 3
    assert bsky.requests[-1][0] == "app.bsky.feed.getPosts" and len(bsky.requests) == 1
    with bouncer.db.connect() as conn:
        liked, _ = trends.trending_posts(conn, "day", "likes")
        replied, _ = trends.trending_posts(conn, "day", "replies", "bluesky")
        assert trends.trending_posts(conn, "day", "likes", "mastodon")[0] == []
    assert [(p["view"]["text"], p["likes"], p["replies"]) for p in liked] == [("Well liked", 90, 1),
                                                                            ("Big thread", 40, 12)]
    assert [p["view"]["text"] for p in replied] == ["Big thread", "Well liked"]
    assert liked[0]["view"]["handle"] == "alice.bsky.social"
    assert liked[0]["view"]["url"] == f"https://bsky.app/profile/{ALICE}/post/{quiet}"
    assert rows(bouncer, "SELECT gone FROM stream_posts WHERE ref=?", at(gone)) == [{"gone": 1}]
    # Read again only once it's due.
    bsky.requests.clear()
    assert upkeep.check_bluesky() == 0 and bsky.requests == []


def test_the_trending_page(settings, bouncer, bsky):  # noqa: F811
    busy = tid()
    bsky.author_feed = [{"post": post_view(busy, "Big thread", likes=40, replies=12, created=stamp(hours=-1))}]
    tally = trends.Tally("bluesky")
    tally.reply(at(busy), trends.post_time(at(busy)))
    tally.post_links([("https://other.test/2026/09/big-news", "Big news", "What happened")])
    tally.flush(bouncer.db)
    trends.Trends(bouncer).check_bluesky()
    web_client = TestClient(create_app(settings, bouncer))
    web_client.post("/login", data={"password": "pw"})
    page = web_client.get("/trending").text
    assert "Big thread" in page and '♥ <span class="n">40</span>' in page and 'aria-current="page">Most liked' in page
    assert "not subscribed." in page
    # Its replies open under it, read from Bluesky (nothing saved); keeping it saves it.
    peek = f"/trending/peek?source=bluesky&ref={quote(at(busy), safe='/')}"
    assert f'data-peek="{peek}"' in page and 'class="trend-meter trend-peek' in page
    assert 'action="/archive"' in page and 'action="/trending/act"' in page
    view = bsky.author_feed[0]["post"]
    bsky.threads[view["uri"]] = thread(view, thread(post_view("re1", "Cute!", did="did:plc:bob",
                                                               handle="bob.bsky.social", reply_to=view["uri"])))
    shown = web_client.get(peek).text
    assert "data-peek-body" in shown and "Cute!" in shown and "1 reply" in shown and "<html" not in shown
    assert rows(bouncer, "SELECT id FROM archived_threads") == []
    assert web_client.get("/trending/peek", params={"source": "bluesky", "ref": "at://nobody"}).status_code == 404
    # Articles are read here, where they're listed: saved ones from the archive, others on the way.
    articles_page = web_client.get("/trending/articles?t=day").text
    assert "Big news" in articles_page and "What happened" in articles_page and "Bluesky 1" in articles_page
    reader = "/read?url=https%3A//other.test/2026/09/big-news"
    assert f'<a class="act read-article" href="{reader}"' in articles_page
    assert f'<a class="read-article" href="{reader}"' in articles_page and 'target="_blank"' not in \
        articles_page.split("pc-title")[1].split("</h2>")[0]
    # Counting Bluesky's likes from the stream, or not counting it at all.
    web_client.post("/trending/settings", data={"bluesky": "1", "bluesky_likes": "stream"})
    assert trends.settings(bouncer.db) == {"bluesky": True, "bluesky_likes": "stream"}
    web_client.post("/trending/settings", data={"bluesky_likes": "appview"})
    assert trends.settings(bouncer.db)["bluesky"] is False
    # Without a Mastodon account, its timeline can't be subscribed to.
    page = web_client.post("/trending/settings", data={"bluesky": "1", "mastodon_scope": "public"}).text
    assert "Sign in to your Mastodon account" in page and mastodon_stream.subscription(bouncer.db) is None


def test_trending_posts_pictures_are_downloaded_once_shown(settings, abouncer, bsky):  # noqa: F811
    busy = tid()
    images = {"$type": "app.bsky.embed.images#view", "images": [
        {"thumb": "https://news.test/img/own.png", "fullsize": "https://news.test/img/wall.png", "alt": "A wall"}]}
    bsky.author_feed = [{"post": post_view(busy, "Look", likes=5, embed=images, created=stamp(hours=-1))}]
    tally = trends.Tally("bluesky")
    tally.reply(at(busy), trends.post_time(at(busy)))
    tally.flush(abouncer.db)
    trends.Trends(abouncer).check_bluesky()
    web_client = TestClient(create_app(settings, abouncer))
    web_client.post("/login", data={"password": "pw"})
    key = trends.post_key("bluesky", at(busy))
    page = web_client.get("/trending").text
    assert f'id="tp-{key}"' in page and "data-pictures" in page and "1 picture" in page
    assert rows(abouncer, "SELECT id FROM media") == []  # nothing until it's shown
    # Shown on screen, it asks for them; they're downloaded, then shown in it.
    post_ = f"bluesky {at(busy)}"
    got = web_client.post("/trending/pictures", data={"post": post_}).json()
    assert got == {"ready": {key: []}, "waiting": [key]}
    abouncer.media.fetch_pending()
    got = web_client.get("/trending/pictures", params={"post": post_}).json()
    (pic,) = got["ready"][key]
    assert got["waiting"] == [] and pic["full"] == f"/media/{pic['id']}"
    page = web_client.get("/trending").text
    assert f'data-full="/media/{pic["id"]}"' in page and "data-pictures" not in page and "1 picture" not in page
    assert web_client.get(pic["full"]).content.startswith(b"\x89PNG")
    # They go with the post.
    with abouncer.db.transaction() as conn:
        trends.tidy(conn, stamp(days=9))
    upkeep = trends.Trends(abouncer)
    upkeep._checked = upkeep._cached = time.monotonic()
    upkeep.upkeep()
    assert rows(abouncer, "SELECT id FROM media") == [] and rows(abouncer, "SELECT * FROM stream_post_media") == []


def test_trending_posts_links_open_here(settings, abouncer, bsky):  # noqa: F811
    """A linked article is read, and a video played, where the post is listed."""
    story, video, old = tid(clock=1), tid(clock=2), tid(clock=3)

    def card(url, title):
        return {"$type": "app.bsky.embed.external#view", "external": {"uri": url, "title": title, "description": "",
                                                                        "thumb": "https://news.test/img/own.png"}}

    bsky.author_feed = [
        {"post": post_view(story, "Read this", likes=9, embed=card("https://news.test/story", "Harbour wall"),
                           created=stamp(hours=-1))},
        {"post": post_view(video, "Watch this", likes=8, embed=card("https://youtu.be/dQw4w9WgXcQ", "A video"),
                           created=stamp(hours=-1))},
        {"post": post_view(old, "Read before pictures were kept", likes=7,
                           embed=card("https://news.test/2026/09/other-story-here", "The other story"),
                           created=stamp(hours=-1))},
    ]
    tally = trends.Tally("bluesky")
    for rkey in (story, video, old):
        tally.reply(at(rkey), trends.post_time(at(rkey)))
    tally.flush(abouncer.db)
    trends.Trends(abouncer).check_bluesky()
    with abouncer.db.transaction() as conn:  # as a post read before picture addresses were kept
        view = json.loads(conn.execute("SELECT view_json FROM stream_posts WHERE ref=?", (at(old),)).fetchone()[0])
        del view["images"]
        conn.execute("UPDATE stream_posts SET view_json=? WHERE ref=?", (json.dumps(view), at(old)))
    web_client = TestClient(create_app(settings, abouncer))
    web_client.post("/login", data={"password": "pw"})
    page = web_client.get("/trending").text
    story_card = page.split(f'id="tp-{trends.post_key("bluesky", at(story))}"')[1].split("</article>")[0]
    assert '<a class="act read-article" href="/read?url=https%3A//news.test/story"' in story_card
    video_card = page.split(f'id="tp-{trends.post_key("bluesky", at(video))}"')[1].split("</article>")[0]
    assert '<a class="video-link" href="/youtube/v/dQw4w9WgXcQ"' in video_card and "read-article" not in video_card
    # One read before its pictures' addresses were kept asks for them all the same, and is read again for them.
    old_key = trends.post_key("bluesky", at(old))
    assert "data-pictures" in page.split(f'id="tp-{old_key}"')[1].split(">")[0]
    bsky.requests.clear()
    got = web_client.post("/trending/pictures", data={"post": f"bluesky {at(old)}"}).json()
    assert got["waiting"] == [old_key] and bsky.requests[0][0] == "app.bsky.feed.getPosts"
    abouncer.media.fetch_pending()
    assert len(web_client.get("/trending/pictures", params={"post": f"bluesky {at(old)}"}).json()["ready"][old_key]) == 1
    # The article, read where it's listed.
    r = web_client.get("/read", params={"url": "https://news.test/story", "pane": "1"})
    assert r.status_code == 200 and "Harbour wall to be rebuilt" in r.text


def test_liking_and_reposting_trending_posts(settings, bouncer, bsky):  # noqa: F811
    busy = tid()
    bsky.author_feed = [{"post": post_view(busy, "Big thread", likes=40, replies=3, created=stamp(hours=-1))}]
    tally = trends.Tally("bluesky")
    tally.reply(at(busy), trends.post_time(at(busy)))
    tally.flush(bouncer.db)
    trends.Trends(bouncer).check_bluesky()
    anonymous = TestClient(create_app(settings, bouncer))
    anonymous.post("/login", data={"password": "pw"})
    act = {"source": "bluesky", "ref": at(busy), "what": "like", "on": "1"}
    answer = anonymous.post("/trending/act", data=act, headers={"X-ThreadBNC-Fetch": "1"}).json()
    assert not answer["ok"] and "Sign in to Bluesky" in answer["messages"][0]["text"]

    client = signed_in(settings, bouncer, bsky)
    assert client.post("/trending/act", data=act, headers={"X-ThreadBNC-Fetch": "1"}).json()["ok"]
    (like,) = bsky.records.values()
    assert like["collection"] == "app.bsky.feed.like"
    assert like["record"]["subject"] == {"uri": at(busy), "cid": "c" + busy}
    client.post("/trending/act", data=act | {"what": "repost"})
    assert sorted(r["collection"] for r in bsky.records.values()) == ["app.bsky.feed.like", "app.bsky.feed.repost"]
    post_ = rows(bouncer, "SELECT likes, reposts FROM stream_posts")[0]
    assert (post_["likes"], post_["reposts"]) == (41, 1)
    page = client.get("/trending").text
    assert '<input type="hidden" name="on" value="0">' in page and 'aria-pressed="true"' in page and "41" in page
    assert 'name="action"' not in page  # it would hide the forms' own action from app.js
    # Undone.
    client.post("/trending/act", data=act | {"on": "0"})
    assert [r["collection"] for r in bsky.records.values()] == ["app.bsky.feed.repost"]
    assert rows(bouncer, "SELECT likes FROM stream_posts")[0]["likes"] == 40
    assert client.post("/trending/act", data=act | {"ref": "at://nobody"}).status_code == 404


# --- your Mastodon server's public timeline ------------------------------------------------

class TimelineStream:
    """Mastodon's streaming WebSocket: the messages given, then ThreadBNC stopping."""

    def __init__(self, messages, listener):
        self.messages, self.listener = messages, listener

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def recv(self, timeout=None):
        if self.messages:
            return self.messages.pop(0)
        self.listener.stop()
        raise TimeoutError


def update(status: dict) -> str:
    return json.dumps({"stream": ["public:local"], "event": "update", "payload": json.dumps(status)})


def masto_status(sid: str, text: str, tags=(), card=None, reply_to=None, created=None, **more) -> dict:
    return {"id": sid, "uri": f"https://{HOME}/users/carol/statuses/{sid}", "url": f"https://{HOME}/@carol/{sid}",
            "content": f"<p>{text}</p>", "visibility": "public", "spoiler_text": "", "sensitive": False,
            "created_at": created or stamp(), "in_reply_to_id": reply_to, "reblog": None,
            "account": {"id": "5", "username": "carol", "acct": "carol", "display_name": "Carol",
                        "url": f"https://{HOME}/@carol", "uri": f"https://{HOME}/users/carol"},
            "tags": [{"name": t} for t in tags], "card": card, "media_attachments": [],
            "favourites_count": 0, "replies_count": 0, "reblogs_count": 0, **more}


def test_subscribing_to_your_mastodon_servers_public_timeline(web, tagged, fedi, monkeypatch):  # noqa: F811
    b, _ = tagged
    app = web.app
    stream, relays = app.state.mastodon_stream, app.state.tags
    cid = follow(b, relays)  # #selfhosted, followed through its relay
    assert one(b, "SELECT push_state FROM community_follows WHERE community_id=?", cid)["push_state"] == "pending"
    web.get("/accounts/mastodon/callback", params={"state": sign_in(web, fedi), "code": "good"})
    web.post("/login", data={"password": "pw"})
    assert "public timeline, as @dave@home.test" in web.get("/trending").text

    web.post("/trending/settings", data={"bluesky": "1", "mastodon_scope": "public:local"})
    assert mastodon_stream.subscription(b.db) == {"scope": "public:local"} and stream.active()
    relays.housekeeping(now=True)  # (the page does this in the background)
    f = one(b, "SELECT * FROM community_follows WHERE community_id=?", cid)
    assert f["push_state"] is None and f["push_actor"] is None
    assert fedi.posted[-1][1]["type"] == "Undo"  # the relay was unfollowed
    assert "From Mastodon" in web.get(f"/c/{cid}").text

    # The stream: a post with the hashtag, a reply, and a post sharing a link.
    parent = masto_status("300", "A thread")
    messages = [update(masto_status("301", "My new rack #SelfHosted", tags=["SelfHosted"])),
                update(masto_status("302", "Reply #selfhosted", tags=["selfhosted"], reply_to="300")),
                update(masto_status("303", 'Read <a href="https://news.test/story">this</a>',
                                    card={"url": "https://news.test/story", "title": "Harbour wall",
                                          "description": "It's crumbling"})),
                json.dumps({"stream": ["public:local"], "event": "delete", "payload": "299"})]
    opened = []

    def connect(url, **kw):
        opened.append((url, kw["additional_headers"]))
        return TimelineStream(messages, stream)

    monkeypatch.setattr(mastodon_stream, "connect", connect)
    stream._streaming[HOME] = "wss://streaming.home.test"
    stream.run_forever()
    assert opened == [("wss://streaming.home.test/api/v1/streaming?stream=public%3Alocal",
                       {"Authorization": f"Bearer {TOKEN}"})]
    run_jobs(b)
    captured = rows(b, "SELECT o.canonical_ap_id, t.retention FROM archived_threads t "
                       "JOIN objects o ON o.id=t.root_object_id WHERE t.community_id=?", cid)
    assert captured == [{"canonical_ap_id": f"https://{HOME}/users/carol/statuses/301", "retention": "auto"}]
    key = trends.countable("https://news.test/story")
    assert rows(b, "SELECT source, posts FROM link_counts WHERE key=?", key) == [{"source": "mastodon", "posts": 1}]
    assert rows(b, "SELECT ref, replies_seen FROM stream_posts") == [{"ref": f"{HOME}/300", "replies_seen": 1}]

    # Its totals, and the server's own trending posts, read from your server.
    asked, boosted = [], []
    handle = fedi.home.handle

    def home(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/statuses" and request.method == "GET":
            asked.append(request.url.params.get_list("id[]"))
            return httpx.Response(200, json=[masto_status("300", "A thread", replies_count=4, favourites_count=2)])
        if request.url.path in ("/api/v1/statuses/300/reblog", "/api/v1/statuses/300/unreblog"):
            boosted.append(request.url.path.split("/", 4)[-1])
            return httpx.Response(200, json=masto_status("300", "A thread"))
        if request.url.path == "/api/v1/statuses/300/context":
            assert request.headers["authorization"] == f"Bearer {TOKEN}"
            return httpx.Response(200, json={"ancestors": [], "descendants": [
                masto_status("302", "A reply here", reply_to="300")]})
        if request.url.path == "/api/v1/trends/statuses":
            return httpx.Response(200, json=[
                masto_status("400", "Everyone liked this", favourites_count=80, replies_count=0),
                masto_status("401", "From elsewhere", favourites_count=99,
                             account={"acct": "zed@else.test", "username": "zed"})])
        return handle(request)

    fedi.home.handle = home
    b.hooks.remove(relays.housekeeping)
    stream._checked = 0.0
    b.tick()
    assert asked == [["300"]]
    with b.db.connect() as conn:
        liked, _ = trends.trending_posts(conn, "day", "likes", "mastodon")
    assert [(p["view"]["text"], p["likes"], p["replies"]) for p in liked] == [("Everyone liked this", 80, 0),
                                                                            ("A thread", 2, 4)]
    assert liked[0]["view"]["handle"] == "carol@home.test"
    assert parent  # (the thread's first post, as the server returns it)
    page = web.get("/trending?src=mastodon").text
    assert f"data-peek=\"/trending/peek?source=mastodon&ref={HOME}/300\"" in page and "Keep</span>" not in page
    shown = web.get("/trending/peek", params={"source": "mastodon", "ref": f"{HOME}/300"}).text
    assert "A reply here" in shown and "carol@home.test" in shown
    # Liked and boosted as your account, on your server.
    fedi.home.statuses["300"] = masto_status("300", "A thread")
    web.post("/trending/act", data={"source": "mastodon", "ref": f"{HOME}/300", "what": "like", "on": "1"})
    web.post("/trending/act", data={"source": "mastodon", "ref": f"{HOME}/300", "what": "repost", "on": "1"})
    assert fedi.home.favourites[-1] == "+300" and boosted == ["300/reblog"]

    # Unsubscribing follows the relays again.
    web.post("/trending/settings", data={"bluesky": "1", "mastodon_scope": ""})
    assert not stream.active()
    relays.housekeeping(now=True)
    assert one(b, "SELECT push_state FROM community_follows WHERE community_id=?", cid)["push_state"] == "pending"


def test_hashtags_can_be_followed_from_the_timeline_alone(settings, server, bouncer, monkeypatch):
    """No actor of ThreadBNC's own and no Jetstream: your server's timeline is enough."""
    assert bouncer.actor is None
    monkeypatch.setattr(bouncer.tag_adapter, "bluesky", False)
    with pytest.raises(Exception):
        bouncer.follow_community("#cats", None, 30, False)
    monkeypatch.setattr(bouncer.tag_adapter, "mastodon", lambda: True)
    assert bouncer.follow_community("#cats", None, 30, False)
    from threadbnc.adapters import CommunityRef

    described = bouncer.tag_adapter.fetch_community(CommunityRef("tag", "cats", "tag")).description
    assert "your Mastodon server's public timeline" in described
