"""Button-driven menu — the front door instead of a twenty-line command list.

Every action here is read-only or self-contained, so a tap can just run it. Anything that
starts a conversation (journal, review, quiz) tells the user which command to send rather
than arming a state from under them: a button that silently changes what your next message
means is a bad surprise.

Commands still work exactly as before; this is a discovery layer on top.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from inais import db, diagnostics, journal
from inais.config import settings
from inais.textutil import split_message

log = logging.getLogger(__name__)
router = Router(name="menu")

HOME_TEXT = ("👋 I'm INAIS. Talk to me normally — text, voice notes, photos, PDFs or links.\n\n"
             "Or pick something:")

# section key -> (button label, header, [(button label, action key)])
SECTIONS: dict[str, tuple[str, str, list[tuple[str, str]]]] = {
    "study": ("📚 Study", "📚 Study", [
        ("🗂 Deck", "deck"), ("🎓 Plan", "plan"),
        ("📄 Documents", "docs"), ("🔗 Saved links", "links"),
        ("❓ Quiz me", "quiz"), ("🎤 Drill me", "drill"),
    ]),
    "money": ("💰 Money", "💰 Money", [
        ("📊 Portfolio", "finance"), ("💳 This month", "spend"),
        ("📋 Applications", "apps"), ("💸 AI spend", "usage"),
    ]),
    "plan": ("🗓 Plan", "🗓 Plan", [
        ("✅ Tasks", "tasks"), ("☀️ Today", "brief"),
        ("🍅 Focus stats", "focus"), ("📅 Week in review", "weekly"),
        ("🤝 Contacts", "contacts"),
    ]),
    "brain": ("🧠 Brain", "🧠 Brain & memory", [
        ("💡 What I learned", "learned"), ("🔭 Curiosity", "curiosity"),
        ("🕸 Network", "nn"), ("📓 Mood", "mood"),
        ("🧠 My memory", "facts"),
    ]),
    "dev": ("🐙 Dev", "🐙 Dev & mail", [
        ("👀 GitHub", "github"), ("📧 Recent mail", "mail"),
    ]),
    "system": ("⚙️ System", "⚙️ System", [
        ("📈 Status", "status"), ("🩺 Diagnostics", "diag"),
        ("🔍 Why last answer", "why"), ("⏸ Pause", "pause"),
        ("▶️ Resume", "resume"),
    ]),
}


def home_kb() -> InlineKeyboardMarkup:
    keys = list(SECTIONS)
    rows = [
        [InlineKeyboardButton(text=SECTIONS[k][0], callback_data=f"m:{k}")
         for k in keys[i:i + 3]]
        for i in range(0, len(keys), 3)
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def section_kb(key: str) -> InlineKeyboardMarkup:
    actions = SECTIONS[key][2]
    rows = [
        [InlineKeyboardButton(text=label, callback_data=f"a:{action}")
         for label, action in actions[i:i + 2]]
        for i in range(0, len(actions), 2)
    ]
    rows.append([InlineKeyboardButton(text="◀ Back", callback_data="m:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ---------- actions ----------

async def _tasks() -> str:
    p = db.pool()
    if p is None:
        return "No database configured — tasks need one."
    from inais.timeutil import fmt, now_local

    rows = await p.fetch(
        "select id, context, title, due from tasks where status = 'open'"
        " order by due nulls last, priority limit 25")
    if not rows:
        return "No open tasks 🎉"
    today = now_local().date()
    out = ["✅ Open tasks"]
    for r in rows:
        late = "⚠️ " if r["due"] and r["due"].date() < today else ""
        out.append(f"#{r['id']} [{r['context']}] {r['title']} — {late}{fmt(r['due'])}")
    return "\n".join(out)


async def _usage() -> str:
    p = db.pool()
    if p is None:
        return "No database configured — usage tracking is off."
    rows = await p.fetch(
        "select model, sum(cost_usd) as cost from llm_usage"
        " where ts >= date_trunc('month', now()) group by model order by cost desc")
    if not rows:
        return "No AI usage recorded this month yet."
    total = sum(float(r["cost"]) for r in rows)
    lines = [f"💸 This month: ${total:.2f} of ${settings().monthly_budget_usd:.0f}"]
    lines += [f"- {r['model']}: ${float(r['cost']):.2f}" for r in rows]
    return "\n".join(lines)


async def _mail() -> str:
    p = db.pool()
    if p is None:
        return "No database configured."
    rows = await p.fetch(
        "select from_, subject, importance, ts from email_events"
        " order by ts desc limit 10")
    if not rows:
        return "No triaged email yet."
    return "📧 Recent mail\n" + "\n".join(
        f"[{r['importance']}] {r['subject'][:70]} — {r['from_'][:40]}" for r in rows)


async def _finance() -> str:
    from inais.agents import finance

    if not settings().binance_enabled:
        return "Binance isn't configured (read-only key needed)."
    return await finance.build_daily_summary() or "No portfolio data yet."


async def _github() -> str:
    from inais.integrations import github

    if not settings().github_enabled:
        return "GitHub isn't configured — set GITHUB_TOKEN (read-only)."
    return github.render_digest(await github.fetch_all())


ACTIONS: dict[str, Callable[[], Awaitable[str]]] = {}


def _register_actions() -> None:
    """Wired lazily so importing this module doesn't drag in every subsystem."""
    from inais.agents import applications, expenses
    from inais.brain import autonomy, curiosity, nn
    from inais.jobs import brief, reminders, weekly
    from inais.study import links, spaced
    from inais.study import store as study_store

    ACTIONS.update({
        "tasks": _tasks,
        "usage": _usage,
        "mail": _mail,
        "finance": _finance,
        "github": _github,
        "deck": spaced.stats,
        "plan": lambda: study_store.get_plan(None),
        "docs": lambda: _docs(study_store),
        "links": lambda: _links(links),
        "spend": lambda: expenses.month_summary(0),
        "apps": lambda: _apps(applications),
        "brief": lambda: brief.build_morning_brief(),
        "focus": reminders.stats,
        "weekly": lambda: _weekly(weekly),
        "contacts": _contacts,
        "learned": lambda: autonomy.summary(5),
        "curiosity": curiosity.queue_summary,
        "nn": nn.status,
        "mood": lambda: journal.trend(14),
        "diag": diagnostics.run,
    })


