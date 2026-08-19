"""Stopping an alarm-grade reminder: the 🛑 button, or just typing "stop".

The text path uses a guarded filter — it only claims a message when a reminder is actually
waiting, so "stop" in any other conversation still reaches the brain like normal text.
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, Message

from inais.jobs import reminders

log = logging.getLogger(__name__)
router = Router(name="alarms")


@router.callback_query(F.data.startswith("rstop:"))
async def on_stop_button(cb: CallbackQuery) -> None:
    try:
        reminder_id = int((cb.data or "rstop:0").split(":", 1)[1])
    except ValueError:
        await cb.answer("Malformed.")
        return
    row = await reminders.acknowledge(reminder_id)
    if row is None:
        await cb.answer("Already stopped.")
        return
    await cb.answer("Stopped 🛑")
    if cb.message:
        try:
            await cb.message.edit_text(f"⏰ {row['text']}\n\n✅ Stopped.")
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e):
                raise


async def _stop_meant_here(message: Message) -> bool:
    """True only when this text is a stop phrase AND a reminder is waiting for one."""
    if not reminders.is_stop_text(message.text or ""):
        return False
    return await reminders.any_awaiting_ack()


@router.message(F.text, _stop_meant_here)
async def on_stop_text(message: Message) -> None:
    row = await reminders.acknowledge_latest()
    if row is None:
        await message.answer("Nothing was ringing — all quiet.")
        return
    await message.answer(f"🛑 Stopped: {row['text']}")
    if row.get("message_id"):
        try:
            await message.bot.edit_message_text(
                f"⏰ {row['text']}\n\n✅ Stopped.",
                chat_id=message.chat.id, message_id=row["message_id"])
        except Exception:
            log.debug("could not edit original reminder message")
