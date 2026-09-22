"""Searching the archive.

Every stored version of every post and comment is searchable, so text that
was later edited away, or deleted or removed, can still be found. Results show
what the post or comment says now, and say when only an earlier version
matched.

SQLite uses an FTS5 index over `revisions`, kept in step by triggers; Postgres
a GIN index on a tsvector of the same columns. Both match whole words, without
stemming, since the archive holds many languages. Where SQLite was built
without FTS5, a slower substring search stands in.

Query syntax, the same on both: words (all must match), "exact phrases",
word* (prefix), OR between two terms, and -word to exclude.
"""

from __future__ import annotations

import difflib
import html
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from markupsafe import Markup

PAGE_SIZE = 25
MAX_TERMS = 12
SORTS = {"relevance": "Best match", "new": "Newest", "old": "Oldest"}
KINDS = {"all": "Posts and comments", "post": "Posts", "comment": "Comments"}
ONLY = {"": "Any state", "gone": "Deleted or removed", "edited": "Edited"}
# Postgres: title counts most, then body, then link. Bodies are capped so one
# enormous article can't exceed tsvector's 1 MB limit and fail its insert.
PG_VECTOR = ("(setweight(to_tsvector('simple', coalesce(title, '')), 'A') || "
             "setweight(to_tsvector('simple', left(coalesce(body, ''), 200000)), 'B') || "
             "setweight(to_tsvector('simple', coalesce(url, '')), 'C'))")
FTS5_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS revisions_fts USING fts5(
    title, body, url, content='revisions', content_rowid='id', tokenize='unicode61 remove_diacritics 2');
CREATE TRIGGER IF NOT EXISTS revisions_fts_insert AFTER INSERT ON revisions BEGIN
    INSERT INTO revisions_fts(rowid, title, body, url) VALUES (new.id, new.title, new.body, new.url);
END;
CREATE TRIGGER IF NOT EXISTS revisions_fts_delete AFTER DELETE ON revisions BEGIN
    INSERT INTO revisions_fts(revisions_fts, rowid, title, body, url)
    VALUES ('delete', old.id, old.title, old.body, old.url);
END;
CREATE TRIGGER IF NOT EXISTS revisions_fts_update AFTER UPDATE ON revisions BEGIN
    INSERT INTO revisions_fts(revisions_fts, rowid, title, body, url)
    VALUES ('delete', old.id, old.title, old.body, old.url);
    INSERT INTO revisions_fts(rowid, title, body, url) VALUES (new.id, new.title, new.body, new.url);
