"""The forum directory: the server's topics (which its admins arrange) and its
public feeds (which anyone there can), each a tree of communities, laid out as
ThreadBNC's Forums are. The server lists all of either in one request, which is
big and seldom changes, so it's kept for a while (Settings.forum_cache_seconds)."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from threadbnc.forums import Link, excerpt, hue, hue_key, tree_from, walk

KINDS = {"topic": "Topics", "feed": "Feeds"}


def build(data: Any, server: str, kind: str) -> dict[str, Any]:
    """Every topic (or feed) on the server as one tree."""
    root = tree_from(data, Link(server, kind, None))
    root["title"] = KINDS[kind]
    root["url"] = f"https://{server}/{kind}s"
    return root


def href(kind: str, path: list[str]) -> str:
    return f"/forums/{kind}s" + "".join("/" + quote(p, safe="") for p in path)


def _mine(node: dict[str, Any], followed: set[str]) -> int:
    """How many of the communities in a node, and below it, are subscribed to."""
    seen: set[str] = set()

    def gather(n: dict[str, Any]) -> None:
        seen.update(c["ap_id"] for c in n["communities"] if c["ap_id"] in followed)
        for child in n["children"]:
            gather(child)

    if followed:
        gather(node)
    return len(seen)


def view(api: Any, kind: str, root: dict[str, Any], parts: list[str], followed: set[str],
         part: int = 0, depth: int = 0) -> dict[str, Any] | None:
    """What a page of the directory shows of the node at `parts`: its sections
    (the forums in it that have forums in them), its other forums, and its
    communities. `part`: 2 for only its communities. None when there's no such node."""
    found = walk(root, parts)
    if found is None:
        return None
    node, trail = found

    def row(n: dict[str, Any], p: list[str]) -> dict[str, Any]:
        return {"title": n["title"], "description": excerpt(n["description"]), "href": href(kind, p),
                "icon": n["icon"] if not n["nsfw"] else None, "nsfw": n["nsfw"], "total": n["total"],
                "followed": _mine(n, followed), "unread": 0, "letter": (n["title"] or "?")[:1].upper(),
                "hue": hue(n["url"]), "own": len(n["communities"]),
                "own_names": [c["title"] or c["name"] for c in n["communities"][:6]],
                "children": [(ch["title"], href(kind, [*p, ch["name"]])) for ch in n["children"]]}

    categories, loose = [], []
    for child in node["children"] if part != 2 else []:
        p = [*parts, child["name"]]
        if child["children"]:
            categories.append({"head": row(child, p), "rows": [row(g, [*p, g["name"]]) for g in child["children"]]})
        else:
            loose.append(row(child, p))
    communities = [{**c, "icon": c["icon"] if not c["nsfw"] else None, "handle": api.handle(c["name"], c["ap_id"]),
                    "subscribed": c["ap_id"] in followed, "hue": hue(c["ap_id"]),
                    "letter": (c["title"] or c["name"] or "?")[:1].upper(), "anchor": "fc-%08x" % hue_key(c["ap_id"])}
                   for c in node["communities"]]
    head = row(node, parts)
    return {"node": node, "head": head, "title": node["title"], "href": head["href"], "categories": categories,
            "loose": loose, "communities": communities, "waiting": False,
            "src": f"{head['href']}?part={part or 1}&depth={depth}",
            "trail": [(n["title"], href(kind, p)) for n, p in trail]}
