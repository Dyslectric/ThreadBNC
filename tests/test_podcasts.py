"""Podcasts: a feed's audio enclosures become episodes that play from the post,
downloaded only when played or kept, and carry on from where you left off."""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from threadbnc import feed, media
from threadbnc.adapters import rss
from threadbnc.adapters.http import HostThrottle
from threadbnc.adapters.rss import FeedFetcher, RssAdapter, parse_feed
from threadbnc.web import create_app, running_time

FEED = "https://pod.example/feed.xml"
EPISODE = b"ID3\x04\x00\x00" + b"\x00" * 4994  # 5 KB: over the Audio limit below, within the podcast one


def item(n: int, enclosure: bool = True, duration: str = "1:02:03") -> str:
    audio = (f'<enclosure\n        url="https://cdn.pod.example/ep{n}.mp3?src=rss"\n        length="5000"\n'
             f'        type="audio/mpeg"/>') if enclosure else ""
    return f"""<item><title>Episode {n}</title><link>https://pod.example/{n}</link>
      <guid isPermaLink="false">ep-{n}</guid><pubDate>Mon, 2{n} Sep 2026 10:00:00 GMT</pubDate>
      <description><![CDATA[<p>Show notes for {n}.</p>]]></description>
      <itunes:duration>{duration}</itunes:duration>{audio}</item>"""


def podcast(items: list[str]) -> str:
    return f"""<?xml version="1.0"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">
  <channel><title>The Pod</title><link>https://pod.example/</link><description>Talk</description>
    <itunes:image href="https://pod.example/cover.jpg"/>
    {''.join(items)}</channel></rss>"""


class FakePod:
    def __init__(self) -> None:
        self.items = [item(1)]
        self.audio_requests: list[str] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.url.host == "pod.example" and path == "/feed.xml":
            return httpx.Response(200, text=podcast(self.items), headers={"content-type": "application/rss+xml"})
        if request.url.host == "pod.example" and path == "/cover.jpg":
            return httpx.Response(200, content=b"\xff\xd8\xff\xe0" + b"\x00" * 20,
                                  headers={"content-type": "image/jpeg"})
        if request.url.host == "cdn.pod.example":
            self.audio_requests.append(path)
            return httpx.Response(200, content=EPISODE, headers={"content-type": "audio/mpeg"})
        return httpx.Response(404)


@pytest.fixture
def pod(bouncer, monkeypatch):
    fake = FakePod()
    transport = httpx.MockTransport(fake.handle)
    bouncer.rss_adapter = RssAdapter(FeedFetcher("test", throttle=HostThrottle(0), check_host=False,
                                                 transport=transport))
    bouncer.media = media.MediaFetcher(bouncer.db, bouncer.media_dir, "t", 1_000, client=httpx.Client(
        transport=transport), check_host=False, throttle=HostThrottle(0), episode_max_bytes=10_000)
    monkeypatch.setattr(rss, "FEED_CACHE", 0)
    return fake


def one(b, sql, *args):
    with b.db.connect() as conn:
        return conn.execute(sql, args).fetchone()


def episode_thread(b, n: int = 1) -> int:
    return one(b, "SELECT t.id FROM archived_threads t JOIN objects o ON o.id=t.root_object_id "
                  "WHERE o.canonical_ap_id=?", f"rss:ep-{n}")[0]


def episode_media(b, n: int = 1):
    return one(b, "SELECT * FROM media WHERE url=?", f"https://cdn.pod.example/ep{n}.mp3?src=rss")


def follow(b) -> int:
    cid = b.follow_community(FEED, None, 30, backfill=True)
    b.poll_follow(cid)
    return cid


def logged_in(settings, b) -> TestClient:
    client = TestClient(create_app(settings, b))
    client.post("/login", data={"password": "pw"})
    return client


def run_jobs(b) -> None:
    while b.run_one_job():
        pass


# -- parsing ---------------------------------------------------------------------------------

def test_enclosures_are_episodes():
    feed_ = parse_feed(podcast([item(1), item(2, duration="45:10"), item(3, enclosure=False)]).encode(), FEED)
    one_, two, three = feed_.entries
    assert one_.episode == rss.Episode("https://cdn.pod.example/ep1.mp3?src=rss", 3723)
    assert two.episode.seconds == 2710
    assert three.episode is None  # a plain article in the same feed
    assert feed_.image == "https://pod.example/cover.jpg"


