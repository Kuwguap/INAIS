"""Voice: Telegram OGG/Opus in → transcription; reply text → OGG/Opus voice bubble out.

ffmpeg does both conversions (gpt-4o-mini-transcribe doesn't accept ogg; Telegram's
sendVoice only renders a native voice bubble for OGG-encoded-with-Opus audio).
"""

from __future__ import annotations

import asyncio
import logging

from inais import llm
from inais.config import settings

log = logging.getLogger(__name__)

TTS_MAX_CHARS = 2000  # bound TTS cost; longer replies go out as text only


class FfmpegError(RuntimeError):
    pass


async def _ffmpeg(input_bytes: bytes, out_args: list[str]) -> bytes:
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", "pipe:0", *out_args, "pipe:1",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate(input_bytes)
    if proc.returncode != 0 or not out:
        raise FfmpegError(err.decode(errors="replace")[:500])
    return out


async def transcribe_voice(oga_bytes: bytes, duration_seconds: int = 0) -> str:
    """Telegram voice note (.oga) → text."""
    mp3 = await _ffmpeg(oga_bytes, ["-f", "mp3"])
    resp = await llm.openai_client().audio.transcriptions.create(
        model=settings().stt_model, file=("voice.mp3", mp3),
    )
    # rough cost: ~$0.003/min for gpt-4o-mini-transcribe
    await llm.record_usage("openai", settings().stt_model, "stt", 0, 0,
                           cost_usd=(duration_seconds / 60) * 0.003)
    return (resp.text or "").strip()


async def synthesize_voice(text: str) -> bytes | None:
    """Reply text → OGG/Opus bytes for sendVoice. None if text is too long to voice."""
    text = text.strip()
    if not text or len(text) > TTS_MAX_CHARS:
        return None
    cfg = settings()
    resp = await llm.openai_client().audio.speech.create(
        model=cfg.tts_model, voice=cfg.tts_voice, input=text, response_format="opus",
    )
    data = getattr(resp, "content", None)
    if data is None:  # some SDK versions stream the body
        data = await resp.aread()
    # re-mux/encode to a Telegram-native voice container
    ogg = await _ffmpeg(data, ["-c:a", "libopus", "-b:a", "48k", "-application", "voip", "-f", "ogg"])
    await llm.record_usage("openai", cfg.tts_model, "tts", 0, 0,
                           cost_usd=(len(text) / 1000) * 0.015)
    return ogg
