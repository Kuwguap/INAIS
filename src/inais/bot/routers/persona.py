"""/persona — tune how INAIS carries itself (tone, brevity, humour) at runtime.

The choice is persisted in persona_controls and injected into the persona block every turn
by inais.persona. This router only reads/writes the knobs; it holds no FSM state, so it can
sit among the plain command routers.
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from inais import db, persona
from inais.bot.keyboards import persona_kb

log = logging.getLogger(__name__)
router = Router(name="persona")


def _summary(knobs: dict) -> str:
    return (
        "🎭 How I carry myself\n\n"
        f"Tone: {knobs.get('tone')}\n"
        f"Length: {knobs.get('brevity')}\n"
        f"Humour: {knobs.get('humour')}\n\n"
        "Tap to change. It sticks until you change it again."
    )


@router.message(Command("persona"))
async def cmd_persona(message: Message) -> None:
    knobs = persona.current_knobs()
    note = "" if db.pool() is not None else (
        "\n\n⚠️ No database — changes won't persist across a restart.")
    await message.answer(_summary(knobs) + note, reply_markup=persona_kb(knobs))


@router.callback_query(F.data.startswith("psona:"))
async def on_persona_set(cb: CallbackQuery) -> None:
    try:
        _, field, value = cb.data.split(":", 2)
    except ValueError:
        await cb.answer("Didn't catch that.")
        return

    ok = await persona.set_knob(field, value)
    if not ok:
        await cb.answer(
            "Couldn't save that (no database?)." if db.pool() is None else "Invalid choice.",
            show_alert=db.pool() is None)
        return

    await cb.answer(f"{field} → {value}")
    if cb.message is None:
        return
    knobs = persona.current_knobs()
    try:
        await cb.message.edit_text(_summary(knobs), reply_markup=persona_kb(knobs))
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise
