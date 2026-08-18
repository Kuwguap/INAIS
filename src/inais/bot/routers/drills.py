"""Spoken interview/viva drills and the daily spaced-repetition card.

Both live here because they share the same shape: ask something, take an answer, adjust what
comes next. The drill FSM must claim voice notes before the generic voice router, so this
router is registered ahead of it.
"""

from __future__ import annotations

import io
import logging

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from inais.bot import keyboards
from inais.bot.routers.chat import typing_indicator
from inais.config import settings
from inais.integrations import stt_tts
from inais.study import drills, spaced
from inais.textutil import split_message

log = logging.getLogger(__name__)
router = Router(name="drills")


class Drill(StatesGroup):
    waiting_answer = State()


async def _safe_edit(message, text: str, markup=None) -> None:
    try:
        await message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise


# ---------- spaced repetition cards ----------

async def send_card(bot, chat_id: int, topic: str | None = None) -> bool:
    """Send one due card. Returns False when nothing is due."""
    card = await spaced.due_card(topic)
    if card is None:
        return False
    await bot.send_message(
        chat_id, f"🗂 Review\n\n{card['front']}",
        reply_markup=keyboards.review_card_kb(card["id"]))
    return True


@router.message(Command("card"))
async def cmd_card(message: Message) -> None:
    topic = (message.text or "").partition(" ")[2].strip() or None
    if not await send_card(message.bot, message.chat.id, topic):
        await message.answer(await spaced.stats())


@router.message(Command("deck"))
async def cmd_deck(message: Message) -> None:
    await message.answer(await spaced.stats())


@router.callback_query(F.data.startswith("cardshow:"))
async def on_card_show(cb: CallbackQuery) -> None:
    try:
        card_id = int((cb.data or "cardshow:0").split(":", 1)[1])
    except ValueError:
        await cb.answer("Malformed.")
        return
    card = await spaced.get_card(card_id)
    if card is None:
        await cb.answer("That card is gone.")
        return
    await cb.answer()
    if cb.message:
        await _safe_edit(cb.message,
                         f"🗂 Review\n\n{card['front']}\n\n— — —\n{card['back']}\n\nHow did it go?",
                         keyboards.review_grade_kb(card_id))


@router.callback_query(F.data.startswith(("cardok:", "cardno:")))
async def on_card_graded(cb: CallbackQuery) -> None:
    data = cb.data or ""
    correct = data.startswith("cardok:")
    try:
        card_id = int(data.split(":", 1)[1])
    except ValueError:
        await cb.answer("Malformed.")
        return
    card = await spaced.get_card(card_id)
    if card is None:
        await cb.answer("That card is gone.")
        return
    interval = await spaced.record_answer(card_id, correct)
    await cb.answer("✅ Nice" if correct else "Noted — you'll see it sooner")
    if cb.message:
        verdict = "✅ Got it" if correct else "❌ Missed"
        await _safe_edit(
            cb.message,
            f"🗂 {card['front']}\n\n{card['back']}\n\n{verdict} — next review in {interval} "
            f"day{'s' if interval != 1 else ''}.")
    # keep the session going while there is more due
    if cb.message and not await send_card(cb.bot, cb.message.chat.id):
        await cb.message.answer("That's everything due today 🎉")


# ---------- interview / viva drills ----------

async def _ask(bot, chat_id: int, state: FSMContext, category: str | None,
               exam: str | None) -> bool:
    question = await drills.pick(category, exam)
    if question is None:
        await bot.send_message(
            chat_id,
            "No questions in the bank yet. Ask me to \"generate viva questions on <topic>\" "
            "or \"make behavioral interview questions\" and I'll build some.")
        await state.clear()
        return False

    await drills.mark_asked(question["id"])
    await state.set_state(Drill.waiting_answer)
    await state.update_data(question_id=question["id"], category=category, exam=exam)

    label = {"behavioral": "🎤 Behavioral", "technical": "🎤 Technical",
             "viva": "🎤 Viva"}.get(question["category"], "🎤 Drill")
    await bot.send_message(
        chat_id, f"{label}\n\n{question['question']}\n\nAnswer out loud — send me a voice note.",
        reply_markup=keyboards.drill_kb(question["id"]))

    # ask it aloud too: rehearsing against a spoken question is the point
    try:
        ogg = await stt_tts.synthesize_voice(question["question"])
        if ogg:
            await bot.send_voice(chat_id, BufferedInputFile(ogg, filename="question.ogg"))
    except Exception:
        log.exception("drill TTS failed — question already sent as text")
    return True


@router.message(Command("drill"))
async def cmd_drill(message: Message, state: FSMContext) -> None:
    arg = (message.text or "").partition(" ")[2].strip().lower()
    category = arg if arg in drills.CATEGORIES else None
    exam = None if (category or not arg) else arg
    await _ask(message.bot, message.chat.id, state, category, exam)


@router.callback_query(F.data == "drillnext")
async def on_drill_next(cb: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    await cb.answer("Next one…")
    if cb.message:
        await _ask(cb.bot, cb.message.chat.id, state,
                   data.get("category"), data.get("exam"))


@router.callback_query(F.data == "drillstop")
async def on_drill_stop(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await cb.answer("Stopped")
    if cb.message:
        await cb.message.answer(await drills.stats())


@router.message(Drill.waiting_answer, F.text == "/cancel")
async def on_drill_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Drill stopped.")


@router.message(Drill.waiting_answer, F.voice)
async def on_drill_voice(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    buf = io.BytesIO()
    await message.bot.download(message.voice, destination=buf)
    async with typing_indicator(message.bot, message.chat.id):
        try:
            transcript = await stt_tts.transcribe_voice(buf.getvalue(),
                                                        message.voice.duration or 0)
        except Exception:
            log.exception("drill transcription failed")
            await message.answer("Couldn't transcribe that — try again?")
            return
        await _grade_and_reply(message, state, data, transcript)


@router.message(Drill.waiting_answer, F.text)
async def on_drill_text(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    async with typing_indicator(message.bot, message.chat.id):
        await _grade_and_reply(message, state, data, message.text or "")


async def _grade_and_reply(message: Message, state: FSMContext, data: dict,
                           transcript: str) -> None:
    question = await drills.get_question(int(data.get("question_id", 0)))
    if question is None:
        await state.clear()
        await message.answer("Lost track of that question — run /drill again.")
        return
    await message.answer(f"🎙 “{transcript[:600]}”")
    try:
        feedback, _ = await drills.grade(question, transcript)
    except Exception:
        log.exception("drill grading failed")
        await message.answer("Grading failed — check the logs.")
        return
    for chunk in split_message(feedback):
        await message.answer(chunk)
    if settings().voice_replies:
        try:
            ogg = await stt_tts.synthesize_voice(feedback)
            if ogg:
                await message.answer_voice(BufferedInputFile(ogg, filename="feedback.ogg"))
        except Exception:
            log.exception("drill feedback TTS failed — text already sent")
    await message.answer("Another?", reply_markup=keyboards.drill_kb(question["id"]))
