"""Build a PDF from markdown-ish text and hand back the bytes.

Uses PyMuPDF's Story engine (already a dependency for ingestion) to flow content across as
many pages as it needs — no reportlab, no extra wheel. Input is a light markdown subset:
`#`/`##`/`###` headings, `-`/`*` bullets, `1.` numbered items, `**bold**`, `` `code` ``, and
blank-line-separated paragraphs. Everything is HTML-escaped before styling, so user or model
text can never inject markup.
"""

from __future__ import annotations

import html
import io
import logging
import re

log = logging.getLogger(__name__)

_BOLD = re.compile(r"\*\*(.+?)\*\*")
_CODE = re.compile(r"`([^`]+)`")

_CSS = """
body { font-family: sans-serif; font-size: 11pt; color: #1a1a1a; line-height: 1.5; }
h1 { font-size: 20pt; margin: 0 0 4pt 0; }
h2 { font-size: 15pt; margin: 14pt 0 4pt 0; }
h3 { font-size: 12pt; margin: 10pt 0 3pt 0; color: #333; }
p  { margin: 0 0 8pt 0; }
li { margin: 0 0 3pt 0; }
code { font-family: monospace; background: #f0f0f0; padding: 0 2pt; }
.meta { color: #888; font-size: 9pt; margin: 0 0 12pt 0; }
"""


def _inline(text: str) -> str:
    """Escape, then re-apply the inline markup we allow."""
    out = html.escape(text)
    out = _BOLD.sub(r"<b>\1</b>", out)
    out = _CODE.sub(r"<code>\1</code>", out)
    return out


def markdown_to_html(title: str, body: str, subtitle: str = "") -> str:
    """Light markdown → HTML. Deliberately small; unknown syntax is treated as plain text."""
    parts: list[str] = [f"<h1>{html.escape(title)}</h1>"]
    if subtitle:
        parts.append(f'<p class="meta">{html.escape(subtitle)}</p>')

    lines = body.replace("\r\n", "\n").split("\n")
    list_mode: str | None = None  # "ul" | "ol" | None
    para: list[str] = []

    def flush_para() -> None:
        if para:
            parts.append(f"<p>{' '.join(para)}</p>")
            para.clear()

    def close_list() -> None:
        nonlocal list_mode
        if list_mode:
            parts.append(f"</{list_mode}>")
            list_mode = None

    for raw in lines:
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped:
            flush_para()
            close_list()
            continue
        if stripped.startswith("### "):
            flush_para()
            close_list()
            parts.append(f"<h3>{_inline(stripped[4:])}</h3>")
        elif stripped.startswith("## "):
            flush_para()
            close_list()
            parts.append(f"<h2>{_inline(stripped[3:])}</h2>")
        elif stripped.startswith("# "):
            flush_para()
            close_list()
            parts.append(f"<h2>{_inline(stripped[2:])}</h2>")
        elif stripped[:2] in ("- ", "* "):
            flush_para()
            if list_mode != "ul":
                close_list()
                parts.append("<ul>")
                list_mode = "ul"
            parts.append(f"<li>{_inline(stripped[2:])}</li>")
        elif re.match(r"\d+\.\s", stripped):
            flush_para()
            if list_mode != "ol":
                close_list()
                parts.append("<ol>")
                list_mode = "ol"
            parts.append(f"<li>{_inline(stripped.split('.', 1)[1].strip())}</li>")
        else:
            close_list()
            para.append(_inline(stripped))
    flush_para()
    close_list()
    return f"<html><head><style>{_CSS}</style></head><body>{''.join(parts)}</body></html>"


def build_pdf(title: str, body: str, subtitle: str = "") -> bytes:
    """Render markdown-ish text to PDF bytes. Raises on a PyMuPDF failure."""
    import pymupdf  # lazy: the bot boots without the wheel present

    doc_html = markdown_to_html(title, body, subtitle)
    buffer = io.BytesIO()
    writer = pymupdf.DocumentWriter(buffer)
    story = pymupdf.Story(html=doc_html)
    media = pymupdf.paper_rect("a4")
    frame = media + (54, 54, -54, -54)   # ~0.75in margins
    more = True
    guard = 0
    while more and guard < 200:          # 200-page ceiling: a chat PDF is never a book
        device = writer.begin_page(media)
        more, _ = story.place(frame)
        story.draw(device)
        writer.end_page()
        guard += 1
    writer.close()
    return buffer.getvalue()
