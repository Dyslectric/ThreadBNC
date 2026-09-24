"""Markdown rendering for archived content (Lemmy-flavoured CommonMark).

Pipeline: markdown-it (raw HTML disabled) -> media URLs rewritten to archived
copies -> nh3 sanitisation. Archived text is untrusted; nothing reaches the page
without passing through nh3.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlparse

import nh3
from markdown_it import MarkdownIt
from markdown_it.token import Token
from markupsafe import Markup

MEDIA_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".bmp", ".heic",
                    ".mp4", ".webm", ".mov", ".m4v", ".gifv",
                    ".mp3", ".m4a", ".aac", ".ogg", ".oga", ".opus", ".flac", ".wav")
VIDEO_EXTENSIONS = (".mp4", ".webm", ".mov", ".m4v", ".gifv")
AUDIO_EXTENSIONS = (".mp3", ".m4a", ".aac", ".ogg", ".oga", ".opus", ".flac", ".wav")

_URL_RE = re.compile(r"https?://[^\s<>()\[\]\"']+[^\s<>()\[\]\"'.,;:!?*_~]", re.IGNORECASE)
_SPOILER_RE = re.compile(r"^:::\s*spoiler\s*(.*?)\n(.*?)\n:::[ \t]*$", re.MULTILINE | re.DOTALL)


_ESCAPE = re.compile(r"([\\`*_\[\]<>])")
_LINE_START = re.compile(r"^(\s*)([#>+-]|\d+[.)])(?=\s|$)")


def escape_markdown(text: str) -> str:
    """Plain text that Markdown shows as written, within a line."""
    return _ESCAPE.sub(r"\\\1", text)


def plain_lines(lines: list[str]) -> str | None:
    """Lines of plain text (already escaped with escape_markdown, links made
    links) as Markdown that keeps them: line breaks kept, blank lines as
    paragraph breaks, and nothing at the start of one taken for a heading,
    quote or list."""
    lines = [_LINE_START.sub(lambda m: m.group(1) + m.group(2)[:-1] + "\\" + m.group(2)[-1], line.rstrip())
             for line in lines]
    out = ""
    for i, line in enumerate(lines):
        if i:  # a hard line break between lines of a paragraph; blank lines stay paragraph breaks
            out += "\\\n" if line and lines[i - 1] else "\n"
        out += line
    return out or None


def looks_like_media(url: str | None) -> bool:
    if not url:
        return False
    path = urlparse(url).path.lower()
    return path.endswith(MEDIA_EXTENSIONS) or "/pictrs/image/" in path


def looks_like_audio(url: str | None) -> bool:
    return bool(url) and urlparse(url).path.lower().endswith(AUDIO_EXTENSIONS)


def sole_link(text: str | None) -> str | None:
    """Return the URL when Markdown consists only of one HTTP(S) link.

    The link label may contain formatting, but there must be no visible text
    outside it. This covers Markdown links, autolinks and bare URLs after the
    parser's linkification rule has run.
    """
    if not text:
        return None
    blocks = _MD.parse(text)
    inlines = [t for t in blocks if t.type == "inline"]
    if len(inlines) != 1 or any(t.type not in ("paragraph_open", "inline", "paragraph_close") for t in blocks):
        return None
    links: list[str] = []
    depth = 0
    for token in inlines[0].children or []:
        if token.type == "link_open":
            href = str(token.attrGet("href") or "")
            if depth == 0:
                links.append(href)
            depth += 1
        elif token.type == "link_close":
            depth = max(0, depth - 1)
        elif depth == 0 and token.type in ("text", "code_inline", "html_inline", "image") \
                and token.content.strip():
            return None
    if len(links) != 1 or urlparse(links[0]).scheme not in ("http", "https"):
        return None
    return links[0]


def _linkify(state: Any) -> None:
    """Core rule: turn bare http(s) URLs in text into links (no extra dependency)."""
    for block in state.tokens:
        if block.type != "inline" or not block.children:
            continue
        out: list[Token] = []
        in_link = 0
        for tok in block.children:
            if tok.type == "link_open":
                in_link += 1
            elif tok.type == "link_close":
                in_link -= 1
            if tok.type != "text" or in_link or not _URL_RE.search(tok.content):
                out.append(tok)
                continue
            pos = 0
            for m in _URL_RE.finditer(tok.content):
                if m.start() > pos:
                    t = Token("text", "", 0)
                    t.content = tok.content[pos:m.start()]
                    out.append(t)
                lo = Token("link_open", "a", 1)
                lo.attrs = {"href": m.group(0)}
                lo.markup, lo.info = "linkify", "auto"
                txt = Token("text", "", 0)
                txt.content = m.group(0)
                lc = Token("link_close", "a", -1)
                lc.markup, lc.info = "linkify", "auto"
                out.extend([lo, txt, lc])
                pos = m.end()
            if pos < len(tok.content):
                t = Token("text", "", 0)
                t.content = tok.content[pos:]
                out.append(t)
        block.children = out


VIDEO_HREF = "/youtube/v/{}"  # the box a YouTube link opens (web.py, app.js)
VIDEO_LINK_TITLE = "YouTube video: see how big it is, then save it here or watch it on YouTube"
_YOUTUBE_ADDRESS = re.compile(r"^(?:https?://)?(?:[\w-]+\.)*(?:youtube\.com|youtu\.be)/", re.IGNORECASE)
VideoTitles = Callable[[str], "str | None"]


def video_link_text(text: str, href: str) -> bool:
    """Whether a link's text is just its address (so the video's title can stand in)."""
    text = text.strip()
    return text == href or bool(_YOUTUBE_ADDRESS.match(text))


def _video_links(state: Any) -> None:
    """Core rule: links to YouTube videos open their box here (app.js) and,
    where their text is just the address, show the video's title instead;
    one not known yet is marked `untitled` for app.js to ask for. Only when
    rendering (sole_link and extract_media_urls see the links as written)."""
    if "titles" not in state.env:
        return
    from .youtube import video_id  # youtube.py imports this module

    for block in state.tokens:
        if block.type != "inline" or not block.children:
            continue
        kids = block.children
        for i, tok in enumerate(kids):
            vid = video_id(str(tok.attrGet("href") or "")) if tok.type == "link_open" else None
            if not vid:
                continue
            href = str(tok.attrGet("href"))
            tok.attrSet("href", VIDEO_HREF.format(vid))
            tok.attrSet("class", "video-link")
            tok.attrSet("title", VIDEO_LINK_TITLE)
            text = kids[i + 1] if i + 2 < len(kids) and kids[i + 2].type == "link_close" else None
            if text is not None and text.type == "text" and video_link_text(text.content, href):
                title = state.env["titles"](vid)
                if title:
                    text.content = title
                else:
                    tok.attrSet("class", "video-link untitled")


def _parser() -> MarkdownIt:
    md = MarkdownIt("commonmark", {"html": False, "breaks": False})
    md.enable(["table", "strikethrough"])
    md.core.ruler.push("threadbnc_linkify", _linkify)
    md.core.ruler.push("threadbnc_video_links", _video_links)
    return md


_MD = _parser()


def extract_media_urls(text: str | None) -> list[str]:
    """Embedded images, plus links that point straight at image/video files."""
    if not text:
        return []
    found: list[str] = []

    def walk(tokens: list[Token]) -> None:
        for t in tokens:
            if t.type == "image":
                src = t.attrGet("src")
                if src:
                    found.append(str(src))
            elif t.type == "link_open":
                href = t.attrGet("href")
                if href and looks_like_media(str(href)):
                    found.append(str(href))
            if t.children:
                walk(t.children)

    walk(_MD.parse(_strip_spoiler_markers(text)))
    seen: set[str] = set()
    return [u for u in found if urlparse(u).scheme in ("http", "https") and not (u in seen or seen.add(u))]


def _strip_spoiler_markers(text: str) -> str:
    return _SPOILER_RE.sub(lambda m: f"{m.group(1)}\n\n{m.group(2)}", text)


@dataclass
class MediaInfo:
    id: int
    status: str  # pending | ok | failed | skipped
    content_type: str | None
    error: str | None = None
    size_bytes: int | None = None
    original_bytes: int | None = None  # transcoded down from this
    original_type: str | None = None
    transcode_error: str | None = None


MediaLookup = Callable[[str], "MediaInfo | None"]

ALLOWED_TAGS = {
    "p", "br", "hr", "strong", "em", "del", "s", "code", "pre", "blockquote", "ul", "ol", "li", "a",
    "img", "video", "audio", "h1", "h2", "h3", "h4", "h5", "h6", "table", "thead", "tbody", "tr", "th", "td",
    "details", "summary", "sup", "sub", "span",
}
ALLOWED_ATTRS = {
    "a": {"href", "title", "class", "target"},
    "img": {"src", "alt", "title", "loading", "class"},
    "video": {"src", "controls", "loop", "muted", "playsinline", "preload", "class", "title"},
    "audio": {"src", "controls", "preload", "class", "title"},
    "ol": {"start"},
    "th": {"style"},
    "td": {"style"},
    "span": {"class", "title"},
    "code": {"class"},
    "details": {"class"},
}


def _media_html(url: str, alt: str, info: MediaInfo | None) -> str:
    esc_alt = html.escape(alt or "")
    host = html.escape(urlparse(url).hostname or url)
    if info and info.status == "ok":
        src = f"/media/{info.id}"
        if (info.content_type or "").startswith("video/"):
            return (f'<video class="media" src="{src}" controls loop muted playsinline preload="metadata" '
                    f'title="{esc_alt}"></video>')
        if (info.content_type or "").startswith("audio/"):
            return f'<audio class="media" src="{src}" controls preload="metadata" title="{esc_alt}"></audio>'
        if info.content_type == "image/svg+xml":  # never inline SVG
            return f'<a class="media-note" href="{src}" target="_blank">[SVG image: {esc_alt or host}]</a>'
        return f'<a href="{src}" target="_blank"><img class="media" src="{src}" alt="{esc_alt}" loading="lazy"></a>'
    if info and info.status == "skipped":
        return f'<a href="{html.escape(url)}" target="_blank">{esc_alt or html.escape(url)}</a>'
    state = "not archived: " + html.escape(info.error or "failed") if info and info.status == "failed" \
        else "archiving pending"
    path = urlparse(url).path.lower()
    kind = "video" if path.endswith(VIDEO_EXTENSIONS) else "audio" if path.endswith(AUDIO_EXTENSIONS) else "image"
    return (f'<a class="media-note" href="{html.escape(url)}" target="_blank">'
            f'[{kind}{": " + esc_alt if esc_alt else ""} · {host} · {state}]</a>')


def render_markdown(text: str | None, lookup: MediaLookup | None = None,
                    titles: VideoTitles | None = None) -> Markup:
    """`titles`: YouTube videos' titles by id, for links to them (see _video_links)."""
    if not text:
        return Markup("")
    lookup = lookup or (lambda _u: None)
    titles = titles or (lambda _v: None)
    parts: list[str] = []
    pos = 0
    for m in _SPOILER_RE.finditer(text):
        parts.append(_render(text[pos:m.start()], lookup, titles))
        title = html.escape(m.group(1).strip() or "Spoiler")
        parts.append(f'<details class="spoiler"><summary>{title}</summary>{_render(m.group(2), lookup, titles)}</details>')
        pos = m.end()
    parts.append(_render(text[pos:], lookup, titles))
    cleaned = nh3.clean(
        "".join(parts), tags=ALLOWED_TAGS, attributes=ALLOWED_ATTRS, url_schemes={"http", "https", "mailto"},
        link_rel="noopener noreferrer nofollow", strip_comments=True,
        filter_style_properties={"text-align"},
    )
    return Markup(cleaned)


