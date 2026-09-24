"""The languages posts are in, and which ones feeds show.

A post's language is what its server says it is: a Lemmy or PieFed post's
language, a Mastodon post's (the key of its contentMap), a Bluesky post's
first "langs", an RSS or Atom feed's own. It's kept as a two- or three-letter
ISO 639 code on the post (objects.language), without any region ("en-GB" is
"en"); posts that don't say, or say "undetermined", have none.

Feeds show only posts in the languages chosen (app_settings "languages"),
plus every post whose language isn't known: Reddit posts, most feeds, and
posts from before languages were recorded. Choosing none shows everything.
Kept posts are always shown, as are posts opened by their address.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .db import Conn

# Names for the common ones, to show them and to let them be typed as names.
NAMES = {
    "ar": "Arabic", "bg": "Bulgarian", "ca": "Catalan", "cs": "Czech", "cy": "Welsh", "da": "Danish",
    "de": "German", "el": "Greek", "en": "English", "eo": "Esperanto", "es": "Spanish", "et": "Estonian",
    "eu": "Basque", "fa": "Persian", "fi": "Finnish", "fr": "French", "ga": "Irish", "gl": "Galician",
    "he": "Hebrew", "hi": "Hindi", "hr": "Croatian", "hu": "Hungarian", "id": "Indonesian", "is": "Icelandic",
    "it": "Italian", "ja": "Japanese", "ko": "Korean", "lt": "Lithuanian", "lv": "Latvian", "nl": "Dutch",
    "no": "Norwegian", "pl": "Polish", "pt": "Portuguese", "ro": "Romanian", "ru": "Russian", "sk": "Slovak",
    "sl": "Slovenian", "sr": "Serbian", "sv": "Swedish", "th": "Thai", "tl": "Tagalog", "tok": "Toki Pona",
    "tr": "Turkish", "uk": "Ukrainian", "vi": "Vietnamese", "zh": "Chinese",
}
_BY_NAME = {name.lower(): code for code, name in NAMES.items()}
_CODE = re.compile(r"^[a-z]{2,3}$")
# ISO 639 codes that aren't a language: undetermined, no linguistic content, several.
_NOT_ONE = {"und", "zxx", "mul", "mis"}
# Norwegian Bokmål and Nynorsk are both Norwegian; Filipino is Tagalog.
_SAME = {"nb": "no", "nn": "no", "fil": "tl"}


def normalize(value: Any) -> str | None:
    """A language code as posts are tagged with it: "en-GB" and "EN" are "en".
    None for anything that isn't one language."""
    if not isinstance(value, str):
        return None
    code = re.split(r"[-_]", value.strip().lower(), maxsplit=1)[0]
    if not _CODE.match(code) or code in _NOT_ONE:
        return None
    return _SAME.get(code, code)


def name(code: str) -> str:
    return NAMES.get(code, code)


def parse(text: str) -> tuple[list[str], list[str]]:
    """Languages typed as codes or names, separated by commas or spaces:
    (codes, what wasn't understood)."""
    codes: list[str] = []
    unknown: list[str] = []
    for word in re.split(r"[,;\s]+", text.strip()):
        if not word:
            continue
        code = _BY_NAME.get(word.lower()) or normalize(word)
        if code is None or (code not in NAMES and len(word) > 3):
            unknown.append(word)
        elif code not in codes:
            codes.append(code)
    return codes, unknown


def load(conn: Conn) -> list[str]:
    """The languages feeds show; empty: all of them."""
    row = conn.execute("SELECT value FROM app_settings WHERE key='languages'").fetchone()
    try:
        saved = json.loads(row[0]) if row and row[0] else []
    except ValueError:
        return []
    return [c for c in (normalize(v) for v in saved if isinstance(v, str)) if c] if isinstance(saved, list) else []


def save(conn: Conn, codes: list[str]) -> None:
    conn.execute("INSERT INTO app_settings(key, value) VALUES ('languages', ?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(codes),))


def shown_sql(codes: list[str], column: str) -> tuple[str, list[str]]:
    """SQL that's true for posts these languages let through, by the
    post's objects.language `column`: in one of them, or not known."""
    if not codes:
        return "1=1", []
    return f"({column} IS NULL OR {column} IN ({','.join('?' * len(codes))}))", list(codes)


def thread_shown_sql(codes: list[str], root_column: str) -> tuple[str, list[str]]:
    """The same for a thread, by its root post's id `root_column`."""
    if not codes:
        return "1=1", []
    return (f"NOT EXISTS (SELECT 1 FROM objects lo WHERE lo.id={root_column} AND lo.language IS NOT NULL "
            f"AND lo.language NOT IN ({','.join('?' * len(codes))}))", list(codes))
