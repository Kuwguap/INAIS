"""The Sunday-evening review: what actually happened this week, and what to aim at next.

The statistics are computed in SQL — the model is only asked for the three focuses, and only
from the numbers gathered here. That split matters: a model asked to "review my week" invents
plausible-sounding progress, while a model handed real counts can only reason about them.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

from inais import db, journal, llm
from inais.config import settings
from inais.timeutil import now_local

log = logging.getLogger(__name__)

FOCUS_SYSTEM = """You are closing out a student/developer's week with them.

You get real statistics for the week just ended. Suggest exactly three focuses for next week.

Rules:
- Ground every suggestion in the numbers you were given. Never invent activity.
- Be specific and small enough to actually do ("clear the 3 overdue tasks before Wednesday"),
  not aspirational ("be more consistent").
- If something slipped badly, say so plainly in one clause — no cheerleading — then make the
  focus the fix.
- If the week was genuinely good, say that instead of manufacturing a problem.
- Reply as exactly three lines, each starting "• ". No preamble, no heading."""


async def gather(start: date, end: date) -> dict:
    """Everything the review reports, straight from the database."""
    p = db.pool()
    if p is None:
        return {}
    stats: dict = {"start": start, "end": end}

    tasks = await p.fetchrow(
        "select count(*) filter (where status = 'done' and completed_at::date >= $1) as done,"
        " count(*) filter (where status = 'open' and due is not null and due < now()) as overdue,"
        " count(*) filter (where status = 'open') as open"
        " from tasks", start)
    stats["tasks"] = dict(tasks) if tasks else {}

    pomo = await p.fetchrow(
        "select count(*) as sessions, coalesce(sum(minutes), 0) as minutes,"
        " count(distinct started_at::date) as days"
        " from pomodoro_sessions where completed and started_at::date >= $1", start)
    stats["pomodoro"] = dict(pomo) if pomo else {}

    plan = await p.fetchrow(
        "select count(*) as total, count(*) filter (where done) as done from study_plan"
        " where day >= $1 and day <= $2", start, end)
    stats["study_plan"] = dict(plan) if plan else {}

    spend = await p.fetch(
        "select model, sum(cost_usd) as cost from llm_usage"
        " where ts::date >= $1 group by model order by cost desc limit 5", start)
    stats["spend"] = [dict(r) for r in spend]
    stats["spend_total"] = sum(float(r["cost"]) for r in spend)

    learned = await p.fetch(
        "select topic, summary from knowledge where learned_at::date >= $1"
        " order by learned_at desc limit 5", start)
    stats["learned"] = [dict(r) for r in learned]

    reviews = await p.fetchrow(
        "select count(*) as n from review_items where last_reviewed::date >= $1", start)
    stats["cards_reviewed"] = int(reviews["n"]) if reviews else 0

    drills = await p.fetchrow(
        "select count(*) as n from drill_answers where created_at::date >= $1", start)
    stats["drills"] = int(drills["n"]) if drills else 0

    entries, mood = await journal.counts_since(start)
    stats["journal_entries"], stats["mood_avg"] = entries, mood
    return stats


def render_stats(s: dict) -> str:
    """The factual half — no model involved."""
    if not s:
        return ""
    tasks, pomo, plan = s.get("tasks", {}), s.get("pomodoro", {}), s.get("study_plan", {})
    lines = [f"📅 Week in review — {s['start']:%d %b} to {s['end']:%d %b}", ""]

    lines.append(f"✅ Tasks: {tasks.get('done', 0)} completed"
                 + (f", ⚠️ {tasks.get('overdue', 0)} overdue" if tasks.get("overdue") else "")
                 + f" ({tasks.get('open', 0)} still open)")

    if pomo.get("sessions"):
        lines.append(f"🍅 Focus: {pomo['sessions']} sessions, {pomo['minutes']} min "
                     f"across {pomo['days']} day(s)")
    else:
        lines.append("🍅 Focus: no completed sessions this week")

    if plan.get("total"):
        pct = plan["done"] / plan["total"] * 100
        lines.append(f"📚 Study plan: {plan['done']}/{plan['total']} days done ({pct:.0f}%)")

    if s.get("cards_reviewed") or s.get("drills"):
        lines.append(f"🗂 Review: {s.get('cards_reviewed', 0)} cards, "
                     f"{s.get('drills', 0)} drill answers")

    if s.get("journal_entries"):
        mood = s.get("mood_avg")
        lines.append(f"📓 Journal: {s['journal_entries']} entries"
                     + (f", mood {mood:+.1f}" if mood is not None else ""))

    lines.append(f"💸 AI spend: ${s.get('spend_total', 0):.2f}"
                 + (f" (top: {s['spend'][0]['model']})" if s.get("spend") else ""))

    if s.get("learned"):
        lines.append("🧠 Taught myself: " + ", ".join(k["topic"] for k in s["learned"][:3]))
    return "\n".join(lines)


async def suggest_focuses(stats: dict) -> str:
    if not settings().brain_enabled or not stats:
        return ""
    payload = render_stats(stats)
    learned = "\n".join(f"- {k['topic']}: {k['summary'][:150]}" for k in stats.get("learned", []))
    try:
        return await llm.agent_text(
            system=FOCUS_SYSTEM,
            user=f"{payload}\n\nWHAT I RESEARCHED FOR THEM:\n{learned or '(nothing)'}",
            max_tokens=500,
            purpose="weekly_review",
        )
    except Exception:
        log.exception("weekly focus suggestion failed")
        return ""


async def build_weekly_review() -> str | None:
    """Full review text, or None when there is nothing at all to say."""
    if db.pool() is None:
        return None
    today = now_local().date()
    start = today - timedelta(days=6)
    stats = await gather(start, today)
    if not stats:
        return None

    body = render_stats(stats)
    nothing_happened = (
        not stats.get("tasks", {}).get("done")
        and not stats.get("pomodoro", {}).get("sessions")
        and not stats.get("journal_entries")
        and not stats.get("spend_total")
    )
    if nothing_happened:
        return None   # a review of an empty week is noise

    focuses = await suggest_focuses(stats)
    if focuses:
        body += f"\n\n🎯 Next week\n{focuses}"
    return body