def _rule_image(self: Any, tokens: list[Token], idx: int, options: Any, env: dict[str, Any]) -> str:
    tok = tokens[idx]
    src = str(tok.attrGet("src") or "")
    alt = self.renderInlineAsText(tok.children or [], options, env)
    return _media_html(src, alt, env["lookup"](src))


def _rule_link_open(self: Any, tokens: list[Token], idx: int, options: Any, env: dict[str, Any]) -> str:
    tok = tokens[idx]
    env["link_stack"].append(str(tok.attrGet("href") or ""))
    if not str(tok.attrGet("class") or "").startswith("video-link"):  # a video's box opens here
        tok.attrSet("target", "_blank")
    return self.renderToken(tokens, idx, options, env)


def _rule_link_close(self: Any, tokens: list[Token], idx: int, options: Any, env: dict[str, Any]) -> str:
    href = env["link_stack"].pop() if env["link_stack"] else ""
    out = self.renderToken(tokens, idx, options, env)
    info = env["lookup"](href) if looks_like_media(href) else None
    if info and info.status == "ok":
        out += f' <a class="archived-copy" href="/media/{info.id}" target="_blank">[archived copy]</a>'
    return out


# Rules are installed once; per-call state (the media lookup) travels in `env`,
# so concurrent renders on different threads don't interfere.
_MD.add_render_rule("image", _rule_image)
_MD.add_render_rule("link_open", _rule_link_open)
_MD.add_render_rule("link_close", _rule_link_close)


def _render(text: str, lookup: MediaLookup, titles: VideoTitles) -> str:
    if not text.strip():
        return ""
    return _MD.render(text, {"lookup": lookup, "link_stack": [], "titles": titles})
