"""Deciding whether there's anything worth saying, unprompted.

The bar is deliberately high. An assistant that pipes up because it can is one the user
mutes within a week, so the model is given the day's actual state and told that saying
nothing is the correct answer most of the time — then the result is rate-limited on top.
"""

from __future__ import annotations

import logging
import re

from inais import db, llm, persona
from inais.config import settings
from inais.timeutil import now_local

log = logging.getLogger(__name__)

DECIDE_SYSTEM = """You are INAIS deciding whether to message the user unprompted right now.

You get the current state of their day. Reply with ONLY JSON:
{"action": "send" | "skip",
 "medium": "text" | "voice",
 "message": "at most 2 short lines, in your own voice (empty when skipping)"}

Choose "voice" only when hearing it beats reading it — encouragement, a good-morning, a
thought worth sharing warmly. Deadlines, numbers and anything they may need to scroll back
to stay "text".

"skip" is the right answer most of the time. Send something only if it is genuinely useful
or genuinely warm at this exact moment: a deadline they'll miss, something you learned that
answers a question they actually asked, a follow-up that's due today, a real change in their
portfolio, or a well-judged word after a stretch of hard days.

Never send: filler check-ins ("how's it going?"), things they can see themselves, anything
you already told them today, or a nudge about something they're clearly in the middle of.
Never invent activity you weren't given. If it can wait for the morning brief, it waits."""


async def _context() -> tuple[str, dict | None]:
    """The real state of the user's day — no invention downstream.

    Returns (context_text, unshared_knowledge_candidate). The candidate is one thing it
    researched on its own that hasn't been shared yet; if the decision sends and references it,
    consider() marks it surfaced so it never repeats."""
    p = db.pool()
    if p is None:
        return "", None
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

    # next exam (study agent)
    try:
        from inais.study import store as study_store

        ex = await study_store.upcoming_exam()
        if ex:
            days = (ex["date"] - now_local().date()).days
            parts.append(f"Next exam: {ex['name']} in {days} day(s)")
    except Exception:
        log.exception("proactive context: exam lookup failed")

    follow = await p.fetch(
        "select name from contacts where follow_up_at is not null"
        "   and follow_up_at <= current_date limit 3")
    if follow:
        parts.append("Follow-ups due: " + ", ".join(c["name"] for c in follow))

    # commitments the user made ("I'll email my advisor")
    try:
        from inais.agents import commitments

        commits = await commitments.due_commitments(3)
        if commits:
            parts.append("Commitments due: " + "; ".join(c["text"] for c in commits))
    except Exception:
        log.exception("proactive context: commitments lookup failed")

    # application deadlines + stalled applications
    try:
        from inais.agents import applications

        deadlines = await applications.due_deadlines(3)
        if deadlines:
            parts.append("Application deadlines: " + "; ".join(
                f"{a['org']} ({a['deadline']:%d %b})" for a in deadlines))
        stale = await applications.stale_applications(limit=3)
        if stale:
            parts.append("Applications with no update in a while: " + "; ".join(
                f"{a['org']} — {a['status']}" for a in stale))
    except Exception:
        log.exception("proactive context: applications lookup failed")

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

    # mood direction over the last week — lets a well-judged warm word land after a hard stretch
    try:
        from inais import journal

        d = await journal.trend_direction(7)
        if d:
            parts.append(f"Mood trend: {d}")
    except Exception:
        log.exception("proactive context: mood trend failed")

    last_user = await p.fetchval(
        "select extract(epoch from (now() - max(ts))) / 3600 from messages where role = 'user'")
    if last_user is not None:
        parts.append(f"Hours since they last messaged: {float(last_user):.1f}")

    # one thing it researched on its own that it hasn't shared yet (de-duped via surfaced_at)
    candidate: dict | None = None
    unshared = await p.fetchrow(
        "select id, topic, summary from knowledge where surfaced_at is null"
        "   and learned_at > now() - interval '7 days' and engagement >= 0"
        " order by engagement desc, learned_at desc limit 1")
    if unshared:
        parts.append(f"Unshared thing you researched: {unshared['topic']} — "
                     f"{unshared['summary'][:120]}")
        candidate = {"id": unshared["id"], "topic": unshared["topic"]}

    already = await p.fetch(
        "select content from proactive_log where sent_at::date = current_date limit 5")
    parts.append("Already said unprompted today: " + (
        "; ".join(a["content"][:80] for a in already) if already else "nothing"))
    return "\n".join(parts), candidate


