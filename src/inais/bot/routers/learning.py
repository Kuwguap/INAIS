"""Feedback taps on self-learned knowledge — the labels that train the interest network."""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.types import CallbackQuery

from inais.brain import signals

log = logging.getLogger(__name__)
router = Router(name="learning")


async def _feedback(cb: CallbackQuery, positive: bool) -> None:
    try:
        knowledge_id = int((cb.data or "x:0").split(":", 1)[1])
    except ValueError:
        await cb.answer("Malformed.")
        return
    try:
        await signals.record_knowledge_feedback(knowledge_id, positive)
    except Exception:
        log.exception("knowledge feedback failed")
        await cb.answer("Couldn't record that.", show_alert=True)
        return
    await cb.answer("Noted — I'll chase more of this 👍" if positive else "Noted — less of this 👎")
    if cb.message:
        suffix = "👍 more like this" if positive else "👎 not useful"
        await cb.message.edit_text(f"{cb.message.text}\n\n({suffix} — logged for training)")


@router.callback_query(F.data.startswith("kup:"))
async def on_up(cb: CallbackQuery) -> None:
    await _feedback(cb, True)


@router.callback_query(F.data.startswith("kdn:"))
async def on_down(cb: CallbackQuery) -> None:
    await _feedback(cb, False)
