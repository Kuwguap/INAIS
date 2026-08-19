"""Free-text conversation — the main entry into the brain."""

from __future__ import annotations

import asyncio
import contextlib
import logging

from aiogram import F, Router
from aiogram.types import Message

from inais.orchestrator import loop
from inais.textutil import error_reply, split_message, wants_voice

log = logging.getLogger(__name__)
router = Router(name="chat")


async def _send_voice_reply(message, reply: str) -> None:
    """Speak a reply as a Telegram voice bubble; silently no-op if TTS/ffmpeg is unavailable."""
    from aiogram.types import BufferedInputFile

    from inais.integrations import stt_tts

    try:
        ogg = await stt_tts.synthesize_voice(reply)
        if ogg:
            await message.answer_voice(BufferedInputFile(ogg, filename="reply.ogg"))
    except Exception:
        log.exception("voice reply synthesis failed — text already sent")


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
            reply = await loop.handle_text(message.bot, message.chat.id, message.text or "")
        except Exception as e:
            log.exception("brain failed on text turn")
            reply = error_reply(e)
    for chunk in split_message(reply):
        await message.answer(chunk)

    # deterministic: an explicit request always gets voice, whatever the model did
    if wants_voice(message.text or ""):
        await _send_voice_reply(message, reply)
