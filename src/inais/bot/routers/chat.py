"""Free-text conversation — the main entry into the brain."""

from __future__ import annotations

import asyncio
import contextlib
import logging

from aiogram import F, Router
from aiogram.types import Message

from inais.orchestrator import loop
from inais.textutil import error_reply, split_message, strip_voice_label, wants_voice

log = logging.getLogger(__name__)
router = Router(name="chat")

_MAX_VOICE_NOTES = 3  # bound TTS cost on a long, explicitly-voiced reply


async def _send_voice_reply(message, reply: str) -> None:
    """Speak an EXPLICITLY-requested reply as Telegram voice bubbles.

    Chunks a long reply into up to 3 notes instead of silently dropping it, and — because the
    request was explicit — SAYS SO if TTS fails, rather than leaving text-only (which is the
    exact "why didn't it send a voice note" failure we're fixing)."""
    from aiogram.types import BufferedInputFile

    from inais.integrations import stt_tts

    body = strip_voice_label(reply).strip()
    if not body:
        return
    chunks = split_message(body, limit=stt_tts.TTS_MAX_CHARS)
    sent = 0
    try:
        for chunk in chunks[:_MAX_VOICE_NOTES]:
            ogg = await stt_tts.synthesize_voice(chunk)
            if not ogg:
                break
            await message.answer_voice(BufferedInputFile(ogg, filename="reply.ogg"))
            sent += 1
    except Exception:
        log.exception("voice reply synthesis failed")
    if sent == 0:
        await message.answer(
            "⚠️ Couldn't send that as a voice note (TTS failed). The text is above.")
    elif len(chunks) > _MAX_VOICE_NOTES:
        await message.answer("(Voiced the first part — the rest is in the text above.)")


@contextlib.asynccontextmanager
async def typing_indicator(bot, chat_id: int):
    """Telegram's typing action lasts ~5s — repeat it while the brain works."""

    async def _loop() -> None:
        with contextlib.suppress(Exception):
            while True:
                await bot.send_chat_action(chat_id, "typing")
                await asyncio.sleep(4.5)

    task = asyncio.create_task(_loop())
    try:
        yield
    finally:
        task.cancel()


@router.message(F.text & ~F.text.startswith("/"))
async def on_text(message: Message) -> None:
    async with typing_indicator(message.bot, message.chat.id):
        try:
            result = await loop.handle_text(message.bot, message.chat.id, message.text or "")
        except Exception as e:
            log.exception("brain failed on text turn")
            result = loop.TurnResult(error_reply(e))

    explicit = wants_voice(message.text or "")
    # if voice was explicitly asked for, don't show the model's fake "Voice note:" label
    display = strip_voice_label(result.text) if explicit else result.text
    for chunk in split_message(display):
        await message.answer(chunk)

    # deterministic backstop: an explicit request always gets voice — unless the model already
    # sent one via the speak tool this turn (result.voice_sent), which would be a duplicate.
    if explicit and not result.voice_sent:
        await _send_voice_reply(message, result.text)
