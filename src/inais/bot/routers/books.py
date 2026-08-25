"""Guap Books commands — request ebooks/ideas from Telegram; the studio builds them.

The bot never generates a word of book content (zero llm.py calls). /ebook and /ebookideas
insert rows into the guap_jobs queue; the Claude Code studio claims them, and books_watch
delivers the finished PDF + flyer back to this chat. Topics are DATA, not instructions.
"""

from __future__ import annotations

import logging
import re

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from inais import db
from inais.bot import keyboards
from inais.integrations import guapbooks
from inais.textutil import split_message

log = logging.getLogger(__name__)
router = Router(name="books")

MAX_TOPIC_CHARS = 300
# which download button maps to which storage-path column on the book row
_ASSET_COLUMN = {"pdf": "pdf_path", "cover": "cover_path", "flyer": "flyer_path"}


def _pdf_name(title: str) -> str:
    cleaned = re.sub(r"[^\w\- ]", "", title or "book")[:60].strip().replace(" ", "_")
    return f"{cleaned or 'book'}.pdf"


async def _safe_edit(message, text: str, markup=None) -> None:
    try:
        await message.edit_text(text, reply_markup=markup, disable_web_page_preview=True)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise


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


# ---------- library: browse + download finished books ----------

@router.message(Command("library"))
async def cmd_library(message: Message) -> None:
    """/library — your finished ebooks, as buttons you can open and download."""
    if not await _guard(message):
        return
    books = await _run(message, guapbooks.list_books(status="ready", limit=10))
    if books is None:
        return
    await message.answer(guapbooks.render_library(books),
                         reply_markup=keyboards.books_library_kb(books))


async def _show_library(message) -> None:
    books = await _run(message, guapbooks.list_books(status="ready", limit=10))
    if books is not None:
        await _safe_edit(message, guapbooks.render_library(books),
                         keyboards.books_library_kb(books))


@router.callback_query(F.data == "gblib")
async def on_library(cb: CallbackQuery) -> None:
    await cb.answer()
    if cb.message is not None:
        await _show_library(cb.message)


@router.callback_query(F.data.startswith("gbk:"))
async def on_book_detail(cb: CallbackQuery) -> None:
    book_id = (cb.data or "gbk:").split(":", 1)[1]
    await cb.answer("Loading…")
    if cb.message is None:
        return
    book = await _run(cb.message, guapbooks.get_book(book_id))
    if not book:
        await cb.message.answer("That book isn't in the library any more.")
        return
    await _safe_edit(cb.message, guapbooks.render_book_detail(book),
                     keyboards.book_actions_kb(book))


@router.callback_query(F.data.startswith("gbdl:"))
async def on_book_download(cb: CallbackQuery) -> None:
    try:
        _, kind, book_id = (cb.data or "gbdl::").split(":", 2)
    except ValueError:
        await cb.answer("Bad request.")
        return
    column = _ASSET_COLUMN.get(kind)
    if cb.message is None or column is None:
        await cb.answer("Nothing to download.")
        return
    await cb.answer("Fetching…")
    book = await _run(cb.message, guapbooks.get_book(book_id))
    if not book:
        await cb.message.answer("That book isn't available.")
        return
    path = book.get(column)
    if not path:
        await cb.message.answer("That file isn't ready for this book yet.")
        return
    data = await _run(cb.message, guapbooks.fetch_asset(path))
    if data is None:
        return
    title = book.get("title") or "book"
    if kind == "pdf":
        await cb.message.answer_document(
            BufferedInputFile(data, filename=_pdf_name(title)),
            caption=f"📗 {title} — Books by Keeping Up With Guap"[:1024])
    else:
        await cb.message.answer_photo(
            BufferedInputFile(data, filename=f"{kind}.png"),
            caption=(f"🖼 {title} — cover" if kind == "cover" else "🚀 Promo flyer")[:1024])
