"""Morning brief and evening study nudge — the proactive daily touchpoints."""

from __future__ import annotations

import logging

from inais import db
from inais.config import settings
from inais.timeutil import fmt, now_local

log = logging.getLogger(__name__)


async def build_morning_brief() -> str:
    p = db.pool()
    if p is None:
        return ""
    cfg = settings()
    parts = [f"☀️ Good morning — {now_local().strftime('%A %d %B')}"]

    if cfg.calendar_enabled:
        try:
            from inais.agents.calendar_tools import primary_token
            from inais.integrations import calendar

            token = await primary_token()
            if token:
                events = await calendar.agenda(token, 0, 1)
                parts.append("📅 Today\n" + calendar.render_agenda(events))
        except Exception:
            log.exception("morning brief: calendar section failed")

    tasks = await p.fetch(
        "select id, context, title, due from tasks where status = 'open'"
        "  and (due is null or due::date <= current_date + 1)"
        " order by due nulls last, priority limit 12",
    )
    if tasks:
        lines = []
        for t in tasks:
            overdue = t["due"] is not None and t["due"].date() < now_local().date()
            flag = "⚠️ overdue — " if overdue else ""
            lines.append(f"• [{t['context']}] {t['title']} ({flag}{fmt(t['due'])})")
        parts.append("✅ Tasks\n" + "\n".join(lines))

    reminders = await p.fetch(
        "select text, fire_at from reminders where not fired"
        "  and fire_at::date = current_date order by fire_at limit 8",
    )
    if reminders:
        parts.append("⏰ Reminders today\n" + "\n".join(
            f"• {fmt(r['fire_at'], with_date=False)} — {r['text']}" for r in reminders))

    plan = await p.fetch(
        "select e.name, sp.focus from study_plan sp join exams e on e.id = sp.exam_id"
        " where sp.day = current_date and not sp.done limit 5",
    )
    if plan:
        parts.append("📚 Study focus\n" + "\n".join(f"• {r['name']}: {r['focus']}" for r in plan))

    try:
        from inais.agents.contacts import due_follow_ups

        follow_ups = await due_follow_ups()
        if follow_ups:
            parts.append("🤝 Follow up\n" + "\n".join(
                f"• {c['name']}" + (f" ({c['org']})" if c["org"] else "")
                + (f" — due {c['follow_up_at']}" if c["follow_up_at"] else "")
                for c in follow_ups))
    except Exception:
        log.exception("morning brief: contacts section failed")

    try:
        from inais.agents.commitments import due_commitments

        commits = await due_commitments()
        if commits:
            parts.append("🤝 You said you'd\n" + "\n".join(
                f"• {c['text']}" + (f" (by {c['due_at']:%d %b})" if c["due_at"] else "")
                for c in commits))
    except Exception:
        log.exception("morning brief: commitments section failed")

    pomo = await p.fetchrow(
        "select count(*) as n, coalesce(sum(minutes), 0) as mins from pomodoro_sessions"
        " where completed and started_at::date = current_date - 1",
    )
    if pomo and pomo["n"]:
        parts.append(f"🍅 Yesterday: {pomo['n']} focus sessions ({pomo['mins']} min)")

    if cfg.learning_enabled:
        learned = await p.fetch(
            "select topic, summary from knowledge"
            " where learned_at > now() - interval '24 hours' order by learned_at desc limit 3",
        )
        if learned:
            parts.append("🧠 What I learned overnight\n" + "\n".join(
                f"• {r['topic']}: {r['summary'][:160]}" for r in learned) + "\n(/learned for more)")

    return "\n\n".join(parts) if len(parts) > 1 else ""


async def build_study_nudge() -> tuple[str, int] | None:
    """Today's unfinished study focus, if any. Returns (text, study_plan_id)."""
    p = db.pool()
    if p is None:
        return None
    row = await p.fetchrow(
        "select sp.id, sp.focus, e.name, e.date from study_plan sp"
        " join exams e on e.id = sp.exam_id"
        " where sp.day = current_date and not sp.done order by sp.id limit 1",
    )
    if row is None:
        return None
    days_left = (row["date"] - now_local().date()).days
    text = (f"📚 Today's focus for {row['name']} ({days_left} day"
            f"{'s' if days_left != 1 else ''} to go):\n\n{row['focus']}\n\n"
            f"Voice-note me a recap when you're done and I'll check it (/review).")
    return text, row["id"]
