"""Stories: Trending's articles and hashtags grouped by what's posted together.

One piece of news shows in Trending as several articles, from different sites,
and a few hashtags. The streams count which links and hashtags each post has
together (trends.Tally.pair), by the day, and a story is those posted together
far more than chance would have it:

- The articles and hashtags at the top of a Trending ranking (Rising, the past
  day or week; on one network or both; in your languages) are taken in the
  order they're ranked, the busiest of each kind interleaved.
- Each joins the story it's posted with most, when that's at least STORY_LINK
  of what both are posted (the posts with both over the square root of the
  product of their posts, a story's posts being all its members'), and in at
  least STORY_MIN_POSTS posts. Otherwise it starts a story of its own. So a
  hashtag posted with everything (#news) joins nothing: what it shares with
  any one story is a small part of its posts.
- Stories come in the order of their first, highest ranked, member.

Nothing is read from the pages to do this, and the pairs go after
trends.PAIRS_KEPT days: stories are only for now, not for History.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone
from typing import Any

from . import trends
from .db import Conn, parse_ts, utcnow

WINDOWS = {"rising": 1, "day": 1, "week": 7}  # the rankings stories are made of: days before today counted together
STORY_LINK = 0.15
STORY_MIN_POSTS = 3
TAKEN = 60  # of each ranking (articles, hashtags): those looked at


def _measure(item: dict[str, Any], kind: str, source: str | None, rising: bool) -> float:
    """How high an item is ranked, comparably across articles and hashtags."""
    if rising:
        return float(item.get("score") or 0)
    if kind == "tag":
        return float(item.get(source) if source in trends.SOURCES else item.get("total") or 0)
    sources = (source,) if source in trends.ARTICLE_SOURCES else tuple(trends.ARTICLE_SOURCES)
    return float(sum(item.get(s, 0) for s in sources))


def stories(conn: Conn, articles: list[dict[str, Any]], tags: list[dict[str, Any]], window: str,
            source: str | None = None, now: str | None = None) -> list[dict[str, Any]]:
    """Stories of the ranked `articles` (trends.order_articles) and `tags`
    (trends.trending_tags) of a ranking `window` (in WINDOWS), on `source`
    or both: [{"articles": [...], "tags": [...], "lead": "article" | "tag",
    "measure": the first member's, "linked": posts with two of its members}],
    the first member first in each."""
    rising = window == "rising"
    members = [("article", a, list(a["keys"])) for a in articles[:TAKEN]] + \
        [("tag", t, ["#" + t["tag"]]) for t in tags[:TAKEN]]
    measures = [_measure(item, kind, source, rising) for kind, item, _names in members]
    order = sorted(range(len(members)), key=lambda i: (-measures[i], members[i][0] != "article", i))
    moment = parse_ts(now or utcnow()) or datetime.now(timezone.utc)
    since = (moment.date() - timedelta(days=WINDOWS.get(window, 1)))
    posts, together = _counts(conn, [n for _kind, _item, names in members for n in names], since)
    of = {n: i for i, (_kind, _item, names) in enumerate(members) for n in names}
    linked: dict[tuple[int, int], int] = {}
    for (a, b), n in together.items():
        i, j = of.get(a), of.get(b)
        if i is not None and j is not None and i != j:
            linked[(min(i, j), max(i, j))] = linked.get((min(i, j), max(i, j)), 0) + n
    size = [sum(posts.get(n, 0) for n in names) for _kind, _item, names in members]

    groups: list[dict[str, Any]] = []
    for i in order:
        best, strength, shared = None, 0.0, 0
        for g in groups:
            both = sum(linked.get((min(i, j), max(i, j)), 0) for j in g["members"])
            if both < STORY_MIN_POSTS or not size[i] or not g["posts"]:
                continue
            link = both / math.sqrt(size[i] * g["posts"])
            if link > strength:
                best, strength, shared = g, link, both
        if best is not None and strength >= STORY_LINK:
            best["members"].append(i)
            best["posts"] += size[i]
            best["linked"] += shared
        else:
            groups.append({"members": [i], "posts": size[i], "linked": 0})
    out = []
    for g in groups:
        first = g["members"][0]
        out.append({"lead": members[first][0], "measure": measures[first], "linked": g["linked"],
                    "articles": [members[i][1] for i in g["members"] if members[i][0] == "article"],
                    "tags": [members[i][1] for i in g["members"] if members[i][0] == "tag"]})
    return out


def _counts(conn: Conn, names: list[str], since: date) -> tuple[dict[str, int], dict[tuple[str, str], int]]:
    """Since the start of the day `since`: how many posts each of `names`
    ("#" + hashtag, or a link's key) was in, and how many had two of them together."""
    names = sorted(set(names))
    hour, day = f"{since.isoformat()}T00", since.isoformat()
    posts: dict[str, int] = {}
    tags = [n[1:] for n in names if n.startswith("#")]
    keys = [n for n in names if not n.startswith("#")]
    for chunk in trends._chunks(tags):
        posts.update(("#" + r["tag"], int(r["posts"] or 0)) for r in conn.execute(
            f"SELECT tag, SUM(posts) AS posts FROM tag_counts WHERE hour >= ? AND tag IN ({trends._marks(chunk)}) "
            "GROUP BY tag", (hour, *chunk)))
    for chunk in trends._chunks(keys):
        posts.update((r["key"], int(r["posts"] or 0)) for r in conn.execute(
            f"SELECT key, SUM(posts) AS posts FROM link_counts WHERE hour >= ? AND key IN ({trends._marks(chunk)}) "
            "GROUP BY key", (hour, *chunk)))
    together: dict[tuple[str, str], int] = {}
    wanted = set(names)
    for chunk in trends._chunks(names, 400):
        for r in conn.execute(
                f"SELECT a, b, SUM(posts) AS posts FROM pair_counts WHERE day >= ? AND a IN ({trends._marks(chunk)}) "
                f"AND b IN ({trends._marks(names)}) GROUP BY a, b", (day, *chunk, *names)):
            if r["b"] in wanted:
                together[(r["a"], r["b"])] = int(r["posts"] or 0)
    return posts, together
