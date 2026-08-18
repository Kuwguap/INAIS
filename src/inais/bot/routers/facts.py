"""Browsing and correcting semantic memory.

A wrong fact is worse than a missing one: it is retrieved silently into every future prompt
and the assistant states it with confidence. This gives the user direct control — see what is
stored, delete it, or replace it with the correct version.

Corrections use supersede (not overwrite) so the old statement stays visible in the audit
trail with a pointer to what replaced it; deletes are soft for the same reason.
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from inais import db
from inais.bot import keyboards
from inais.memory import store

log = logging.getLogger(__name__)
router = Router(name="facts")

PAGE_SIZE = 5


class FixFact(StatesGroup):
    waiting_statement = State()


def _render_page(facts: list[dict], offset: int, total: int) -> str:
    if not facts:
        return ("🧠 I haven't stored any facts about you yet.\n"
                "They accumulate as we talk, or say \"remember that …\".")
    lines = [f"🧠 Memory — {total} fact{'s' if total != 1 else ''} stored"]
    for f in facts:
        conf = float(f["confidence"])
        lines.append(f"\n#{f['id']} [{f['category']}] (confidence {conf:.2f})\n{f['statement']}")
    lines.append("\n🗑 removes a fact · ✏️ replaces it with a correction")
    return "\n".join(lines)


async def _show(target: Message, offset: int, edit: bool = False) -> None:
    if not db.pool():
        await target.answer("No database configured — memory is off.")
        return
    total = await store.count_facts()
    offset = max(0, min(offset, max(0, total - 1)))
    facts = await store.page_facts(offset, PAGE_SIZE)
    text = _render_page(facts, offset, total)
    markup = keyboards.facts_kb(facts, offset, total, PAGE_SIZE) if facts else None
    if not edit:
        await target.answer(text, reply_markup=markup)
        return
    try:
        await target.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as e:
        # re-rendering identical content (double-tapped page, or deleting an already-gone
        # fact) is not an error worth surfacing to the user
        if "message is not modified" not in str(e):
            raise


@router.message(Command("facts"))
async def cmd_facts(message: Message) -> None:
    await _show(message, offset=0)


@router.callback_query(F.data.startswith("fpg:"))
async def on_page(cb: CallbackQuery) -> None:
    try:
        offset = int((cb.data or "fpg:0").split(":", 1)[1])
    except ValueError:
        await cb.answer("Malformed.")
        return
    await cb.answer()
    if cb.message:
        await _show(cb.message, offset, edit=True)


@router.callback_query(F.data == "fnop")
async def on_noop(cb: CallbackQuery) -> None:
    await cb.answer()


@router.callback_query(F.data.startswith("fdel:"))
async def on_delete(cb: CallbackQuery) -> None:
    parts = (cb.data or "fdel:0:0").split(":")
    try:
        fact_id, offset = int(parts[1]), int(parts[2]) if len(parts) > 2 else 0
    except ValueError:
        await cb.answer("Malformed.")
        return
    statement = await store.forget_fact(fact_id)
    if statement is None:
        await cb.answer("Already gone.")
    else:
        await cb.answer(f"Forgotten: {statement[:60]}", show_alert=True)
        log.info("user forgot fact %s", fact_id)
    if cb.message:
        await _show(cb.message, offset, edit=True)


@router.callback_query(F.data.startswith("fsup:"))
async def on_supersede(cb: CallbackQuery, state: FSMContext) -> None:
    try:
        fact_id = int((cb.data or "fsup:0").split(":", 1)[1])
    except ValueError:
        await cb.answer("Malformed.")
        return
    fact = await store.get_fact(fact_id)
    if fact is None or fact["deleted_at"] is not None or fact["superseded_by"] is not None:
        await cb.answer("That fact is no longer active.")
        return
    await state.set_state(FixFact.waiting_statement)
    await state.update_data(fact_id=fact_id, category=fact["category"])
    await cb.answer()
    if cb.message:
        await cb.message.answer(
            f"✏️ Current #{fact_id}:\n{fact['statement']}\n\n"
            f"Send me the corrected version (/cancel to keep it).")


@router.message(FixFact.waiting_statement, F.text == "/cancel")
async def on_fix_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Left it unchanged.")


@router.message(FixFact.waiting_statement, F.text)
async def on_fix_statement(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    await state.clear()
    fact_id = int(data["fact_id"])
    replacement = (message.text or "").strip()
    if not replacement:
        await message.answer("Empty — left it unchanged.")
        return
    try:
        new_id = await store.supersede_fact(
            fact_id, replacement, category=data.get("category", "general"), confidence=0.95)
    except Exception:
        log.exception("superseding fact %s failed", fact_id)
        await message.answer("Couldn't save the correction — check the logs.")
        return
    if new_id is None:
        await message.answer("Couldn't save the correction (no database).")
        return
    await message.answer(
        f"✅ #{fact_id} replaced by #{new_id}:\n{replacement}\n\n"
        f"The old version stays in the audit trail but is no longer retrieved.")


@router.message(Command("forget"))
async def cmd_forget(message: Message) -> None:
    arg = (message.text or "").partition(" ")[2].strip().lstrip("#")
    if not arg.isdigit():
        await message.answer("Usage: /forget <fact id> — see /facts for ids.")
        return
    statement = await store.forget_fact(int(arg))
    if statement is None:
        await message.answer(f"No active fact #{arg} (already forgotten, or wrong id).")
        return
    log.info("user forgot fact %s via command", arg)
    await message.answer(f"🗑 Forgotten #{arg}:\n{statement}")
