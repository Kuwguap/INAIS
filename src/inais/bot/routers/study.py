"""Study interactions: PDF upload, quiz keyboards, study-plan ticks, brain-dump review.

Registered BEFORE the generic voice router so the /review FSM state can claim a voice note.
"""

from __future__ import annotations

import io
import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from inais.bot import keyboards
from inais.bot.routers.chat import typing_indicator
from inais.config import settings
from inais.integrations import stt_tts
from inais.study import ingest, quiz, review, store
from inais.textutil import split_message

log = logging.getLogger(__name__)
router = Router(name="study")


class StudyReview(StatesGroup):
    waiting_dump = State()


# ---------- PDF ingestion ----------

@router.message(F.document)
async def on_document(message: Message) -> None:
    doc = message.document
    name = doc.file_name or "document.pdf"
    is_pdf = (doc.mime_type == "application/pdf") or name.lower().endswith(".pdf")
    if not is_pdf:
        await message.answer("I can only ingest PDFs right now — send me a .pdf and I'll study it.")
        return
    if (doc.file_size or 0) > ingest.MAX_PDF_BYTES:
        await message.answer(
            f"That file is {(doc.file_size or 0) / 1_048_576:.1f} MB. Telegram only lets bots "
            f"download up to 20 MB — split it or send the chapter you need.")
        return
    if not settings().db_enabled:
        await message.answer("I need a database (SUPABASE_DB_URL) to store documents.")
        return

    note = await message.answer(f"📥 Reading {name}…")
    buf = io.BytesIO()
    try:
        await message.bot.download(doc, destination=buf)
        result = await ingest.ingest_pdf(buf.getvalue(), name)
    except ingest.PdfTooLarge as e:
        await note.edit_text(f"That PDF is too large ({e}). Max 20 MB.")
        return
    except ingest.PdfUnreadable as e:
        await note.edit_text(f"I couldn't extract text from that PDF: {e}")
        return
    except Exception:
        log.exception("pdf ingestion failed")
        await note.edit_text("Ingestion failed — check the logs.")
        return

    await note.edit_text(
        f"📚 Ingested “{result['title']}”: {result['pages']} pages, {result['chunks']} chunks.\n"
        f"Ask me anything from it, say “quiz me on …”, or “read … to me”.")


# ---------- quizzes ----------

@router.message(Command("quiz"))
async def cmd_quiz(message: Message) -> None:
    topic = (message.text or "").partition(" ")[2].strip() or None
    item = await store.due_quiz_item(topic)
    if item is None:
        if topic:
            await message.answer(
                f"No questions on “{topic}” yet. Say “generate a quiz on {topic}” and I'll "
                f"build some from your material.")
        else:
            await message.answer("No quiz questions stored yet — ask me to generate some from "
                                 "a PDF you've sent.")
        return
    await _ask_quiz(message, item)


async def _ask_quiz(message: Message, item: dict) -> None:
    choices = quiz.parse_choices(item["choices"])
    if not choices:
        await message.answer(f"❓ {item['question']}\n\n(Open question — reply and I'll check it.)")
        return
    header = f"❓ {item['question']}"
    if item.get("topic"):
        header = f"[{item['topic']}] {header}"
    await message.answer(header, reply_markup=keyboards.quiz_kb(item["id"], choices))


@router.callback_query(F.data.startswith("qz:"))
async def on_quiz_answer(cb: CallbackQuery) -> None:
    try:
        _, item_id_s, index_s = (cb.data or "").split(":", 2)
        item_id, index = int(item_id_s), int(index_s)
    except ValueError:
        await cb.answer("Malformed answer.")
        return
    item = await store.get_quiz_item(item_id)
    if item is None:
        await cb.answer("That question is gone.")
        return
    choices = quiz.parse_choices(item["choices"])
    chosen = choices[index] if 0 <= index < len(choices) else ""
    correct = chosen == item["answer"]
    interval = await store.record_quiz_answer(item_id, correct)
    await cb.answer("✅ Correct" if correct else "❌ Not quite")
    verdict = ("✅ Correct." if correct
               else f"❌ You picked: {chosen}\n✅ Answer: {item['answer']}")
    if cb.message:
        await cb.message.edit_text(
            f"❓ {item['question']}\n\n{verdict}\n\nNext review in {interval} day"
            f"{'s' if interval != 1 else ''}. /quiz for another.")


# ---------- study plan tick ----------

@router.callback_query(F.data.startswith("spd:"))
async def on_plan_done(cb: CallbackQuery) -> None:
    try:
        plan_id = int((cb.data or "spd:0").split(":", 1)[1])
    except ValueError:
        await cb.answer("Malformed.")
        return
    ok = await store.mark_plan_done(plan_id)
    await cb.answer("Nice work 🎉" if ok else "Already done.")
    if cb.message:
        base = cb.message.text or "Study focus"
        await cb.message.edit_text(f"{base}\n\n✅ Marked done.")


# ---------- brain-dump review ----------

@router.message(Command("review"))
async def cmd_review(message: Message, state: FSMContext) -> None:
    hint = (message.text or "").partition(" ")[2].strip()
    await state.set_state(StudyReview.waiting_dump)
    await state.update_data(hint=hint)
    target = f" on {hint}" if hint else ""
    await message.answer(
        f"🎙 Go ahead — voice-note (or type) everything you remember{target}. "
        f"I'll check it against your material.\n(/cancel to stop.)")


@router.message(StudyReview.waiting_dump, F.text == "/cancel")
async def on_review_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Review cancelled.")


@router.message(StudyReview.waiting_dump, F.voice)
async def on_review_voice(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    await state.clear()
    buf = io.BytesIO()
    await message.bot.download(message.voice, destination=buf)
    async with typing_indicator(message.bot, message.chat.id):
        try:
            transcript = await stt_tts.transcribe_voice(buf.getvalue(),
                                                        message.voice.duration or 0)
        except Exception:
            log.exception("review transcription failed")
            await message.answer("Couldn't transcribe that — try again?")
            return
        await _deliver_review(message, transcript, data.get("hint"))


@router.message(StudyReview.waiting_dump, F.text)
async def on_review_text(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    await state.clear()
    async with typing_indicator(message.bot, message.chat.id):
        await _deliver_review(message, message.text or "", data.get("hint"))


async def _deliver_review(message: Message, transcript: str, hint: str | None) -> None:
    """Shared by the FSM paths and the automatic post-pomodoro path."""
    if not transcript.strip():
        await message.answer("There was nothing to review in that.")
        return
    await message.answer(f"🎙 “{transcript[:600]}”")
    try:
        feedback, _ = await review.run_review(transcript, hint)
    except Exception:
        log.exception("study review failed")
        await message.answer("The review failed — check the logs.")
        return
    for chunk in split_message(feedback):
        await message.answer(chunk)
    if settings().voice_replies:
        try:
            ogg = await stt_tts.synthesize_voice(feedback)
            if ogg:
                await message.answer_voice(BufferedInputFile(ogg, filename="review.ogg"))
        except Exception:
            log.exception("review TTS failed — text already sent")
