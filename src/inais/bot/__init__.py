"""Bot assembly: dispatcher, middlewares, routers (order matters — commands before chat)."""

from __future__ import annotations

from aiogram import Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from inais.bot.middleware import DedupeMiddleware, OwnerOnlyMiddleware
from inais.bot.middleware_commands import CommandEscapesStateMiddleware
from inais.bot.routers import (
    alarms, approvals, capture, chat, commands, drills, facts, learning, menu,
    study, tracking, vision, voice,
)


def create_dispatcher() -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(DedupeMiddleware())
    dp.update.outer_middleware(OwnerOnlyMiddleware())
    dp.message.middleware(CommandEscapesStateMiddleware())  # commands escape FSM flows
    # FSM-owning routers first (their states must win over plain chat), then commands,
    # then study (documents/quizzes), learning feedback, voice, and finally free chat.
    dp.include_router(alarms.router)   # 'stop' must beat the brain when an alarm rings
    dp.include_router(approvals.router)
    dp.include_router(vision.router)   # before study: study's F.document catches everything
    dp.include_router(capture.router)  # link + journal capture, before chat/voice
    dp.include_router(drills.router)   # before voice: its FSM claims voice answers
    dp.include_router(study.router)
    dp.include_router(facts.router)
    dp.include_router(tracking.router)
    dp.include_router(commands.router)
    dp.include_router(menu.router)
    dp.include_router(learning.router)
    dp.include_router(voice.router)
    dp.include_router(chat.router)
    return dp
