"""Slash commands."""

from __future__ import annotations

import logging

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from inais import db
from inais.agents import finance
from inais.brain import autonomy, curiosity, nn
from inais.config import settings
from inais.jobs import brief, reminders
from inais.memory import reflection, store
from inais.orchestrator import loop
from inais.study import store as study_store
from inais.textutil import split_message

log = logging.getLogger(__name__)
router = Router(name="commands")

HELP = """I'm INAIS — your personal assistant.

Just talk to me (text or voice notes). I can:
• 📧 watch your Gmail, flag important mail, and draft replies you approve
• 💰 track your Binance portfolio (read-only) — try /finance
• 🗓 hold your tasks, deadlines and reminders — "remind me in 20 min to submit"
• 📚 study with you: send me a PDF, then ask questions, /quiz, or voice-note a recap
• 🧠 remember things long-term, and teach myself while you're away

Commands
/tasks — open tasks · /brief — today's brief
/pomodoro [min] [label] — focus timer · /pomodoro stop · /stats
/quiz [topic] — spaced-repetition questions
/review [topic] — brain-dump: recap out loud, I check it
/finance — portfolio snapshot
/learned — what I taught myself · /learn — learn something now
/brain — neural-network status · /train — retrain it now
/usage — this month's AI spend · /reflect — consolidate memory now
/reset — fresh conversation · /help — this message"""


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


# ---------- planner ----------

@router.message(Command("tasks"))
async def cmd_tasks(message: Message) -> None:
    p = db.pool()
    if p is None:
        await message.answer("No database configured — tasks need one.")
        return
    rows = await p.fetch(
        "select id, context, title, due, priority from tasks where status = 'open'"
        " order by due nulls last, priority limit 30",
    )
    if not rows:
        await message.answer("No open tasks 🎉 — tell me about something and I'll track it.")
        return
    from inais.timeutil import fmt, now_local

    today = now_local().date()
    lines = ["✅ Open tasks"]
    for r in rows:
        overdue = r["due"] is not None and r["due"].date() < today
        lines.append(f"#{r['id']} [{r['context']}] {r['title']} — "
                     f"{'⚠️ ' if overdue else ''}{fmt(r['due'])}")
    for chunk in split_message("\n".join(lines)):
        await message.answer(chunk)


@router.message(Command("brief"))
async def cmd_brief(message: Message) -> None:
    text = await brief.build_morning_brief()
    for chunk in split_message(text or "Nothing to brief on yet."):
        await message.answer(chunk)


@router.message(Command("pomodoro"))
async def cmd_pomodoro(message: Message) -> None:
    arg = (message.text or "").partition(" ")[2].strip()
    if arg.lower().startswith("stop"):
        label = await reminders.stop_pomodoro()
        await message.answer(f"🍅 Stopped ({label})." if label else "No focus session running.")
        return
    parts = arg.split(maxsplit=1)
    minutes = 25
    label = None
    if parts and parts[0].isdigit():
        minutes = max(1, min(int(parts[0]), 180))
        label = parts[1].strip() if len(parts) > 1 else None
    elif arg:
        label = arg
    started = await reminders.start_pomodoro(minutes, label)
    if started is None:
        await message.answer("No database configured — the timer needs one.")
        return
    _, ends_at = started
    from inais.timeutil import fmt

    suffix = f" — {label}" if label else ""
    await message.answer(
        f"🍅 Focus started{suffix}: {minutes} min, until {fmt(ends_at, with_date=False)}.\n"
        f"I'll ping you when it's done. /pomodoro stop to cancel.")


@router.message(Command("stats"))
async def cmd_stats(message: Message) -> None:
    await message.answer(await reminders.stats())


# ---------- the growing brain ----------

@router.message(Command("learned"))
async def cmd_learned(message: Message) -> None:
    try:
        text = await autonomy.summary(limit=5)
    except Exception:
        log.exception("/learned failed")
        text = "Couldn't read my knowledge base — check the logs."
    for chunk in split_message(text):
        await message.answer(chunk)


@router.message(Command("learn"))
async def cmd_learn(message: Message) -> None:
    if not settings().learning_enabled:
        await message.answer("Autonomous learning is off. Set LEARNING_ENABLED=true to let me "
                             "research things on my own while you're away.")
        return
    await message.answer("🔎 Going off to learn something…")
    try:
        result = await autonomy.run_cycle(message.bot, force=True)
    except Exception:
        log.exception("manual learning cycle failed")
        result = "The learning cycle failed — check the logs."
    await message.answer(result + "\n\nUse /learned to read the notes.")


@router.message(Command("curiosity"))
async def cmd_curiosity(message: Message) -> None:
    await message.answer("🔭 What I want to learn next:\n" + await curiosity.queue_summary())


@router.message(Command("brain"))
async def cmd_brain(message: Message) -> None:
    try:
        status = await nn.status()
    except Exception:
        log.exception("/brain failed")
        status = "Couldn't read the network status."
    await message.answer(status)


@router.message(Command("train"))
async def cmd_train(message: Message) -> None:
    if not settings().nn_enabled:
        await message.answer("The neural network is disabled (NN_ENABLED=false).")
        return
    await message.answer("🏋️ Training on your behaviour signals…")
    try:
        result = await nn.train_all()
    except Exception:
        log.exception("manual training failed")
        result = "Training failed — check the logs."
    await message.answer(result)


# ---------- study ----------

@router.message(Command("plan"))
async def cmd_plan(message: Message) -> None:
    exam = (message.text or "").partition(" ")[2].strip() or None
    text = await study_store.get_plan(exam)
    for chunk in split_message(text):
        await message.answer(chunk)


@router.message(Command("docs"))
async def cmd_docs(message: Message) -> None:
    docs = await study_store.list_documents()
    if not docs:
        await message.answer("No documents yet — send me a PDF and I'll ingest it.")
        return
    lines = ["📚 Ingested documents"]
    lines += [f"#{d['id']} {d['title']} ({d['pages']} pages)" for d in docs]
    await message.answer("\n".join(lines))