END;
"""


class SearchError(ValueError):
    """Shown to the user as-is."""


def install(conn: Any, is_postgres: bool) -> str:
    """Create the search index if it's missing (building it from what's
    already stored). Returns the backend in use: postgres, fts5 or like."""
    if is_postgres:
        conn.execute(f"CREATE INDEX IF NOT EXISTS revisions_search ON revisions USING GIN ({PG_VECTOR})")
        return "postgres"
    existed = conn.execute("SELECT 1 FROM sqlite_master WHERE name='revisions_fts'").fetchone()
    try:
        conn.executescript(FTS5_SCHEMA)
    except sqlite3.OperationalError:  # SQLite built without FTS5
        return "like"
    if not existed:
        conn.execute("INSERT INTO revisions_fts(revisions_fts) VALUES ('rebuild')")
    return "fts5"


# -- the query ----------------------------------------------------------------
@dataclass
class Term:
    text: str
    kind: str  # word | phrase | prefix

    @property
    def words(self) -> list[str]:
        return re.findall(r"\w+", self.text)


@dataclass
class Query:
    groups: list[list[Term]] = field(default_factory=list)  # every group must match; any term in a group
    exclude: list[Term] = field(default_factory=list)

    @property
    def positive(self) -> list[Term]:
        return [t for g in self.groups for t in g]


_TOKEN = re.compile(r'-?"[^"]*"?|\S+')


def parse(text: str) -> Query:
    """Words, "phrases", prefix*, a OR b, -excluded."""
    q = Query()
    join_next = False
    for raw in _TOKEN.findall(text or "")[: MAX_TERMS * 2]:
        if raw == "OR":
            join_next = bool(q.groups)
            continue
        negative = raw.startswith("-") and len(raw) > 1
        body = raw[1:] if negative else raw
        if body.startswith('"'):
            term = Term(body.strip('"').strip(), "phrase")
        elif body.endswith("*") and len(body) > 1:
            term = Term(body.rstrip("*"), "prefix")
        else:
            term = Term(body, "word")
        if not term.words:
            join_next = False
            continue
        if negative:
            q.exclude.append(term)
        elif join_next:
            q.groups[-1].append(term)
        else:
            q.groups.append([term])
        join_next = False
    q.groups = q.groups[:MAX_TERMS]
    return q


def _fts5_term(t: Term) -> str:
    text = " ".join(t.words) if t.kind == "prefix" else t.text
    quoted = '"' + text.replace('"', '""') + '"'
    return quoted + "*" if t.kind == "prefix" else quoted


def fts5_match(q: Query) -> str:
    parts = []
    for g in q.groups:
        alts = [_fts5_term(t) for t in g]
        parts.append(alts[0] if len(alts) == 1 else "(" + " OR ".join(alts) + ")")
    expr = " AND ".join(parts)
    for t in q.exclude:
        expr = f"({expr}) NOT {_fts5_term(t)}"
    return expr


def _pg_term(t: Term) -> tuple[str, list[str]]:
    if t.kind == "phrase":
        return "phraseto_tsquery('simple', ?)", [t.text]
    if t.kind == "prefix":  # only word characters reach to_tsquery, so its syntax can't be injected
        words = [w.lower() for w in t.words]
        return "to_tsquery('simple', ?)", [" & ".join(words[:-1] + [words[-1] + ":*"])]
    return "plainto_tsquery('simple', ?)", [t.text]


def pg_tsquery(q: Query) -> tuple[str, list[str]]:
    sql, args = [], []
    for g in q.groups:
        alts = [_pg_term(t) for t in g]
        sql.append("(" + " || ".join(s for s, _ in alts) + ")")
        args += [a for _, a2 in alts for a in a2]
    expr = " && ".join(sql)
    for t in q.exclude:
        s, a = _pg_term(t)
        expr += f" && !!({s})"
        args += a
    return f"({expr})", args


def _like(t: Term) -> tuple[str, str]:
    text = " ".join(t.words) if t.kind == "prefix" else t.text
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return ("(COALESCE(r.title, '') || ' ' || COALESCE(r.body, '') || ' ' || COALESCE(r.url, '')) "
            "LIKE ? ESCAPE '\\'"), f"%{escaped}%"


def _like_where(q: Query) -> tuple[str, list[str]]:
    sql, args = [], []
    for g in q.groups:
        alts = [_like(t) for t in g]
        sql.append("(" + " OR ".join(s for s, _ in alts) + ")")
        args += [a for _, a in alts]
    for t in q.exclude:
        s, a = _like(t)
        sql.append(f"NOT {s}")
        args.append(a)
    return " AND ".join(sql), args


def _matches(backend: str, q: Query) -> tuple[str, list[Any]]:
    """Objects with a matching version: (object id, rank, newest matching version).
    Lower ranks are better matches."""
    if backend == "fts5":
        return ("SELECT r.object_id AS oid, MIN(m.rank) AS rank, MAX(r.seq) AS best_seq FROM "
                "(SELECT rowid AS rid, rank FROM revisions_fts "
                "WHERE revisions_fts MATCH ? AND rank MATCH 'bm25(10.0, 1.0, 0.5)') m "
                "JOIN revisions r ON r.id=m.rid GROUP BY r.object_id",
                [fts5_match(q)])
    if backend == "postgres":
        tsq, args = pg_tsquery(q)
        vec = PG_VECTOR
        return (f"SELECT r.object_id AS oid, MIN(-ts_rank({vec}, {tsq})) AS rank, MAX(r.seq) AS best_seq "
                f"FROM revisions r WHERE {vec} @@ {tsq} GROUP BY r.object_id", [*args, *args])
    where, args = _like_where(q)
    return (f"SELECT r.object_id AS oid, 0 AS rank, MAX(r.seq) AS best_seq FROM revisions r "
            f"WHERE {where} GROUP BY r.object_id", args)


# -- running it ---------------------------------------------------------------
@dataclass
class Filters:
    kind: str = "all"
    community_id: int | None = None
    author: str = ""
    kept_only: bool = False
    only: str = ""
    sort: str = "relevance"


@dataclass
class Results:
    query: Query
    hits: list[dict[str, Any]]
    total: int
    page: int

    @property
    def pages(self) -> int:
        return max(1, -(-self.total // PAGE_SIZE))


def _author(text: str) -> tuple[str, str | None]:
    """user, u/user, @user or user@host -> (name, host or None)."""
    text = text.strip().lstrip("@")
    if text.lower().startswith("u/"):
        text = text[2:]
    name, _, host = text.partition("@")
    return name.lower(), host.lower() or None


def search(conn: Any, backend: str, text: str, filters: Filters | None = None, page: int = 1) -> Results:
    q = parse(text)
    if not q.groups:
        raise SearchError("Type at least one word to look for." if not q.exclude else
                          "Add a word to look for, not only ones to leave out.")
    f = filters or Filters()
    page = max(1, page)
    match_sql, args = _matches(backend, q)
    where = ["1=1"]
    if f.kind in ("post", "comment"):
        where.append("o.object_type=?")
        args.append(f.kind)
    if f.community_id:
        where.append("t.community_id=?")
        args.append(f.community_id)
    if f.author.strip():
        name, host = _author(f.author)
        where.append("LOWER(a.username)=?")
        args.append(name)
        if host:
            where.append("LOWER(a.instance)=?")
            args.append(host)
    if f.kept_only:
        where.append("t.retention='manual'")
    if f.only == "gone":
        where.append("(o.cur_deleted=1 OR o.cur_removed=1 OR o.cur_missing=1)")
    elif f.only == "edited":
        where.append("o.revision_count>1")
    base = f"""
        FROM ({match_sql}) m
        JOIN objects o ON o.id=m.oid
        JOIN revisions r ON r.object_id=o.id AND r.seq=o.revision_count
        JOIN archived_threads t ON t.id=o.thread_id AND t.trashed_at IS NULL
        JOIN objects ro ON ro.id=t.root_object_id
        JOIN revisions tr ON tr.object_id=ro.id AND tr.seq=ro.revision_count
        LEFT JOIN actors a ON a.id=o.author_id
        LEFT JOIN communities c ON c.id=t.community_id
        WHERE {' AND '.join(where)}"""
    total = conn.execute(f"SELECT COUNT(*) {base}", args).fetchone()[0]
    when = "COALESCE(o.created_at, o.first_seen_at)"
    order = {"new": f"{when} DESC", "old": f"{when} ASC"}.get(f.sort, f"m.rank ASC, {when} DESC")
    rows = conn.execute(
        f"""SELECT o.id, o.object_type, o.thread_id, o.created_at, o.first_seen_at, o.cur_deleted,
                   o.cur_removed, o.cur_missing, o.revision_count, o.score, o.upvotes, o.downvotes,
                   m.rank, m.best_seq, r.title, r.body, r.url, a.username, a.instance AS a_instance,
                   c.id AS cid, c.name AS cname, c.canonical_ap_id AS c_ap, t.retention,
                   tr.title AS thread_title
            {base} ORDER BY {order}, o.id DESC LIMIT ? OFFSET ?""",
        [*args, PAGE_SIZE, (page - 1) * PAGE_SIZE]).fetchall()
    pattern = highlighter(q)
    hits = []
    for r in rows:
        d = dict(r)
        d["matched_current"] = r["best_seq"] == r["revision_count"]
        d["title_html"] = mark(r["title"] or "", pattern) if r["object_type"] == "post" else None
        d["snippet"] = snippet(r["body"], pattern)
        d["url_matched"] = bool(r["url"] and pattern and pattern.search(r["url"]))
        d["earlier"] = d["diff"] = None
        if not d["matched_current"]:  # show the version that matched, too
            old = conn.execute("SELECT title, body FROM revisions WHERE object_id=? AND seq=?",
                               (r["id"], r["best_seq"])).fetchone()
            if old:
                before = " ".join(x for x in (old["title"], old["body"]) if x)
                d["earlier"] = snippet(before, pattern)
                d["diff"] = diff_snippet(before, " ".join(x for x in (r["title"], r["body"]) if x), pattern)
        hits.append(d)
    return Results(q, hits, total, page)


# -- showing matches -----------------------------------------------------------
def highlighter(q: Query) -> re.Pattern[str] | None:
    """A pattern finding the query's words in text, for <mark>ing them."""
    alts = []
    for t in q.positive:
        words = [re.escape(w) for w in t.words]
        if t.kind == "phrase":
            alts.append(r"\W+".join(words))
        elif t.kind == "prefix":
            alts.append(r"\W+".join(words) + r"\w*")
        else:
            alts.extend(words)
    if not alts:
        return None
    alts.sort(key=len, reverse=True)
    return re.compile(r"(?<!\w)(?:" + "|".join(alts) + r")(?!\w)", re.IGNORECASE)


