"""Reminder delivery + pomodoro timing. Driven by the 30s tick in jobs/schedules.py."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from apscheduler.triggers.cron import CronTrigger

from inais import db
from inais.config import settings
from inais.timeutil import fmt, tz

log = logging.getLogger(__name__)


def next_cron_fire(cron_expr: str, after: datetime | None = None) -> datetime | None:
    """Next fire time for a 5-field cron expression, or None if it doesn't parse."""
    try:
        trigger = CronTrigger.from_crontab(cron_expr, timezone=tz())
    except ValueError:
        log.warning("invalid cron expression %r", cron_expr)
        return None
    base = (after or datetime.now(UTC)).astimezone(tz())
    nxt = trigger.get_next_fire_time(None, base)
    return nxt.astimezone(UTC) if nxt else None


async def deliver_due(bot) -> int:
    """Send every reminder whose time has come. Returns how many fired."""
    p = db.pool()
    if p is None:
        return 0
    # claim atomically so a slow send can't double-fire on the next tick
    rows = await p.fetch(
        "update reminders set fired = true"
        " where id in (select id from reminders where not fired and fire_at <= now()"
        "              order by fire_at limit 10)"
        " returning id, text, fire_at, recurring_cron",
    )
    owner = settings().owner_telegram_id
    for r in rows:
        try:
            await bot.send_message(owner, f"⏰ {r['text']}")
        except Exception:
            log.exception("failed to deliver reminder %s", r["id"])
        if r["recurring_cron"]:
            nxt = next_cron_fire(r["recurring_cron"])
            if nxt is not None:
                await p.execute(
                    "update reminders set fired = false, fire_at = $1 where id = $2",
                    nxt, r["id"],
                )
    return len(rows)


# ---------- pomodoro ----------

async def start_pomodoro(minutes: int, label: str | None) -> tuple[int, datetime] | None:
    p = db.pool()
    if p is None:
        return None
    await p.execute(
        "update pomodoro_sessions set ended_at = now()"
        " where ended_at is null and not completed",
    )  # only one focus session at a time
    row = await p.fetchrow(
        "insert into pomodoro_sessions (minutes, label) values ($1, $2)"
        " returning id, started_at",
        minutes, label,
    )
    ends_at = row["started_at"] + timedelta(minutes=minutes)
    return row["id"], ends_at


async def stop_pomodoro() -> str | None:
    p = db.pool()
    if p is None:
        return None
    row = await p.fetchrow(
        "update pomodoro_sessions set ended_at = now()"
        " where id = (select id from pomodoro_sessions where ended_at is null and not completed"
        "             order by started_at desc limit 1)"
        " returning label, minutes",
    )
    return (row["label"] or "focus") if row else None


async def finish_due(bot) -> None:
    """Complete sessions whose time is up and ping the user for a break."""
    p = db.pool()
    if p is None:
        return
    rows = await p.fetch(
        "update pomodoro_sessions set completed = true, ended_at = now()"
        " where ended_at is null and not completed"
        "   and started_at + make_interval(mins => minutes) <= now()"
        " returning id, minutes, label",
    )
    for r in rows:
        label = f" — {r['label']}" if r["label"] else ""
        try:
            await bot.send_message(
                settings().owner_telegram_id,
                f"🍅 {r['minutes']}-minute focus block done{label}. Take a 5-minute break.\n"
                f"Voice-note me a recap and I'll check your understanding (/review).",
            )
        except Exception:
            log.exception("failed to send pomodoro break ping")


async def stats() -> str:
    p = db.pool()
    if p is None:
        return "No database configured."
    row = await p.fetchrow(
        "select"
        " count(*) filter (where completed and started_at::date = current_date) as today,"
        " count(*) filter (where completed and started_at >= current_date - 6) as week,"
        " coalesce(sum(minutes) filter (where completed and started_at::date = current_date), 0)"
        "   as mins_today"
        " from pomodoro_sessions",
    )
    days = await p.fetch(
        "select distinct started_at::date as d from pomodoro_sessions"
        " where completed order by d desc limit 60",
    )
    streak = 0
    today = datetime.now(tz()).date()
    seen = {r["d"] for r in days}
    cursor = today if today in seen else today - timedelta(days=1)
    while cursor in seen:
        streak += 1
        cursor -= timedelta(days=1)

    active = await p.fetchrow(
        "select label, minutes, started_at from pomodoro_sessions"
        " where ended_at is null and not completed order by started_at desc limit 1",
    )
    lines = [
        "🍅 Focus stats",
        f"Today: {row['today']} sessions ({row['mins_today']} min)",
        f"Last 7 days: {row['week']} sessions",
        f"Streak: {streak} day{'s' if streak != 1 else ''}",
    ]
    if active:
        ends = active["started_at"] + timedelta(minutes=active["minutes"])
        lines.append(f"In progress: {active['label'] or 'focus'} until {fmt(ends, with_date=False)}")
    return "\n".join(lines)
