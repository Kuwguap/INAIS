"""Free-text conversation — the main entry into the brain."""

from __future__ import annotations

import asyncio
import contextlib
import logging

from aiogram import F, Router
from aiogram.types import Message

from inais.orchestrator import loop
from inais.textutil import error_reply, split_message

log = logging.getLogger(__name__)
router = Router(name="chat")


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
