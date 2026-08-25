"""Guap Books delivery job: send finished ebooks (PDF + flyer) to the chat that asked.

Delivery invariant: send everything FIRST, mark delivered_at ONLY after every send succeeded.
A crash in between re-delivers on the next tick — a rare duplicate beats a dropped book.
One bad job never blocks the rest (per-job try/except).
"""

from __future__ import annotations

import logging
import re

from aiogram.types import BufferedInputFile

from inais import controls, db
from inais.config import settings
from inais.integrations import guapbooks

log = logging.getLogger(__name__)


def _filename(title: str) -> str:
    cleaned = re.sub(r"[^\w\- ]", "", title or "book")[:60].strip().replace(" ", "_")
    return f"{cleaned or 'book'}.pdf"


async def _deliver_book(bot, job: dict) -> None:
    chat_id = job["requester_chat_id"]
    title = job.get("title") or "Your book"
    if not job.get("pdf_path"):
        raise guapbooks.GuapBooksError(f"job {job['id']} is done but the book has no pdf_path")
    pdf = await guapbooks.fetch_asset(job["pdf_path"])
    await bot.send_document(
        chat_id, BufferedInputFile(pdf, filename=_filename(title)),
        caption=f"📗 {title} — Books by Keeping Up With Guap"[:1024])
    if job.get("flyer_path"):
        try:
            flyer = await guapbooks.fetch_asset(job["flyer_path"])
            await bot.send_photo(
                chat_id, BufferedInputFile(flyer, filename="flyer.png"),
                caption="The promo flyer — post it when you list the book. 🚀"[:1024])
        except Exception:
            # The book made it — a missing flyer shouldn't hold delivery hostage.
            log.exception("flyer send failed for job %s (book delivered)", job["id"])


async def poll(bot) -> int:
    """Deliver every finished, unsent telegram job. Returns how many were delivered."""
    cfg = settings()
    if not cfg.guapbooks_enabled or db.pool() is None or controls.is_paused():
        return 0
    try:
        jobs = await guapbooks.ready_undelivered()
    except Exception:
        log.exception("guap books poll failed")
        return 0

    delivered = 0
    for job in jobs:
        try:
            if job["kind"] == "ideas":
                await bot.send_message(job["requester_chat_id"],
                                       guapbooks.render_ideas(job.get("result") or {}))
            else:
                await _deliver_book(bot, job)
            await guapbooks.mark_delivered(str(job["id"]))
            delivered += 1
        except Exception:
            log.exception("delivery failed for guap job %s", job.get("id"))
    return delivered