def mark(text: str, pattern: re.Pattern[str] | None) -> Markup:
    if not pattern:
        return Markup(html.escape(text))
    out, pos = [], 0
    for m in pattern.finditer(text):
        out.append(html.escape(text[pos:m.start()]))
        out.append(f"<mark>{html.escape(m.group(0))}</mark>")
        pos = m.end()
    out.append(html.escape(text[pos:]))
    return Markup("".join(out))


_MD_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_MD_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_MD_MARKS = re.compile(r"(?m)^\s*(?:>+|#+|[-*+]\s|\d+\.\s)\s*|[*`~]+|:::\s*spoiler")


def plain(text: str | None) -> str:
    """Markdown flattened to one line of readable text."""
    text = _MD_IMAGE.sub(lambda m: m.group(1), text or "")
    text = _MD_LINK.sub(lambda m: m.group(1), text)
    return re.sub(r"\s+", " ", _MD_MARKS.sub(" ", text)).strip()


def snippet(text: str | None, pattern: re.Pattern[str] | None, width: int = 240) -> Markup:
    """A stretch of the text around its first match (or its start)."""
    flat = plain(text)
    if not flat:
        return Markup("")
    found = pattern.search(flat) if pattern else None
    start = max(0, found.start() - width // 3) if found else 0
    if start:
        space = flat.find(" ", start)
        start = space + 1 if 0 <= space < (found.start() if found else start + 20) else start
    end = min(len(flat), start + width)
    if end < len(flat):
        space = flat.rfind(" ", start, end)
        end = space if space > start + width // 2 else end
    piece = flat[start:end]
    return Markup(("…" if start else "") + mark(piece, pattern) + ("…" if end < len(flat) else ""))


def diff_snippet(old: str | None, new: str | None, pattern: re.Pattern[str] | None,
                 before: int = 12, after: int = 28) -> Markup:
    """How the text changed from the version that matched to the current one,
    word by word, around the first removed match (else the first change)."""
    a, b = plain(old).split(), plain(new).split()
    words: list[tuple[str, str]] = []  # (op, word) with op in " -+"
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op == "equal":
            words += [(" ", w) for w in a[i1:i2]]
            continue
        words += [("-", w) for w in a[i1:i2]] + [("+", w) for w in b[j1:j2]]
    changed = [i for i, (op, _) in enumerate(words) if op != " "]
    if not changed:
        return Markup("")
    hit = next((i for i in changed if words[i][0] == "-" and pattern and pattern.search(words[i][1])), changed[0])
    start, end = max(0, hit - before), min(len(words), hit + after)
    out, run_op, run = [], None, []

    def flush() -> None:
        if run:
            text = mark(" ".join(run), pattern)
            out.append(text if run_op == " " else Markup("<{0}>{1}</{0}>").format(
                "del" if run_op == "-" else "ins", text))

    for op, w in words[start:end]:
        if op != run_op:
            flush()
            run_op, run = op, []
        run.append(w)
    flush()
    return Markup(("… " if start else "") + Markup(" ").join(out) + (" …" if end < len(words) else ""))
