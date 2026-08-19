"""Voice notes: transcribe → brain → reply as text + native voice bubble."""

from __future__ import annotations

import io
import logging

from aiogram import F, Router
from aiogram.types import BufferedInputFile, Message

from inais.bot.routers.chat import _send_voice_reply, typing_indicator
from inais.config import settings
from inais.integrations import stt_tts
from inais.orchestrator import loop
from inais.study import review
from inais.textutil import error_reply, split_message, strip_voice_label, wants_voice

log = logging.getLogger(__name__)
router = Router(name="voice")


@router.message(F.voice)
async def on_voice(message: Message) -> None:
    if not settings().openai_api_key:
        await message.answer("Voice needs OPENAI_API_KEY configured.")
        return

    buf = io.BytesIO()
    await message.bot.download(message.voice, destination=buf)

    async with typing_indicator(message.bot, message.chat.id):
        try:
            text = await stt_tts.transcribe_voice(buf.getvalue(), message.voice.duration or 0)
        except Exception:
            log.exception("transcription failed")
            await message.answer("Couldn't understand that voice note — try again?")
            return
        if not text:
            await message.answer("I heard silence 🤫")
            return

        # A recap voice-noted right after a study-labelled focus block goes to the
        # brain-dump reviewer instead of the normal chat brain.
        try:
            session = await review.recent_study_pomodoro()
        except Exception:
            log.exception("auto-review check failed")
            session = None
        if session is not None:
            from inais.bot.routers.study import _deliver_review

            await _deliver_review(message, text, session.get("label"))
            return

        await message.answer(f"🎙 “{text}”")
        try:
            result = await loop.handle_text(message.bot, message.chat.id, text, source="voice")
        except Exception as e:
            log.exception("brain failed on voice turn")
            result = loop.TurnResult(error_reply(e))

    explicit = wants_voice(text)
    display = strip_voice_label(result.text) if explicit else result.text
    for chunk in split_message(display):
        await message.answer(chunk)

    if result.voice_sent:
        return  # the model already voiced this turn via the speak tool
    if explicit:
        # explicit request → chunk long replies and surface a TTS failure (shared with chat)
        await _send_voice_reply(message, result.text)
    elif settings().voice_replies:
        # opportunistic nicety: nobody asked, so a failure stays quiet (log only)
        try:
            ogg = await stt_tts.synthesize_voice(strip_voice_label(result.text))
            if ogg:
                await message.answer_voice(BufferedInputFile(ogg, filename="reply.ogg"))
        except Exception:
            log.exception("TTS failed — text reply already sent")
