"""Deciding whether there's anything worth saying, unprompted.

The bar is deliberately high. An assistant that pipes up because it can is one the user
mutes within a week, so the model is given the day's actual state and told that saying
nothing is the correct answer most of the time — then the result is rate-limited on top.
"""

from __future__ import annotations

import logging

from inais import db, llm, persona
from inais.config import settings
from inais.timeutil import now_local

log = logging.getLogger(__name__)

DECIDE_SYSTEM = """You are INAIS deciding whether to message the user unprompted right now.

You get the current state of their day. Reply with ONE of:
- The message you want to send — at most 2 short lines, in your own voice.
- The single word NOTHING.

NOTHING is the right answer most of the time. Send something only if it is genuinely useful
or genuinely warm at this exact moment: a deadline they'll miss, something you learned that
answers a question they actually asked, a follow-up that's due today, a real change in their
portfolio, or a well-judged word after a stretch of hard days.

Never send: filler check-ins ("how's it going?"), things they can see themselves, anything
you already told them today, or a nudge about something they're clearly in the middle of.
Never invent activity you weren't given. If it can wait for the morning brief, it waits."""


async def _context() -> str:
    """The real state of the user's day — no invention downstream."""
    p = db.pool()
    if p is None:
        return ""
    parts: list[str] = [f"Local time: {now_local():%A %H:%M}"]

    tasks = await p.fetch(
        "select title, due from tasks where status = 'open' and due is not null"
        "   and due < now() + interval '2 days' order by due limit 5")
    if tasks:
        parts.append("Due soon: " + "; ".join(
            f"{t['title']} ({t['due']:%a %H:%M})" for t in tasks))

    overdue = await p.fetchval(
        "select count(*) from tasks where status = 'open' and due < now()")
    if overdue:
        parts.append(f"Overdue tasks: {overdue}")

    follow = await p.fetch(
        "select name from contacts where follow_up_at is not null"
        "   and follow_up_at <= current_date limit 3")
    if follow:
        parts.append("Follow-ups due: " + ", ".join(c["name"] for c in follow))

    learned = await p.fetch(
        "select topic, summary from knowledge where learned_at > now() - interval '12 hours'"
        " order by learned_at desc limit 3")
    if learned:
        parts.append("You researched recently: " + "; ".join(
            f"{k['topic']} — {k['summary'][:120]}" for k in learned))

    mood = await p.fetchrow(
        "select avg(mood_score) as score, count(*) as n from journal_entries"
        " where created_at > now() - interval '5 days'")
    if mood and mood["n"]:
        parts.append(f"Journal mood over {mood['n']} recent entries: {float(mood['score']):+.1f}")

    last_user = await p.fetchval(
        "select extract(epoch from (now() - max(ts))) / 3600 from messages where role = 'user'")
    if last_user is not None:
        parts.append(f"Hours since they last messaged: {float(last_user):.1f}")

    already = await p.fetch(
        "select content from proactive_log where sent_at::date = current_date limit 5")
    parts.append("Already said unprompted today: " + (
        "; ".join(a["content"][:80] for a in already) if already else "nothing"))
    return "\n".join(parts)


async def consider(bot) -> str | None:
    """Decide, and send if worth it. Returns what was sent, or None."""
    allowed, reason = await persona.may_speak_now()
    if not allowed:
        log.debug("proactive skipped: %s", reason)
        return None
    if not settings().brain_enabled:
        return None

    context = await _context()
    if not context:
        return None
    try:
        reply = await llm.agent_text(
            system=f"{persona.CHARACTER}\n\n{DECIDE_SYSTEM}",
            user=context, max_tokens=400, purpose="proactive", cheap=True)
    except Exception:
        log.exception("proactive decision failed")
        return None

    message = (reply or "").strip()
    if not message or message.upper().startswith("NOTHING"):
        return None

    await bot.send_message(settings().owner_telegram_id, message)
    await persona.log_proactive("checkin", message)
    log.info("proactive message sent: %s", message[:80])
    return message


async def scheduled(bot) -> None:
    """APScheduler entry point — never raises."""
    try:
        await consider(bot)
    except Exception:
        log.exception("proactive check failed")
