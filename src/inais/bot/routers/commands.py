"""Slash commands."""

from __future__ import annotations

import logging

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from inais import controls, db, trace
from inais.agents import finance
from inais.brain import autonomy, curiosity, nn
from inais.config import settings
from inais.integrations import github
from inais.jobs import brief, reminders, schedules
from inais.memory import reflection, store
from inais.orchestrator import loop
from inais.study import store as study_store
from inais.textutil import split_message

log = logging.getLogger(__name__)
router = Router(name="commands")

HELP = """👋 I'm INAIS — your assistant.

Just talk to me. I understand text, voice notes, photos, PDFs and links.

Tap a category below, or send /commands for the full list."""

ALL_COMMANDS = """All commands

Study — /quiz /card /deck /drill /review /plan /docs /links
Money — /finance /spend /apps /usage
Plan  — /tasks /brief /pomodoro /stats /weekly /contacts
Brain — /learned /learn /curiosity /brain /train /facts /forget /mood /journal
Dev   — /github
System— /status /diag /why /pause /resume /reflect /reset /menu"""



@router.message(Command("start", "help"))
async def cmd_help(message: Message) -> None:
    from inais.bot.routers.menu import home_kb

    await message.answer(HELP, reply_markup=home_kb())


@router.message(Command("commands"))
async def cmd_commands(message: Message) -> None:
    await message.answer(ALL_COMMANDS)


@router.message(Command("diag"))
async def cmd_diag(message: Message) -> None:
    from inais import diagnostics

    await message.answer("🩺 Testing each provider…")
    try:
        report = await diagnostics.run()
    except Exception as e:
        log.exception("/diag failed")
        report = f"Diagnostics itself failed: {type(e).__name__}: {e}"
    for chunk in split_message(report):
        await message.answer(chunk)


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
    # /learn is an explicit request, so it runs even when the AUTOMATIC background loop
    # (LEARNING_ENABLED) is off — that flag only governs unattended scouting.
    note = ("" if settings().learning_enabled else
            "\n\n(Background learning is off — this was a one-off. "
            "Set LEARNING_ENABLED=true to let me do this on my own.)")
    await message.answer("🔎 Going off to learn something…")
    try:
        result = await autonomy.run_cycle(message.bot, force=True)
        result += note
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
        from inais.orchestrator.router import AGENTS

        result = await nn.train_all()
        result += "\n" + await nn.train_router(AGENTS)
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


# ---------- safety controls ----------

@router.message(Command("pause"))
async def cmd_pause(message: Message) -> None:
    """Halt all background work. Conversation keeps working."""
    note = (message.text or "").partition(" ")[2].strip()
    if controls.is_paused():
        await message.answer("Already paused. /resume to start background work again.")
        return
    persisted = await schedules.pause_jobs(note)
    warn = "" if persisted else ("\n\n⚠️ No database — this pause will NOT survive a restart.")
    await message.answer(
        "⏸ Paused. No Gmail polling, reminders, briefs, snapshots, reflection or autonomous "
        "learning until you /resume.\nYou can still talk to me normally." + warn)


@router.message(Command("resume"))
async def cmd_resume(message: Message) -> None:
    if not controls.is_paused():
        await message.answer("Not paused — background work is already running.")
        return
    await schedules.resume_jobs()
    await message.answer("▶️ Resumed. Background jobs are running again; anything missed while "
                         "paused runs once now.")


