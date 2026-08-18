"""Photos and image files: whiteboards, problem sets, receipts, error screenshots, timetables.

Images go through the ordinary orchestrator, so memory, tools and the trace all apply — a
photo of a timetable can create tasks, a receipt can be logged against finance, a whiteboard
can be saved as facts. The only difference is that the user message carries image blocks.

Registered BEFORE the study router, whose F.document handler would otherwise swallow image
files with "I can only ingest PDFs".
"""

from __future__ import annotations

import base64
import io
import logging

from aiogram import F, Router
from aiogram.types import Message

from inais.bot.routers.chat import typing_indicator
from inais.config import settings
from inais.orchestrator import loop
from inais.textutil import split_message

log = logging.getLogger(__name__)
router = Router(name="vision")

# What the Anthropic API accepts. Anything else has to be converted, which needs a decoder
# we do not ship — better to say so than to send bytes the model will reject.
SUPPORTED = {"image/jpeg", "image/png", "image/gif", "image/webp"}
EXTENSION_TYPES = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".gif": "image/gif", ".webp": "image/webp",
}
MAX_IMAGE_BYTES = 4 * 1024 * 1024   # the API rejects images much larger once base64-encoded
DEFAULT_PROMPT = ("Describe what you see, then help with it. If it contains tasks, dates, "
                  "amounts or errors, extract them precisely.")


def is_image_document(message: Message) -> bool:
    doc = message.document
    if doc is None:
        return False
    if (doc.mime_type or "").startswith("image/"):
        return True
    name = (doc.file_name or "").lower()
    return any(name.endswith(ext) for ext in EXTENSION_TYPES)


def media_type_for(mime: str | None, filename: str | None) -> str | None:
    if mime in SUPPORTED:
        return mime
    name = (filename or "").lower()
    for ext, media in EXTENSION_TYPES.items():
        if name.endswith(ext):
            return media
    return None


def image_block(data: bytes, media_type: str) -> dict:
    """An Anthropic image content block."""
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": media_type,
            "data": base64.b64encode(data).decode("ascii"),
        },
    }


def pick_photo_size(sizes: list, max_dimension: int = 1600):
    """Telegram sends several renditions, smallest first.

    Take the largest that stays within the model's efficient range: bigger costs more tokens
    without helping, and the very largest rendition can exceed the request limit.
    """
    if not sizes:
        return None
    within = [s for s in sizes if max(s.width or 0, s.height or 0) <= max_dimension]
    return within[-1] if within else sizes[0]


async def _handle(message: Message, data: bytes, media_type: str, caption: str) -> None:
    if len(data) > MAX_IMAGE_BYTES:
        await message.answer(
            f"That image is {len(data) / 1_048_576:.1f} MB — too big for me to look at. "
            f"Send a smaller version or a screenshot of the part that matters.")
        return
    prompt = caption.strip() or DEFAULT_PROMPT
    async with typing_indicator(message.bot, message.chat.id):
        try:
            reply = await loop.handle_text(
                message.bot, message.chat.id, prompt, source="image",
                images=[image_block(data, media_type)],
            )
        except Exception:
            log.exception("vision turn failed")
            reply = "Something broke while looking at that — try again in a moment."
    for chunk in split_message(reply):
        await message.answer(chunk)


@router.message(F.photo)
async def on_photo(message: Message) -> None:
    if not settings().brain_enabled:
        await message.answer("I need ANTHROPIC_API_KEY set before I can look at images.")
        return
    size = pick_photo_size(message.photo or [])
    if size is None:
        await message.answer("I couldn't read that photo.")
        return
    buf = io.BytesIO()
    await message.bot.download(size, destination=buf)
    await _handle(message, buf.getvalue(), "image/jpeg", message.caption or "")


@router.message(F.document, is_image_document)
async def on_image_document(message: Message) -> None:
    if not settings().brain_enabled:
        await message.answer("I need ANTHROPIC_API_KEY set before I can look at images.")
        return
    doc = message.document
    media_type = media_type_for(doc.mime_type, doc.file_name)
    if media_type is None:
        await message.answer("I can read JPEG, PNG, GIF and WebP images — that format isn't one.")
        return
    if (doc.file_size or 0) > MAX_IMAGE_BYTES:
        await message.answer(
            f"That image is {(doc.file_size or 0) / 1_048_576:.1f} MB — too big. "
            f"Send a smaller version.")
        return
    buf = io.BytesIO()
    await message.bot.download(doc, destination=buf)
    await _handle(message, buf.getvalue(), media_type, message.caption or "")
