"""Smaller copies of archived pictures, for browsing.

The feed shows pictures at three sizes: small thumbnails in the list view,
tiles in the grid, and a column in the pictures view. Serving the archived
file for each would send phone photos and article pictures at full size, so
each gets a copy scaled down to a width for each of those (set on the Storage
page; 0 = none, the archived file is shown). The archived file stays as it is.

Copies are made from it in the background: when a picture is downloaded or
transcoded (media.py), and in a pass over what's archived when the widths
change (run_some). They're kept under <media>/thumbs/, named by the picture's
hash and the width, and served in its place when the feed asks for that width
(web.py: /media/{id}?w=). A picture no wider than a width has no copy for it,
just an empty marker saying so (".same"), so it can be served as it is and
cached for good, rather than checked back on for a copy still to come.
GIFs are left alone so they still move, and SVGs have no pixels to shrink.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from . import transcode
from .db import Conn, Database

log = logging.getLogger(__name__)

# view: (what it's called, default width in pixels)
VIEWS = {"list": ("Thumbnails in the list view", 320), "tile": ("Tiles in the grid view", 640),
         "picture": ("Pictures in the pictures view", 1280)}
KEY = "thumb_widths"            # app_settings: the widths chosen, as JSON
PASS_KEY = "thumbs_after"       # the pass over what's archived: media id it has got to
DONE_KEY = "thumbs_done_for"    # the widths the last pass finished for
DIR = "thumbs"
SCAN = 50  # pictures looked at per call of run_some
MAX_WIDTH = 4000
_EXTS = (".webp", ".jpg")
SAME = ".same"  # the marker: no copy needed at this width
_ELIGIBLE = ("status='ok' AND sha256 IS NOT NULL AND storage_path IS NOT NULL AND content_type LIKE 'image/%' "
             "AND content_type NOT IN ('image/svg+xml', 'image/gif')")


def eligible(content_type: str | None) -> bool:
    return bool(content_type) and content_type.startswith("image/") \
        and content_type not in ("image/svg+xml", "image/gif")


def saved_widths(conn: Conn) -> dict[str, int]:
    """The widths chosen on the Storage page (a view left out: its default)."""
    row = conn.execute("SELECT value FROM app_settings WHERE key=?", (KEY,)).fetchone()
    try:
        data = json.loads(row[0]) if row and row[0] else {}
    except ValueError:
        data = {}
    return {v: int(data[v]) for v in VIEWS if isinstance(data, dict) and isinstance(data.get(v), int)}


def widths(conn: Conn) -> dict[str, int]:
    """The width for each view; 0 = no copies, the archived file is shown."""
    saved = saved_widths(conn)
    return {v: saved.get(v, default) for v, (_, default) in VIEWS.items()}


def save_widths(conn: Conn, chosen: dict[str, int | None]) -> None:
    """None for a view goes back to its default. Starts a pass over what's archived."""
    value = json.dumps({v: w for v, w in chosen.items() if v in VIEWS and w is not None}, sort_keys=True)
    conn.execute("INSERT INTO app_settings(key, value) VALUES (?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (KEY, value))
    request_pass(conn)


def _signature(w: dict[str, int]) -> str:
    return ",".join(str(x) for x in sorted({x for x in w.values() if x}))


def request_pass(conn: Conn) -> None:
    conn.execute("INSERT INTO app_settings(key, value) VALUES (?, '0') "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (PASS_KEY,))


def request_pass_if_needed(conn: Conn) -> None:
    """At startup: a pass when the widths aren't the ones the last one finished for (or none has run)."""
    done = conn.execute("SELECT value FROM app_settings WHERE key=?", (DONE_KEY,)).fetchone()
    running = conn.execute("SELECT 1 FROM app_settings WHERE key=?", (PASS_KEY,)).fetchone()
    if not running and (done[0] if done else None) != _signature(widths(conn)):
        request_pass(conn)


def progress(conn: Conn) -> dict[str, int] | None:
    """How far the pass over what's archived has got; None when it isn't running."""
    row = conn.execute("SELECT value FROM app_settings WHERE key=?", (PASS_KEY,)).fetchone()
    if row is None:
        return None
    total = conn.execute(f"SELECT COUNT(*) FROM media WHERE {_ELIGIBLE}").fetchone()[0]
    done = conn.execute(f"SELECT COUNT(*) FROM media WHERE {_ELIGIBLE} AND id<=?", (int(row[0] or 0),)).fetchone()[0]
    return {"checked": done, "total": total}


def find(media_dir: Path, sha: str | None, width: int) -> Path | None:
    """The copy of a picture (by its hash) at a width, if there is one."""
    if not sha or not width:
        return None
    for ext in _EXTS:
        p = media_dir / DIR / sha[:2] / f"{sha}-{width}{ext}"
        if p.is_file():
            return p
    return None


def settled(media_dir: Path, sha: str | None, width: int) -> bool:
    """Whether a picture has what it needs at a width: a copy, or none needed."""
    return bool(sha) and (find(media_dir, sha, width) is not None
                          or (media_dir / DIR / sha[:2] / f"{sha}-{width}{SAME}").is_file())


def make(media_dir: Path, src: Path, sha: str, want: dict[str, int]) -> int:
    """Copies of one picture at each width wanted that it doesn't have yet
    (and is wider than); how many were made."""
    made = 0
    narrow = False
    for width in sorted({w for w in want.values() if w}):
        if settled(media_dir, sha, width):
            continue
        folder = media_dir / DIR / sha[:2]
        folder.mkdir(parents=True, exist_ok=True)
        if not narrow:
            try:
                got = transcode.thumbnail(src, width, media_dir)
            except (transcode.TranscodeError, OSError) as exc:
                log.warning("thumbnail of %s at %dpx: %s", sha[:12], width, exc)
                return made  # the same would go wrong at the other widths
            narrow = got is None  # no wider than this, so none wider either
        if narrow:
            (folder / f"{sha}-{width}{SAME}").touch()
            continue
        out, ctype = got
        out.replace(folder / f"{sha}-{width}{'.webp' if ctype == 'image/webp' else '.jpg'}")
        made += 1
    return made


def run_some(db: Database, media_dir: Path) -> bool:
    """A step of the pass over what's archived (after the widths changed, or
    on first start): copies for up to SCAN pictures that lack them. When it
    has been through everything, copies at widths no longer used, and of
    pictures no longer archived, are removed. True while there's more to do."""
    if not transcode.available():
        return False
    with db.connect() as conn:
        row = conn.execute("SELECT value FROM app_settings WHERE key=?", (PASS_KEY,)).fetchone()
        if row is None:
            return False
        want = widths(conn)
        rows = conn.execute(f"SELECT id, sha256, storage_path FROM media WHERE id>? AND {_ELIGIBLE} "
                            "ORDER BY id LIMIT ?", (int(row[0] or 0), SCAN)).fetchall()
    for r in rows:
        src = media_dir / r["storage_path"]
        if src.is_file():
            make(media_dir, src, r["sha256"], want)
    with db.transaction() as conn:
        if rows:
            conn.execute("UPDATE app_settings SET value=? WHERE key=?", (str(rows[-1]["id"]), PASS_KEY))
            return True
        conn.execute("DELETE FROM app_settings WHERE key=?", (PASS_KEY,))
        conn.execute("INSERT INTO app_settings(key, value) VALUES (?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (DONE_KEY, _signature(want)))
        keep = {r[0] for r in conn.execute(f"SELECT DISTINCT sha256 FROM media WHERE {_ELIGIBLE}")}
    removed = cleanup(media_dir, keep, {w for w in want.values() if w})
    if removed:
        log.info("removed %d thumbnails no longer used", removed)
    return False


def cleanup(media_dir: Path, shas: set[str], used: set[int]) -> int:
    """Remove copies of pictures not in `shas`, or at widths not `used`."""
    root = media_dir / DIR
    if not root.is_dir():
        return 0
    removed = 0
    for p in root.glob("*/*"):
        sha, _, width = p.stem.rpartition("-")
        if sha not in shas or not width.isdigit() or int(width) not in used:
            p.unlink(missing_ok=True)
            removed += 1
    return removed


def remove_for(media_dir: Path, sha: str) -> None:
    """Remove the copies of a picture that's no longer archived."""
    if len(sha) < 2:
        return
    for p in (media_dir / DIR / sha[:2]).glob(f"{sha}-*"):
        p.unlink(missing_ok=True)


def usage(media_dir: Path) -> tuple[int, int]:
    """(copies, bytes) kept."""
    root = media_dir / DIR
    files = [p for p in root.glob("*/*") if p.is_file() and p.suffix != SAME] if root.is_dir() else []
    return len(files), sum(p.stat().st_size for p in files)
