"""Update-level middlewares: single-user allowlist + webhook-redelivery dedupe."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Update

from inais import db
from inais.config import settings

log = logging.getLogger(__name__)


class OwnerOnlyMiddleware(BaseMiddleware):
    """SECURITY INVARIANT #1: drop every update not from the owner."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user is None or user.id != settings().owner_telegram_id:
            if user is not None:
                log.warning("dropped update from non-owner user id=%s", user.id)
            return None
        return await handler(event, data)


class DedupeMiddleware(BaseMiddleware):
    """Telegram redelivers updates that weren't ACKed fast enough — process each once."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        p = db.pool()
        if p is not None and isinstance(event, Update):
            try:
                row = await p.fetchrow(
                    "insert into processed_updates (update_id) values ($1)"
                    " on conflict (update_id) do nothing returning update_id",
                    event.update_id,
                )
            except Exception:
                log.exception("dedupe check failed — processing anyway")
                row = True
            if row is None:
                log.info("skipping duplicate update %s", event.update_id)
                return None
        return await handler(event, data)
