"""Import and export RSS/Atom subscriptions as OPML 2.0.

OPML is deliberately limited to real HTTP(S) feed URLs.  Threadiverse,
Reddit, and ThreadBNC's synthetic YouTube sources are not portable RSS
subscriptions and therefore are not silently written as misleading feeds.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

from .adapters import RSS_PREFIX

MAX_BYTES = 2_000_000
MAX_FEEDS = 2_000


class OpmlError(ValueError):
    pass


@dataclass(frozen=True)
class Subscription:
    title: str
    url: str


def _feed_url(value: str) -> str | None:
    value = value.strip()
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or len(value) > 8_000:
        return None
    return value


def parse(data: bytes) -> list[Subscription]:
    """Return unique HTTP(S) subscriptions from an OPML document."""
    if len(data) > MAX_BYTES:
        raise OpmlError(f"OPML file is larger than {MAX_BYTES // 1_000_000} MB")
    if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
        raise OpmlError("OPML document types and entities are not accepted")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise OpmlError(f"Invalid OPML: {exc}") from exc
    if root.tag.rsplit("}", 1)[-1].lower() != "opml":
        raise OpmlError("This is not an OPML document")

    found: list[Subscription] = []
    seen: set[str] = set()
    for outline in root.iter():
        if outline.tag.rsplit("}", 1)[-1].lower() != "outline":
            continue
        attrs = {k.lower(): v for k, v in outline.attrib.items()}
        url = _feed_url(attrs.get("xmlurl", "") or attrs.get("url", ""))
        if not url:
            continue
        key = url.casefold()
        if key in seen:
            continue
        seen.add(key)
        title = (attrs.get("title") or attrs.get("text") or urlparse(url).hostname or url).strip()
        found.append(Subscription(title[:300], url))
        if len(found) > MAX_FEEDS:
            raise OpmlError(f"OPML contains more than {MAX_FEEDS:,} feeds")
    if not found:
        raise OpmlError("No RSS or Atom feed URLs were found")
    return found


def export(rows: Iterable[Any], title: str = "ThreadBNC subscriptions") -> bytes:
    """Build OPML for active, portable RSS/Atom follows."""
    root = ET.Element("opml", {"version": "2.0"})
    head = ET.SubElement(root, "head")
    ET.SubElement(head, "title").text = title
    ET.SubElement(head, "dateCreated").text = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")
    body = ET.SubElement(root, "body")
    seen: set[str] = set()
    for row in rows:
        ap_id = row["canonical_ap_id"]
        url = _feed_url(ap_id[len(RSS_PREFIX):]) if ap_id.startswith(RSS_PREFIX) else None
        if not url or url.casefold() in seen:
            continue
        seen.add(url.casefold())
        label = (row["title"] or row["name"] or url).strip()
        ET.SubElement(body, "outline", {"type": "rss", "text": label, "title": label, "xmlUrl": url})
    ET.indent(root, space="  ")
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def followed_rows(conn: Any) -> list[Any]:
    return conn.execute(
        "SELECT c.name, c.title, c.canonical_ap_id FROM community_follows f "
        "JOIN communities c ON c.id=f.community_id WHERE f.active=1 "
        "AND c.canonical_ap_id LIKE 'rss:http%' ORDER BY lower(c.name), c.canonical_ap_id"
    ).fetchall()
