"""Guap Books commands — request ebooks/ideas from Telegram; the studio builds them.

The bot never generates a word of book content (zero llm.py calls). /ebook and /ebookideas
insert rows into the guap_jobs queue; the Claude Code studio claims them, and books_watch
delivers the finished PDF + flyer back to this chat. Topics are DATA, not instructions.
"""

from __future__ import annotations

import logging

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from inais import db
from inais.integrations import guapbooks
from inais.textutil import split_message

log = logging.getLogger(__name__)
router = Router(name="books")

MAX_TOPIC_CHARS = 300


async def _guard(message: Message) -> bool:
    if db.pool() is None:
        await message.answer("The book factory needs the database — SUPABASE_DB_URL isn't working.")
        return False
    return True


async def _run(message: Message, coro):
    """Funnel factory errors into a readable reply. Returns the result, or None on failure."""
    try:
        return await coro
    except guapbooks.GuapBooksError as e:
        await message.answer(f"⚠️ {e}")
    except Exception:
        log.exception("guap books call failed")
        await message.answer("Couldn't reach the book factory — check the logs.")
    return None


@router.message(Command("ebook"))
async def cmd_ebook(message: Message) -> None:
    """/ebook <topic> — queue a full book (short format for Telegram requests)."""
    if not await _guard(message):
        return
    topic = (message.text or "").partition(" ")[2].strip()
    if not topic:
        await message.answer("What should the book be about? e.g. /ebook 7 money habits that stick")
        return
    payload = {"topic": topic[:MAX_TOPIC_CHARS], "length": "short"}
    job_id = await _run(message, guapbooks.queue_job("full", payload, message.chat.id))
    if job_id:
        await message.answer(
            f"📚 On it — \"{topic[:80]}\" is in the queue.\n"
            "The studio will research, write, fact-check and design it, then I'll send the "
            "PDF + flyer right here. /books shows progress.")


@router.message(Command("ebookideas"))
async def cmd_ebookideas(message: Message) -> None:
    """/ebookideas [seed topic] — queue an idea batch."""
    if not await _guard(message):
        return
    topic = (message.text or "").partition(" ")[2].strip()
    payload = {"topic": topic[:MAX_TOPIC_CHARS] or None, "count": 5}
    job_id = await _run(message, guapbooks.queue_job("ideas", payload, message.chat.id))
    if job_id:
        what = f"ideas around \"{topic[:80]}\"" if topic else "fresh trend-researched ideas"
        await message.answer(f"💡 Queued {what} — I'll drop the list here when the studio's done.")


@router.message(Command("books"))
async def cmd_books(message: Message) -> None:
    """/books — factory overview + your recent requests."""
    if not await _guard(message):
        return
    data = await _run(message, guapbooks.overview())
    if data is None:
        return
    jobs = await _run(message, guapbooks.jobs_for_chat(message.chat.id))
    text = guapbooks.render_overview(data)
    if jobs:
        text += "\n\n" + guapbooks.render_jobs(jobs)
    for chunk in split_message(text):
        await message.answer(chunk)
