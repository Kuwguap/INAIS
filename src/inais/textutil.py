"""Text helpers shared across the bot."""

from __future__ import annotations

TELEGRAM_LIMIT = 4096


def split_message(text: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
    """Split text into Telegram-sized chunks, preferring paragraph then line boundaries."""
    text = text.strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        window = remaining[:limit]
        cut = window.rfind("\n\n")
        if cut < limit // 2:
            cut = window.rfind("\n")
        if cut < limit // 2:
            cut = window.rfind(" ")
        if cut <= 0:
            cut = limit
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks


def error_reply(exc: Exception) -> str:
    """What the owner sees when a turn fails.

    This is a single-user assistant and the reader is the person who can fix it, so the
    provider's own message goes straight through — a wrong model id, a rejected key and an
    exhausted quota are indistinguishable from "something broke".
    """
    detail = str(exc).replace("\n", " ").strip()
    return (f"⚠️ That turn failed.\n\n{type(exc).__name__}: {detail[:400]}\n\n"
            f"Run /diag to test each provider, or /why for the full trace of this turn.")
