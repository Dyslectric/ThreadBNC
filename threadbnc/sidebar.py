"""Your own arrangement of the Following list in the feed's sidebar: folders,
the order of everything in it, and the communities left out of it.

It's edited on its own page (/following), dragged into place with JavaScript
or numbered by hand without. Until it's first saved, the list is in
alphabetical order, grouped by kind of source once it's long. Communities
followed later go at the end, outside any folder, until you place them. A
community left out of the list is still followed, and still in your feed;
it's only not listed. Kept as JSON in app_settings:

    {"entries": [{"folder": "k1", "name": "News", "open": true, "communities": [12, 15]},
                 {"community": 3}, ...],
     "hidden": [5]}
"""

from __future__ import annotations

import json
import secrets
from typing import Any

from .db import Conn

KEY = "sidebar_layout"
MAX_NAME = 60


def load(conn: Conn) -> dict[str, Any] | None:
    row = conn.execute("SELECT value FROM app_settings WHERE key=?", (KEY,)).fetchone()
    try:
        layout = json.loads(row[0]) if row and row[0] else None
    except ValueError:
        return None
    return layout if isinstance(layout, dict) and isinstance(layout.get("entries"), list) else None


def save(conn: Conn, layout: dict[str, Any] | None) -> None:
    if layout is None:
        conn.execute("DELETE FROM app_settings WHERE key=?", (KEY,))
        return
    conn.execute("INSERT INTO app_settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (KEY, json.dumps(layout)))


def arrange(follows: list[dict[str, Any]], layout: dict[str, Any] | None) -> dict[str, Any] | None:
    """The Following list as arranged: {"entries": [{"kind": "folder", "id",
    "name", "open", "follows", "unread"} | {"kind": "follow", "f"}], "hidden":
    [follow, ...]}; None while nothing's been arranged."""
    if layout is None:
        return None
    by_id = {f["id"]: f for f in follows}
    placed: set[int] = set()
    hidden = [by_id[c] for c in layout.get("hidden") or [] if c in by_id]
    placed |= {f["id"] for f in hidden}
    entries: list[dict[str, Any]] = []
    for e in layout["entries"]:
        if not isinstance(e, dict):
            continue
        if "folder" in e:
            members = [by_id[c] for c in e.get("communities") or [] if c in by_id and c not in placed]
            placed |= {f["id"] for f in members}
            entries.append({"kind": "folder", "id": str(e["folder"]), "name": e.get("name") or "Folder",
                            "open": e.get("open", True) is not False, "follows": members,
                            "unread": sum(f.get("unread") or 0 for f in members)})
        elif e.get("community") in by_id and e["community"] not in placed:
            placed.add(e["community"])
            entries.append({"kind": "follow", "f": by_id[e["community"]]})
    entries += [{"kind": "follow", "f": f} for f in follows if f["id"] not in placed]  # followed since
    return {"entries": entries, "hidden": hidden}


def current(follows: list[dict[str, Any]], layout: dict[str, Any] | None) -> dict[str, Any]:
    """What the editor starts from: the arrangement, or the list as it's shown without one."""
    return arrange(follows, layout) or {"entries": [{"kind": "follow", "f": f} for f in follows], "hidden": []}


def new_id() -> str:
    return "k" + secrets.token_hex(4)


def from_form(form: Any, follows: list[dict[str, Any]], layout: dict[str, Any] | None) -> dict[str, Any]:
    """The arrangement the editor sent. For each folder: folder_name_<id>,
    folder_pos_<id>, folder_delete_<id>; for each community: pos_<id>,
    in_<id> (its folder's id, or "" for none), hide_<id>; and new_folder, a
    folder to add at the end. Positions order everything: a folder's
    communities among themselves, the rest (and the folders) at the top level.
    A deleted folder's communities stay where it was."""
    def number(value: Any, fallback: float) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return fallback

    was = current(follows, layout)
    folders: dict[str, dict[str, Any]] = {}
    for i, e in enumerate(was["entries"]):
        if e["kind"] == "folder":
            folders[e["id"]] = {"name": e["name"], "open": e["open"], "pos": i}
    top: list[tuple[float, int, dict[str, Any]]] = []
    members: dict[str, list[tuple[float, int]]] = {}
    for fid, info in folders.items():
        name = str(form.get(f"folder_name_{fid}") or info["name"]).strip()[:MAX_NAME] or info["name"]
        pos = number(form.get(f"folder_pos_{fid}"), info["pos"])
        if form.get(f"folder_delete_{fid}"):
            info["deleted_at"] = pos
            continue
        top.append((pos, 0, {"folder": fid, "name": name, "open": info["open"], "communities": []}))
        members[fid] = []
    hidden: list[int] = []
    order = {f["id"]: i for i, f in enumerate(f for e in was["entries"] for f in
                                              ([e["f"]] if e["kind"] == "follow" else e["follows"]))}
    for f in follows:
        cid = f["id"]
        pos = number(form.get(f"pos_{cid}"), order.get(cid, len(order)))
        if form.get(f"hide_{cid}"):
            hidden.append(cid)
            continue
        folder = str(form.get(f"in_{cid}") or "")
        if folder in members:
            members[folder].append((pos, cid))
        else:
            deleted = folders.get(folder, {}).get("deleted_at")
            top.append((deleted if deleted is not None else pos, 1, {"community": cid}))
    top.sort(key=lambda t: (t[0], t[1]))
    entries = [e for _, _, e in top]
    for e in entries:
        if "folder" in e:
            e["communities"] = [cid for _, cid in sorted(members[e["folder"]])]
    name = str(form.get("new_folder") or "").strip()[:MAX_NAME]
    if name:
        entries.append({"folder": new_id(), "name": name, "open": True, "communities": []})
    return {"entries": entries, "hidden": hidden}


def set_open(conn: Conn, folder: str, is_open: bool) -> None:
    """Remember a folder in the sidebar opened or closed."""
    layout = load(conn)
    if layout is None:
        return
    for e in layout["entries"]:
        if isinstance(e, dict) and e.get("folder") == folder:
            e["open"] = is_open
    save(conn, layout)
