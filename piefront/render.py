"""Markdown as ThreadBNC renders it, except that pictures are shown from where
they are: nothing is archived here, so there are no saved copies to point at.
What people wrote is untrusted; nothing reaches a page without passing through nh3."""

from __future__ import annotations

import html

import nh3
from markdown_it import MarkdownIt
from markupsafe import Markup

from threadbnc.render import ALLOWED_ATTRS, ALLOWED_TAGS, _SPOILER_RE, _linkify


def _parser() -> MarkdownIt:
    md = MarkdownIt("commonmark", {"html": False, "breaks": False})
    md.enable(["table", "strikethrough"])
    md.core.ruler.push("piefront_linkify", _linkify)
    return md


_MD = _parser()


def _image(self, tokens, idx, options, env) -> str:
    tok = tokens[idx]
    src = str(tok.attrGet("src") or "")
    alt = html.escape(self.renderInlineAsText(tok.children or [], options, env))
    if not src.startswith("https://"):  # the page only loads pictures over https (its Content-Security-Policy)
        return f'<a href="{html.escape(src)}">{alt or html.escape(src)}</a>'
    return (f'<a href="{html.escape(src)}" target="_blank"><img class="media" src="{html.escape(src)}" '
            f'alt="{alt}" loading="lazy"></a>')


def _link_open(self, tokens, idx, options, env) -> str:
    tokens[idx].attrSet("target", "_blank")
    return self.renderToken(tokens, idx, options, env)


_MD.add_render_rule("image", _image)
_MD.add_render_rule("link_open", _link_open)


def _render(text: str) -> str:
    return _MD.render(text) if text.strip() else ""


def render_markdown(text: str | None) -> Markup:
    if not text:
        return Markup("")
    parts: list[str] = []
    pos = 0
    for m in _SPOILER_RE.finditer(text):
        parts.append(_render(text[pos:m.start()]))
        title = html.escape(m.group(1).strip() or "Spoiler")
        parts.append(f'<details class="spoiler"><summary>{title}</summary>{_render(m.group(2))}</details>')
        pos = m.end()
    parts.append(_render(text[pos:]))
    return Markup(nh3.clean(
        "".join(parts), tags=ALLOWED_TAGS, attributes=ALLOWED_ATTRS, url_schemes={"http", "https", "mailto"},
        link_rel="noopener noreferrer nofollow", strip_comments=True, filter_style_properties={"text-align"},
    ))
