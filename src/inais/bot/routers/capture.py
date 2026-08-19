"""Two capture paths: forwarded links, and voice-journal entries.

Both are registered before the generic chat/voice handlers, because both intercept ordinary
message types under a narrow condition — a message that is essentially just a URL, and a
voice note while the journal state is armed.
"""

from __future__ import annotations

import io
import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message

from inais import journal
from inais.bot.routers.chat import typing_indicator
from inais.config import settings
from inais.integrations import fetch, stt_tts
from inais.study import links
from inais.textutil import split_message

log = logging.getLogger(__name__)
router = Router(name="capture")


class Journal(StatesGroup):
    waiting_entry = State()


def looks_like_link(message: Message) -> bool:
    return fetch.is_link_message(message.text or "")


# ---------- read it later ----------

@router.message(F.text, looks_like_link)
async def on_link(message: Message) -> None:
    url = fetch.extract_urls(message.text or "")[0]
    if not settings().db_enabled:
        await message.answer("I need a database (SUPABASE_DB_URL) to save reading.")
        return

    note = await message.answer("🔗 Reading that…")
    try:
        page = await fetch.fetch_page(url)
    except fetch.FetchError as e:
        await note.edit_text(f"Couldn't save that link — {e}.")
        return
    except Exception:
        log.exception("link fetch failed")
        await note.edit_text("Couldn't fetch that link — check the logs.")
        return

    try:
        result = await links.save(page)
    except Exception:
        log.exception("link save failed")
        await note.edit_text("Fetched it, but saving failed — check the logs.")
        return
    if result is None:
        await note.edit_text("Couldn't save that (no database).")
        return

    verb = "Updated" if result["resaved"] else "Saved"
    body = (f"🔗 {verb}: {page.title[:120]}\n"
            f"{page.words:,} words · {result['chunks']} chunks · searchable now")
    if result["summary"]:
        body += f"\n\n{result['summary']}"
    body += "\n\n/links to see everything saved."
    chunks = split_message(body)
    await note.edit_text(chunks[0], disable_web_page_preview=True)
    for chunk in chunks[1:]:
        await message.answer(chunk, disable_web_page_preview=True)


@router.message(Command("links"))
async def cmd_links(message: Message) -> None:
    for chunk in split_message(links.render(await links.recent())):
        await message.answer(chunk, disable_web_page_preview=True)


# ---------- voice journal ----------

@router.message(Command("journal"))
async def cmd_journal(message: Message, state: FSMContext) -> None:
    inline = (message.text or "").partition(" ")[2].strip()
    if inline:
        await _store(message, inline, source="text")
        return
    await state.set_state(Journal.waiting_entry)
    await message.answer(
        "📓 Go ahead — send me a voice note about how things are going.\n"
        "It stays private in your database; I'll track patterns over time.\n(/cancel to stop.)")


@router.message(Journal.waiting_entry, F.text == "/cancel")
async def on_journal_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Journal cancelled.")


@router.message(Journal.waiting_entry, F.voice)
async def on_journal_voice(message: Message, state: FSMContext) -> None:
    await state.clear()
    buf = io.BytesIO()
    await message.bot.download(message.voice, destination=buf)
    async with typing_indicator(message.bot, message.chat.id):
        try:
            transcript = await stt_tts.transcribe_voice(buf.getvalue(),
                                                        message.voice.duration or 0)
        except Exception:
            log.exception("journal transcription failed")
            await message.answer("Couldn't transcribe that — try again?")
            return
        await _store(message, transcript, source="voice")


@router.message(Journal.waiting_entry, F.text & ~F.text.startswith("/"))
async def on_journal_text(message: Message, state: FSMContext) -> None:
    await state.clear()
    await _store(message, message.text or "", source="text")


async def _store(message: Message, transcript: str, source: str) -> None:
    if not transcript.strip():
        await message.answer("There was nothing in that to save.")
        return
    try:
        entry = await journal.add_entry(transcript, source=source)
    except Exception:
        log.exception("journal save failed")
        await message.answer("Couldn't save that entry — check the logs.")
        return
    if entry is None:
        await message.answer("Couldn't save that (no database).")
        return
    icon = journal.MOOD_ICONS.get(entry["mood"], "📓")
    topics = f" · {', '.join(entry['topics'])}" if entry["topics"] else ""
    await message.answer(
        f"{icon} Saved — reading that as *{entry['mood']}*{topics}".replace("*", ""))


@router.message(Command("mood"))
async def cmd_mood(message: Message) -> None:
    arg = (message.text or "").partition(" ")[2].strip()
    days = int(arg) if arg.isdigit() and 1 <= int(arg) <= 90 else 14
    await message.answer(await journal.trend(days))