@pytest.mark.parametrize("value, seconds", [("3723", 3723), ("3723.6", 3723), ("62:03", 3723), ("1:02:03", 3723),
                                            ("", None), ("soon", None), ("1:2:3:4", None), ("0", None)])
def test_durations(value, seconds):
    assert rss._seconds(value) == seconds


def test_atom_and_media_rss_enclosures():
    atom = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><title>A</title>
      <entry><title>One</title><id>a1</id><link href="https://a.example/1"/>
        <link rel="enclosure" type="audio/ogg" href="https://a.example/1.ogg"/></entry>
      <entry><title>Two</title><id>a2</id><link href="https://a.example/2"/>
        <link rel="enclosure" type="video/mp4" href="https://a.example/2.mp4"/></entry></feed>"""
    first, second = parse_feed(atom, "https://a.example/feed").entries
    assert first.episode.url == "https://a.example/1.ogg" and second.episode is None  # video podcasts aren't
    mrss = b"""<?xml version="1.0"?><rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/"><channel>
      <title>M</title><item><title>x</title><guid>m1</guid>
      <media:content url="https://m.example/x.m4a" medium="audio"/></item></channel></rss>"""
    assert parse_feed(mrss, "https://m.example/").entries[0].episode.url == "https://m.example/x.m4a"


# -- following a podcast ----------------------------------------------------------------------

def test_an_episode_links_to_its_audio_and_waits_to_be_played(settings, bouncer, pod):
    cid = follow(bouncer)
    post = one(bouncer, "SELECT r.url, r.metadata_json, o.thumbnail_url FROM objects o JOIN revisions r "
                        "ON r.object_id=o.id WHERE o.canonical_ap_id='rss:ep-1'")
    assert post["url"] == "https://cdn.pod.example/ep1.mp3?src=rss"
    assert '"page": "https://pod.example/1"' in post["metadata_json"] and '"seconds": 3723' in post["metadata_json"]
    assert post["thumbnail_url"] == "https://pod.example/cover.jpg"  # the podcast's cover
    m = episode_media(bouncer)
    assert (m["episode"], m["held"], m["status"]) == (1, 1, "pending")

    bouncer.media.fetch_pending()
    client = logged_in(settings, bouncer)
    tid = episode_thread(bouncer)
    page = client.get(f"/c/{cid}").text
    assert "Play episode" in page and "1 h 02 min" in page and 'data-audio="pending"' not in page
    assert ">pod.example<" in page  # its page, not the audio host
    client.post("/feed/articles", data={"ids": [tid]})  # scrolled to
    run_jobs(bouncer)
    client.get(f"/t/{tid}")  # and opened
    bouncer.media.fetch_pending()
    run_jobs(bouncer)
    assert pod.audio_requests == []  # hosts count downloads as listens: none until it's played
    assert client.get("/feed/articles", params={"ids": [tid]}).json()["audio"] == {str(tid): "play"}


def test_play_downloads_it(settings, bouncer, pod):
    cid = follow(bouncer)
    tid = episode_thread(bouncer)
    client = logged_in(settings, bouncer)
    client.post(f"/t/{tid}/play")
    assert "Fetching the episode" in client.get(f"/c/{cid}").text
    assert client.get("/feed/articles", params={"ids": [tid]}).json()["waiting"] == [tid]
    run_jobs(bouncer)
    m = episode_media(bouncer)
    assert (m["status"], m["size_bytes"]) == ("ok", 5000)  # bigger than Audio's limit: episodes have their own
    assert pod.audio_requests == ["/ep1.mp3"]
    page = client.get(f"/c/{cid}").text
    assert f'<audio src="/media/{m["id"]}" controls preload="metadata" data-listen="{m["id"]}"' in page
    assert f'data-listen="{m["id"]}"' in client.get(f"/t/{tid}").text


def test_keeping_an_episode_saves_it(bouncer, pod):
    follow(bouncer)
    bouncer.promote(episode_thread(bouncer))
    bouncer.media.fetch_pending()
    assert episode_media(bouncer)["status"] == "ok"


def test_the_audio_limit_still_applies_to_other_audio(bouncer, pod, server):
    from .conftest import DOMAIN
    server.add_post("1", "A clip", "")
    server.edit_post("1", url="https://cdn.pod.example/clip.mp3")
    tid = bouncer.ingest_url(f"https://{DOMAIN}/post/1", "auto")
    bouncer.fetch_audio([tid])
    row = one(bouncer, "SELECT * FROM media WHERE url='https://cdn.pod.example/clip.mp3'")
    assert row["status"] == "failed" and row["error"].startswith("too large")


def test_listening_position_is_kept(settings, bouncer, pod):
    cid = follow(bouncer)
    tid = episode_thread(bouncer)
    client = logged_in(settings, bouncer)
    client.post(f"/t/{tid}/play")
    run_jobs(bouncer)
    mid = episode_media(bouncer)["id"]
    assert client.post(f"/media/{mid}/position", data={"position": "600.7", "duration": "3700"}).json()["ok"]
    page = client.get(f"/c/{cid}").text
    assert f'data-listen="{mid}" data-at="600"' in page and "52 min left" in page
    assert client.get("/feed/articles", params={"ids": [tid]}).json()["waiting"] == []

    client.post(f"/media/{mid}/position", data={"position": "3700", "duration": "Infinity", "finished": "1"})
    page = client.get(f"/c/{cid}").text
    assert "Played" in page and "data-at" not in page  # played again from the start
    assert one(bouncer, "SELECT duration FROM listening WHERE media_id=?", mid)[0] == 3700  # kept, not Infinity
    assert client.post("/media/99999/position", data={"position": "5"}).status_code == 404


def test_listening_goes_with_the_file(bouncer, pod):
    follow(bouncer)
    mid = episode_media(bouncer)["id"]
    with bouncer.db.transaction() as conn:
        feed.listened(conn, mid, 60, 3600, False, "2026-09-24T00:00:00.000000Z")
        conn.execute("DELETE FROM media_refs WHERE media_id=?", (mid,))
        media.collect_orphans(conn, bouncer.media_dir)
    assert one(bouncer, "SELECT COUNT(*) FROM listening")[0] == 0


def test_episodes_stored_before_podcasts_were_read_arent_edits(bouncer, pod, monkeypatch):
    """A feed followed before enclosures were read: its entries linked to their
    pages. Reading them as episodes now changes the link, but isn't an edit."""
    read_episode = rss._episode
    monkeypatch.setattr(rss, "_episode", lambda item, base: None)
    cid = follow(bouncer)
    assert one(bouncer, "SELECT r.url FROM revisions r JOIN objects o ON o.id=r.object_id "
                        "WHERE o.canonical_ap_id='rss:ep-1'")[0] == "https://pod.example/1"
    monkeypatch.setattr(rss, "_episode", read_episode)
    bouncer.poll_follow(cid)
    with bouncer.db.connect() as conn:
        rows = [tuple(r) for r in conn.execute("SELECT r.seq, r.url FROM revisions r JOIN objects o "
                                               "ON o.id=r.object_id WHERE o.canonical_ap_id='rss:ep-1'")]
    assert rows == [(1, "https://cdn.pod.example/ep1.mp3?src=rss")]
    assert one(bouncer, "SELECT revision_count, last_changed_at FROM objects WHERE canonical_ap_id='rss:ep-1'")[:] \
        == (1, None)
    assert episode_media(bouncer)["episode"] == 1


def test_skip_and_speed_buttons(settings, bouncer, pod):
    cid = follow(bouncer)
    tid = episode_thread(bouncer)
    client = logged_in(settings, bouncer)
    assert "data-skip" not in client.get(f"/c/{cid}").text  # nothing to skip through until it's saved
    client.post(f"/t/{tid}/play")
    run_jobs(bouncer)
    page = client.get(f"/c/{cid}").text
    assert 'data-skip="-15"' in page and 'data-skip="30"' in page and 'data-rate="1"' in page and '>1×</button>' in page

    assert client.post("/audio/rate", data={"rate": "1.5"}).json()["ok"]
    for url in (f"/c/{cid}", f"/t/{tid}"):  # every player, everywhere
        text = client.get(url).text
        assert 'data-rate="1.5"' in text and '>1.5×</button>' in text
    assert client.post("/audio/rate", data={"rate": "9"}).status_code == 400
    assert 'data-rate="1.5"' in client.get(f"/c/{cid}").text


def test_running_time():
    assert [running_time(s) for s in (40, 90, 1500, 3723, None)] == ["40 s", "2 min", "25 min", "1 h 02 min", "0 s"]
