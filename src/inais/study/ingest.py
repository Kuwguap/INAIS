"""PDF ingestion: extract text with PyMuPDF, chunk it, embed it into doc_chunks."""

from __future__ import annotations

import asyncio
import logging
import re

from inais import db, llm

log = logging.getLogger(__name__)

CHUNK_CHARS = 1200      # ≈300 tokens
CHUNK_OVERLAP = 150
MAX_PDF_BYTES = 20 * 1024 * 1024   # Telegram bots cannot download more than 20 MB
EMBED_CONCURRENCY = 4


class PdfTooLarge(Exception):
    pass


class PdfUnreadable(Exception):
    pass


def _extract_sync(data: bytes) -> tuple[str, int, str]:
    """Returns (text, page_count, embedded_title)."""
    import pymupdf  # imported lazily so the bot boots without the wheel present

    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except Exception as e:
        raise PdfUnreadable(str(e)) from e
    try:
        pages = [page.get_text("text") for page in doc]
        title = (doc.metadata or {}).get("title") or ""
    finally:
        doc.close()
    return "\n\n".join(pages), len(pages), title.strip()


def chunk_text(text: str, size: int = CHUNK_CHARS, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Split on paragraph boundaries where possible, with a little overlap for context."""
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not text:
        return []

    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            window = text[start:end]
            cut = window.rfind("\n\n")
            if cut < size // 2:
                cut = window.rfind(". ")
            if cut > size // 3:
                end = start + cut + 1
        piece = text[start:end].strip()
        if len(piece) > 40:  # skip page-number / header fragments
            chunks.append(piece)
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return chunks


async def ingest_pdf(data: bytes, filename: str, exam_id: int | None = None) -> dict:
    """Store a PDF's chunks. Returns {document_id, title, pages, chunks}."""
    if len(data) > MAX_PDF_BYTES:
        raise PdfTooLarge(f"{len(data) / 1_048_576:.1f} MB")
    p = db.pool()
    if p is None:
        raise RuntimeError("No database configured — documents need one.")

    text, pages, meta_title = await asyncio.to_thread(_extract_sync, data)
    chunks = chunk_text(text)
    if not chunks:
        raise PdfUnreadable("no extractable text (is it a scanned image?)")

    title = meta_title or filename.rsplit(".", 1)[0].replace("_", " ").strip() or filename
    row = await p.fetchrow(
        "insert into documents (filename, title, pages, exam_id) values ($1, $2, $3, $4)"
        " returning id",
        filename, title[:200], pages, exam_id,
    )
    doc_id = row["id"]

    sem = asyncio.Semaphore(EMBED_CONCURRENCY)

    async def store_chunk(index: int, content: str) -> None:
        async with sem:
            try:
                vec = llm.vec_literal(await llm.embed(content))
            except Exception:
                log.exception("embedding chunk %s of doc %s failed", index, doc_id)
                vec = None
            await p.execute(
                "insert into doc_chunks (document_id, chunk_index, content, embedding)"
                " values ($1, $2, $3, $4::vector)",
                doc_id, index, content, vec,
            )

    await asyncio.gather(*(store_chunk(i, c) for i, c in enumerate(chunks)))
    log.info("ingested %s: %s pages, %s chunks", filename, pages, len(chunks))
    return {"document_id": doc_id, "title": title, "pages": pages, "chunks": len(chunks)}
