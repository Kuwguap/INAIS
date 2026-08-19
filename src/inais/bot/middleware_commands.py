"""A slash command always escapes an open FSM flow.

Without this, typing /learn while the journal (or a drill, review, draft edit…) is waiting
for input gets swallowed by that flow's F.text handler — the command silently becomes a
journal entry. A command means "leave what I'm doing", so any active state is cleared before
routing continues.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import Message

log = logging.getLogger(__name__)


class CommandEscapesStateMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[Message, dict[str, Any]], Awaitable[Any]],
        event: Message,
        data: dict[str, Any],
    ) -> Any:
        text = (getattr(event, "text", None) or "").strip()
        state = data.get("state")
        if text.startswith("/") and state is not None:
            try:
                if await state.get_state() is not None:
                    await state.clear()
            except Exception:  # never let state cleanup block a command
                log.exception("failed to clear FSM state for a command")
        return await handler(event, data)
