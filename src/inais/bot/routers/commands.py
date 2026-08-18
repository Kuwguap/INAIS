"""Slash commands."""

from __future__ import annotations

import logging

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from inais import db
from inais.agents import finance
from inais.config import settings
from inais.memory import reflection, store
from inais.orchestrator import loop
from inais.textutil import split_message

log = logging.getLogger(__name__)
router = Router(name="commands")

HELP = """I'm INAIS — your personal assistant.

Just talk to me (text or voice notes). I can:
• 📧 watch your Gmail, flag important mail, and draft replies you approve
• 💰 track your Binance portfolio (read-only) — try /finance
• 📚 help with study, code, and everything else
• 🧠 remember things long-term — say "remember that ..."

Commands:
/finance — portfolio snapshot
/usage — this month's AI spend
/reflect — consolidate memory now (normally runs nightly)
/reset — start a fresh conversation
/help — this message"""


@router.message(Command("start", "help"))
async def cmd_help(message: Message) -> None:
    await message.answer(HELP)


@router.message(Command("reset"))
async def cmd_reset(message: Message) -> None:
    loop.reset_session(message.chat.id)
    await store.mark_reset(message.chat.id)
    await message.answer("🔄 Fresh start — conversation context cleared.")


@router.message(Command("reflect"))
async def cmd_reflect(message: Message) -> None:
    await message.answer("🧠 Reflecting on recent conversations…")
    try:
        result = await reflection.run_reflection()
    except Exception:
        log.exception("manual reflection failed")
        result = "Reflection failed — check the logs."
    await message.answer(result)


@router.message(Command("finance"))
async def cmd_finance(message: Message) -> None:
    if not settings().binance_enabled:
        await message.answer("Binance isn't configured yet — set BINANCE_API_KEY and "
                             "BINANCE_API_SECRET (read-only key!).")
        return
    try:
        summary = await finance.build_daily_summary()
        for chunk in split_message(summary or "No portfolio data yet."):
            await message.answer(chunk)
    except Exception:
        log.exception("/finance failed")
        await message.answer("Couldn't reach Binance — check the logs.")


@router.message(Command("usage"))
async def cmd_usage(message: Message) -> None:
    p = db.pool()
    if p is None:
        await message.answer("No database configured — usage tracking is off.")
        return
    rows = await p.fetch(
        "select model, sum(input_tokens) as inp, sum(output_tokens) as outp, sum(cost_usd) as cost"
        " from llm_usage where ts >= date_trunc('month', now()) group by model order by cost desc",
    )
    if not rows:
        await message.answer("No usage recorded this month yet.")
        return
    total = sum(float(r["cost"]) for r in rows)
    budget = settings().monthly_budget_usd
    lines = [f"💸 This month: ${total:.2f} of ${budget:.0f} budget"]
    for r in rows:
        lines.append(f"- {r['model']}: ${float(r['cost']):.2f} ({r['inp']} in / {r['outp']} out tokens)")
    await message.answer("\n".join(lines))