@router.message(Command("status"))
async def cmd_status(message: Message) -> None:
    cfg = settings()
    paused = controls.is_paused()
    lines = ["⏸ PAUSED — background work halted (/resume)" if paused else "✅ Running"]
    if paused:
        since = await controls.paused_since()
        if since:
            lines.append(f"since {since}")

    lines.append("\nSubsystems")
    if db.pool() is not None:
        lines.append("• database: on")
    else:
        reason = db.last_error() or "SUPABASE_DB_URL not set"
        lines.append(f"• database: OFF — {reason}")
    from inais import llm

    provider = llm.effective_provider()
    note = " (auto-switched: Anthropic key rejected)" if llm.provider_override() else ""
    model = cfg.openai_agent_model if provider == "openai" else cfg.agent_model
    lines.append(f"• brain: {'on' if cfg.brain_enabled else 'OFF'} "
                 f"({provider}: {model}){note}")
    lines.append(f"• routing/triage: {cfg.triage_model}")
    lines.append(f"• binance: {'on' if cfg.binance_enabled else 'off'}")
    lines.append(f"• calendar: {'on' if cfg.calendar_enabled else 'off'}")
    lines.append(f"• autonomous learning: {'on' if cfg.learning_enabled else 'off'}")
    lines.append(f"• neural network: {'on' if cfg.nn_enabled else 'off'}")

    p = db.pool()
    if p is not None:
        try:
            accounts = await p.fetch("select email, status from gmail_accounts")
            if accounts:
                lines.append("\nGmail")
                for a in accounts:
                    mark = "⚠️ needs re-auth" if a["status"] != "active" else "ok"
                    lines.append(f"• {a['email']}: {mark}")
            counts = await p.fetchrow(
                "select (select count(*) from drafts where status = 'pending') as drafts,"
                " (select count(*) from reminders where not fired) as reminders,"
                " (select count(*) from tasks where status = 'open') as tasks")
            lines.append(f"\nPending: {counts['drafts']} draft(s), {counts['reminders']} "
                         f"reminder(s), {counts['tasks']} open task(s)")
            spend = await p.fetchrow(
                "select coalesce(sum(cost_usd), 0) as c from llm_usage"
                " where ts >= date_trunc('month', now())")
            lines.append(f"Spend this month: ${float(spend['c']):.2f} of "
                         f"${cfg.monthly_budget_usd:.0f}")
        except Exception:
            log.exception("/status database section failed")
            lines.append("\n(could not read database details)")

    jobs = schedules.job_overview()
    if jobs:
        lines.append("\nNext jobs")
        lines += [f"• {j}" for j in jobs]
    await message.answer("\n".join(lines))


@router.message(Command("why"))
async def cmd_why(message: Message) -> None:
    """Explain a recent turn: route, memory, tools, tokens, cost."""
    arg = (message.text or "").partition(" ")[2].strip()
    n = int(arg) if arg.isdigit() and int(arg) > 0 else 1
    turns = trace.recent(min(n, trace.RING_SIZE))
    if not turns:
        await message.answer("No turns traced yet — talk to me first, then ask /why.")
        return
    # recent() is newest-first and capped at n, so the last entry is the nth turn back
    # (or the oldest one still buffered, if the user asked to go back further than that)
    turn = turns[-1]
    for chunk in split_message(turn.render(index=min(n, len(turns)))):
        await message.answer(chunk)
    if trace.count() > 1:
        await message.answer(f"({trace.count()} turns in the buffer — /why 2 for the one before)")


# ---------- github + contacts ----------

@router.message(Command("github"))
async def cmd_github(message: Message) -> None:
    if not settings().github_enabled:
        await message.answer("GitHub isn't configured — set GITHUB_TOKEN (read-only) and "
                             "optionally GITHUB_REPOS for CI checks.")
        return
    try:
        items = await github.fetch_all()
    except github.GitHubAuthError as e:
        await message.answer(f"GitHub rejected the token: {e}")
        return
    except Exception:
        log.exception("/github failed")
        await message.answer("Couldn't reach GitHub — check the logs.")
        return
    for chunk in split_message(github.render_digest(items)):
        await message.answer(chunk, disable_web_page_preview=True)