def _mentions(message: str, topic: str) -> bool:
    """True if the sent message plausibly references the topic — so knowledge is only marked
    'surfaced' when it was actually shared, not when the model sent something unrelated."""
    msg = (message or "").lower()
    words = re.findall(r"[a-z0-9]{4,}", (topic or "").lower())
    return any(w in msg for w in words)


async def consider(bot) -> str | None:
    """Decide, and send if worth it. Returns what was sent, or None."""
    cfg = settings()
    allowed, reason = await persona.may_speak_now()
    if not allowed:
        log.debug("proactive skipped: %s", reason)
        return None
    if not cfg.brain_enabled:
        return None

    context, candidate = await _context()
    if not context:
        return None
    try:
        reply = await llm.agent_text(
            system=f"{persona.CHARACTER}\n\n{DECIDE_SYSTEM}",
            user=context, max_tokens=400, purpose="proactive", cheap=True)
    except Exception:
        log.exception("proactive decision failed")
        return None

    decision = parse_decision(reply, voice_allowed=cfg.voice_notes_enabled)
    if decision is None:
        return None
    medium, message = decision

    # The engagement head has watched which unprompted messages actually got replies.
    # Once it beats chance, a message it rates below the floor is held back — the model
    # proposes, the user's own history disposes.
    from inais.brain import nn

    predicted = await nn.score("engagement", message,
                               context=nn.time_features(now_local()))
    if predicted is not None and predicted < cfg.proactive_min_engagement:
        log.info("proactive held back (engagement %.2f < %.2f): %s",
                 predicted, cfg.proactive_min_engagement, message[:60])
        return None

    if medium == "voice":
        from aiogram.types import BufferedInputFile

        from inais.integrations import stt_tts

        try:
            ogg = await stt_tts.synthesize_voice(message)
        except Exception:
            log.exception("proactive voice synthesis failed — sending text")
            ogg = None
        if ogg:
            await bot.send_voice(cfg.owner_telegram_id,
                                 BufferedInputFile(ogg, filename="inais.ogg"))
        else:
            medium = "text"
    if medium == "text":
        await bot.send_message(cfg.owner_telegram_id, message)

    await persona.log_proactive("checkin", message, medium=medium)
    # if it actually shared the thing it researched, mark it surfaced so it never repeats
    if candidate is not None and _mentions(message, candidate["topic"]):
        try:
            await db.pool().execute(
                "update knowledge set surfaced_at = now() where id = $1", candidate["id"])
        except Exception:
            log.exception("marking knowledge surfaced failed")
    log.info("proactive %s sent: %s", medium, message[:80])
    return message


def parse_decision(raw: str, voice_allowed: bool) -> tuple[str, str] | None:
    """(medium, message) or None. Tolerates the old plain-text/NOTHING format."""
    text = (raw or "").strip()
    if not text or text.upper().startswith(("NOTHING", "SKIP")):
        return None
    data = llm.parse_json_block(text)
    if data:
        if str(data.get("action", "")).lower() != "send":
            return None
        message = str(data.get("message", "")).strip()
        if not message:
            return None
        medium = str(data.get("medium", "text")).lower()
        if medium != "voice" or not voice_allowed:
            medium = "text"
        return medium, message[:1000]
    return "text", text[:1000]   # model ignored the JSON contract; treat it as a text send


async def scheduled(bot) -> None:
    """APScheduler entry point — never raises."""
    try:
        await consider(bot)
    except Exception:
        log.exception("proactive check failed")
