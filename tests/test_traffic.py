"""The Traffic page: what ThreadBNC asks of others, and what streams send it."""

from __future__ import annotations

from dataclasses import dataclass

import httpx
import pytest
from fastapi.testclient import TestClient

from threadbnc import traffic
from threadbnc.traffic import METER, meter_ydl, metered, report, service
from threadbnc.web import create_app

from .conftest import DOMAIN
from .test_tags import PUBLIC, RELAY, client, deliver, fedi, follow, tagged  # noqa: F401


@pytest.fixture(autouse=True)
def fresh():
    with METER._lock:
        METER._counts.clear()


def rows(b, **where):
    sql = "SELECT * FROM traffic" + (" WHERE " + " AND ".join(f"{k}=?" for k in where) if where else "")
    with b.db.connect() as conn:
        return [dict(r) for r in conn.execute(sql + " ORDER BY host, purpose", tuple(where.values()))]


def answer(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/slow":
        return httpx.Response(429, headers={"retry-after": "5"})
    if request.url.path == "/missing":
        return httpx.Response(404, content=b"no")
    return httpx.Response(200, content=b"x" * 1000)


def test_services_group_hosts():
    assert service("oauth.reddit.com") == service("i.redd.it") == service("www.reddit.com") == "Reddit"
    assert service("rr3---sn-abc.googlevideo.com") == service("i.ytimg.com") == "YouTube"
    assert service("jetstream2.us-east.bsky.network") == service("cdn.bsky.app") == "Bluesky"
    assert service("lemmy.world") == "lemmy.world"
    assert service("i.imgur.com") == "imgur.com"
    assert service("news.bbc.co.uk") == "bbc.co.uk"
    assert service("127.0.0.1") == "127.0.0.1"


def test_requests_are_counted_by_host_community_and_purpose(bouncer):
    cid = bouncer.follow_community(f"!math@{DOMAIN}", 10, 7)
    client = httpx.Client(transport=metered(httpx.MockTransport(answer)))
    with traffic.tagged("polling", cid):
        client.get(f"https://{DOMAIN}/api/v3/post/list")
        client.post(f"https://{DOMAIN}/api/v3/post", content=b"y" * 50)
    with traffic.tagged("media"):
        client.get("https://i.redd.it/slow")
        client.get("https://i.redd.it/missing")
    METER.flush(bouncer.db)

    lemmy, = rows(bouncer, host=DOMAIN)
    assert (lemmy["community_id"], lemmy["purpose"], lemmy["requests"], lemmy["errors"]) == (cid, "polling", 2, 0)
    assert lemmy["bytes_in"] > 2000 and lemmy["bytes_out"] > 50  # bodies and headers
    reddit, = rows(bouncer, host="i.redd.it")
    assert (reddit["community_id"], reddit["purpose"], reddit["requests"], reddit["errors"], reddit["slowed"]) == \
        (0, "media", 2, 2, 1)

    with traffic.tagged("polling", cid):  # later counts add to the hour's
        client.get(f"https://{DOMAIN}/api/v3/post/list")
    METER.flush(bouncer.db)
    assert rows(bouncer, host=DOMAIN)[0]["requests"] == 3


def test_a_request_that_fails_to_connect_is_an_error(bouncer):
    def down(request):
        raise httpx.ConnectError("refused")

    with pytest.raises(httpx.ConnectError):
        httpx.Client(transport=metered(httpx.MockTransport(down))).get("https://down.test/")
    METER.flush(bouncer.db)
    r, = rows(bouncer, host="down.test")
    assert (r["requests"], r["errors"], r["bytes_in"]) == (1, 1, 0)


def test_the_bouncers_clients_are_metered(bouncer):
    for client in (bouncer.http._client, bouncer.media.client, bouncer.articles.client, bouncer.reddit._http,
                   bouncer.rss_adapter.fetcher.client):
        assert isinstance(client._transport, traffic.MeteredTransport)


def test_polling_a_community_is_tagged_with_it(bouncer, monkeypatch):
    cid = bouncer.follow_community(f"!math@{DOMAIN}", 10, 7)
    seen = []
    monkeypatch.setattr(bouncer, "_poll_follow", lambda c: seen.append((traffic._purpose.get(), traffic._community.get())))
    bouncer.poll_follow(cid)
    assert seen == [("polling", cid)]
    assert (traffic._purpose.get(), traffic._community.get()) == ("", None)  # and untagged after


def test_ydl_requests_and_downloads_are_counted(bouncer):
    @dataclass
    class Resp:
        url: str
        status: int = 200
        chunks: tuple = (b"a" * 700, b"b" * 300, b"")

        def read(self, amt=None):
            chunks = list(self.chunks)
            self.chunks = tuple(chunks[1:])
            return chunks[0] if chunks else b""

    class FakeYdl:
        def urlopen(self, req):
            return Resp("https://rr1.googlevideo.com/videoplayback")

    ydl = meter_ydl(FakeYdl())
    resp = ydl.urlopen("https://www.youtube.com/watch?v=x")
    while resp.read(512):
        pass
    METER.flush(bouncer.db)
    r, = rows(bouncer)
    assert (r["host"], r["requests"], r["bytes_in"]) == ("rr1.googlevideo.com", 1, 1000)


def test_relay_deliveries_count_as_pushed_for_their_hashtag(client, tagged, fedi):
    b, _ = tagged
    cid = follow(b, client.app.state.tags)
    r = deliver(client, fedi, {"id": "https://relay.test/announce/1", "type": "Announce", "actor": RELAY,
                               "to": [PUBLIC], "object": "https://masto.test/users/alice/statuses/1"})
    assert r.status_code == 202
    METER.flush(b.db)
    pushed, = rows(b, direction="in")
    assert (pushed["host"], pushed["community_id"], pushed["requests"]) == ("relay.test", cid, 1)
    assert pushed["bytes_in"] > 100


def test_traffic_page(settings, bouncer):
    cid = bouncer.follow_community(f"!math@{DOMAIN}", 10, 7)
    client = httpx.Client(transport=metered(httpx.MockTransport(answer)))
    with traffic.tagged("polling", cid):
        client.get(f"https://{DOMAIN}/api/v3/post/list")
    with traffic.tagged("media"):
        client.get("https://i.redd.it/slow")
        client.get("https://oauth.reddit.com/r/python/new")
    traffic.record("in", "jetstream2.us-east.bsky.network", requests=5000, bytes_in=3_000_000)
    METER.flush(bouncer.db)

    r = report(bouncer.db, 1)
    assert r.out.tally.requests == 3 and r.out.tally.slowed == 1
    reddit = r.services.parts["Reddit"]
    assert reddit.tally.requests == 2 and set(reddit.parts) == {"i.redd.it", "oauth.reddit.com"}
    assert r.communities.parts[cid].community["name"] == "math"
    assert r.purposes.parts["media"].label == traffic.PURPOSES["media"]
    assert r.streams.tally.bytes_in == 3_000_000
    assert len(r.timeline) == 24 and sum(o.requests for _, o, _ in r.timeline) == 3
    assert len(report(bouncer.db, 7).timeline) == 7

    web = TestClient(create_app(settings, bouncer))
    web.post("/login", data={"password": "pw"})
    for days in (1, 7, 30, 99):
        page = web.get(f"/traffic?days={days}")
        assert page.status_code == 200
    page = web.get("/traffic")
    assert "Traffic" in page.text and "Reddit" in page.text and "i.redd.it" in page.text
    assert "math" in page.text and "Saving pictures, videos and audio" in page.text
    assert "jetstream2.us-east.bsky.network" in page.text
    assert 'href="/traffic"' in web.get("/").text  # in the menu