async def _docs(study_store) -> str:
    docs = await study_store.list_documents()
    if not docs:
        return "No documents yet — send me a PDF or a link."
    return "📄 Documents\n" + "\n".join(
        f"#{d['id']} {d['title']} ({d['pages']} pages)" for d in docs)


async def _links(links_mod) -> str:
    return links_mod.render(await links_mod.recent())


async def _apps(applications) -> str:
    return applications.render_pipeline(await applications.pipeline())


async def _weekly(weekly_mod) -> str:
    return await weekly_mod.build_weekly_review() or "Not enough activity this week yet."


async def _contacts() -> str:
    p = db.pool()
    if p is None:
        return "No database configured."
    rows = await p.fetch(
        "select id, name, org, follow_up_at from contacts"
        " order by follow_up_at nulls last limit 20")
    if not rows:
        return "No contacts yet."
    return "🤝 Contacts\n" + "\n".join(
        f"#{r['id']} {r['name']}" + (f" ({r['org']})" if r["org"] else "")
        + (f" — follow up {r['follow_up_at']}" if r["follow_up_at"] else "")
        for r in rows)


# Actions that begin an interaction: point at the command instead of arming state silently.
CONVERSATIONAL = {
    "quiz": "Send /quiz — I'll ask a multiple-choice question from your material.",
    "drill": "Send /drill — I'll ask an interview question out loud and grade your answer.",
    "facts": "Send /facts — you can browse, correct and delete what I believe about you.",
    "why": "Send /why — I'll explain how I handled your last message.",
}


async def _safe_edit(message, text: str, markup=None) -> None:
    try:
        await message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise


@router.message(Command("menu"))
async def cmd_menu(message: Message) -> None:
    await message.answer(HOME_TEXT, reply_markup=home_kb())


@router.callback_query(F.data.startswith("m:"))
async def on_section(cb: CallbackQuery) -> None:
    key = (cb.data or "m:home").split(":", 1)[1]
    await cb.answer()
    if cb.message is None:
        return
    if key == "home" or key not in SECTIONS:
        await _safe_edit(cb.message, HOME_TEXT, home_kb())
        return
    await _safe_edit(cb.message, f"{SECTIONS[key][1]}\n\nPick one:", section_kb(key))


@router.callback_query(F.data.startswith("a:"))
async def on_action(cb: CallbackQuery) -> None:
    action = (cb.data or "a:").split(":", 1)[1]

    if action in CONVERSATIONAL:
        await cb.answer()
        if cb.message:
            await cb.message.answer(CONVERSATIONAL[action])
        return

    if action in ("pause", "resume"):
        from inais.jobs import schedules

        if action == "pause":
            await schedules.pause_jobs("via menu")
            text = "⏸ Paused — no background work until you resume. You can still talk to me."
        else:
            await schedules.resume_jobs()
            text = "▶️ Resumed — background jobs are running again."
        await cb.answer(text[:200])
        if cb.message:
            await cb.message.answer(text)
        return

    if action == "status":
        await cb.answer()
        if cb.message:
            await cb.message.answer("Send /status for the full picture.")
        return

    if not ACTIONS:
        _register_actions()
    handler = ACTIONS.get(action)
    if handler is None:
        await cb.answer("Unknown action.")
        return

    await cb.answer("Working…")
    try:
        text = await handler()
    except Exception as e:
        log.exception("menu action %s failed", action)
        text = f"⚠️ {type(e).__name__}: {str(e)[:200]}"
    if cb.message:
        for chunk in split_message(text or "(nothing to show)"):
            await cb.message.answer(chunk, disable_web_page_preview=True)
