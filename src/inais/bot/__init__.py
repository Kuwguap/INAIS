"""Bot assembly: dispatcher, middlewares, routers (order matters — commands before chat)."""

from __future__ import annotations

from aiogram import Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from inais.bot.middleware import DedupeMiddleware, OwnerOnlyMiddleware
from inais.bot.routers import approvals, chat, commands, voice


def create_dispatcher() -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(DedupeMiddleware())
    dp.update.outer_middleware(OwnerOnlyMiddleware())
    # approvals first (its FSM state must win over plain chat), then commands, voice, chat
    dp.include_router(approvals.router)
    dp.include_router(commands.router)
    dp.include_router(voice.router)
    dp.include_router(chat.router)
    return dp