@router.message(Command("contacts"))
async def cmd_contacts(message: Message) -> None:
    p = db.pool()
    if p is None:
        await message.answer("No database configured — contacts need one.")
        return
    rows = await p.fetch(
        "select id, name, org, last_contact, follow_up_at from contacts"
        " order by follow_up_at nulls last, last_contact desc nulls last limit 25")
    if not rows:
        await message.answer("No contacts yet — tell me about someone you met and I'll "
                             "remember them.")
        return
    from inais.timeutil import now_local

    today = now_local().date()
    lines = ["🤝 Contacts"]
    for r in rows:
        line = f"#{r['id']} {r['name']}" + (f" ({r['org']})" if r["org"] else "")
        if r["follow_up_at"]:
            due = "⚠️ due " if r["follow_up_at"] <= today else "follow up "
            line += f" — {due}{r['follow_up_at']}"
        elif r["last_contact"]:
            line += f" — last spoke {r['last_contact']:%Y-%m-%d}"
        lines.append(line)
    for chunk in split_message("\n".join(lines)):
        await message.answer(chunk)


@router.message(Command("weekly"))
async def cmd_weekly(message: Message) -> None:
    from inais.jobs import weekly

    await message.answer("📅 Pulling the week together…")
    try:
        text = await weekly.build_weekly_review()
    except Exception:
        log.exception("/weekly failed")
        text = None
    for chunk in split_message(text or "Not enough activity this week to review yet."):
        await message.answer(chunk)


@router.message(Command("connect"))
async def cmd_connect(message: Message) -> None:
    """Authorize a Google account by tapping a link — no laptop, no script."""
    from inais.integrations import google_oauth

    ready, reason = google_oauth.configured()
    if not ready:
        await message.answer(f"Can't start the Google sign-in yet.\n\n{reason}")
        return
    try:
        url = google_oauth.auth_url()
    except Exception as e:
        log.exception("/connect failed to build the auth url")
        await message.answer(f"Couldn't build the sign-in link: {type(e).__name__}: {e}")
        return
    scopes = "Gmail" + (" and Calendar" if settings().calendar_enabled else "")
    await message.answer(
        f"🔗 Tap to connect a Google account ({scopes}):\n\n{url}\n\n"
        f"You'll see \"Google hasn't verified this app\" — that's expected, since it's your "
        f"own private app. Tap Advanced → Continue.\n"
        f"The link expires in 15 minutes.",
        disable_web_page_preview=True)


@router.message(Command("web"))
async def cmd_web(message: Message) -> None:
    """Search the web from a command (the agent also does this on its own now)."""
    query = (message.text or "").partition(" ")[2].strip()
    if not query:
        await message.answer("Usage: /web <what to search for>")
        return
    from inais.integrations import search

    await message.answer(f"🔎 Searching: {query}")
    try:
        hits = await search.search(query, max_results=8)
    except Exception:
        log.exception("/web failed")
        await message.answer("Search failed — check the logs.")
        return
    if not hits:
        await message.answer("No results (set SERPER_API_KEY for best coverage).")
        return
    body = "\n\n".join(f"• {h.title}\n{h.url}" for h in hits)
    for chunk in split_message(f"🔎 {query}\n\n{body}"):
        await message.answer(chunk, disable_web_page_preview=True)


@router.message(Command("export"))
async def cmd_export(message: Message) -> None:
    """Export the last weekly review (or a topic) as a PDF: /export [weekly|<title>]."""
    from aiogram.types import BufferedInputFile

    from inais.integrations import pdf

    arg = (message.text or "").partition(" ")[2].strip() or "weekly"
    await message.answer("📄 Building the PDF…")
    if arg.lower() == "weekly":
        from inais.jobs import weekly
        body = await weekly.build_weekly_review() or "No activity to report this week."
        title = "Weekly Review"
    else:
        from inais.study import store as study_store
        hits = await study_store.search_chunks(arg, k=8)
        if not hits:
            await message.answer(f"Nothing to export for '{arg}'. Try /export weekly, or ask "
                                 f"me to make a PDF of something in chat.")
            return
        body = "\n\n".join(f"## {h['title']}\n{h['content']}" for h in hits)
        title = arg.title()
    try:
        data = pdf.build_pdf(title, body, "Generated by INAIS")
        await message.answer_document(
            BufferedInputFile(data, filename=f"{title.replace(' ', '_')}.pdf"),
            caption=f"📄 {title}")
    except Exception:
        log.exception("/export failed")
        await message.answer("Couldn't build that PDF — check the logs.")
