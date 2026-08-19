"""Reminder delivery (alarm-grade) + pomodoro timing. Driven by the 30s tick in schedules.

A reminder that sends one silent-ish message is not a reminder, it's a diary entry. Firing
here means: the reminder message with a Stop button, then a burst of extra pings — each one
triggers a notification sound — which are deleted immediately so only the reminder remains.
Until the user stops it (button, or typing "stop"), it re-pings on a doubling interval and
then gives up gracefully, saying so on the original message.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from apscheduler.triggers.cron import CronTrigger

from inais import db
from inais.config import settings
from inais.timeutil import fmt, tz

log = logging.getLogger(__name__)

BURST_GAP_SECONDS = 0.4   # long enough for each ping to notify before it disappears


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


def next_nag_delay(nag_count: int, base_minutes: int) -> timedelta:
    """3, 6, 12… minutes — insistent without being a machine gun."""
    return timedelta(minutes=max(1, base_minutes) * (2 ** max(0, nag_count)))


def is_stop_text(text: str) -> bool:
    """Does this message mean 'stop the reminder'? Deliberately narrow: only bare stop
    phrases count, so ordinary sentences containing the word keep going to the brain."""
    cleaned = (text or "").strip().lower().rstrip("!. ")
    return cleaned in {"stop", "stop it", "stop reminder", "stop reminding me", "ok stop"}


async def _ping_burst(bot, chat_id: int, label: str) -> None:
    """Extra notification sounds: send short pings, then delete them.

    Each send triggers a push notification; deleting right after keeps the chat clean, so
    the phone buzzes N times but only the reminder message remains.
    """
    burst = max(0, settings().reminder_burst)
    sent_ids: list[int] = []
    for _ in range(burst):
        try:
            msg = await bot.send_message(chat_id, f"🔔 {label[:60]}")
            sent_ids.append(msg.message_id)
        except Exception:
            log.exception("ping burst send failed")
            break
        await asyncio.sleep(BURST_GAP_SECONDS)
    for message_id in sent_ids:
        try:
            await bot.delete_message(chat_id, message_id)
        except Exception:
            log.debug("could not delete ping %s", message_id)


async def deliver_due(bot) -> int:
    """Fire every reminder whose time has come. Returns how many fired."""
    from inais.bot import keyboards  # late import (bot package imports jobs indirectly)

    p = db.pool()
    if p is None:
        return 0
    cfg = settings()
    # claim atomically so a slow send can't double-fire on the next tick
    rows = await p.fetch(
        "update reminders set fired = true"
        " where id in (select id from reminders where not fired and fire_at <= now()"
        "              order by fire_at limit 10)"
        " returning id, text, fire_at, recurring_cron",
    )
    owner = cfg.owner_telegram_id
    for r in rows:
        message_id = None
        try:
            msg = await bot.send_message(
                owner, f"⏰ {r['text']}",
                reply_markup=keyboards.reminder_stop_kb(r["id"]))
            message_id = msg.message_id
        except Exception:
            log.exception("failed to deliver reminder %s", r["id"])
        await p.execute(
            "update reminders set acknowledged = false, nag_count = 0, message_id = $1,"
            " nag_at = now() + $2 where id = $3",
            message_id, next_nag_delay(0, cfg.reminder_nag_minutes), r["id"],
        )
        await _ping_burst(bot, owner, r["text"])
        if r["recurring_cron"]:
            nxt = next_cron_fire(r["recurring_cron"])
            if nxt is not None:
                await p.execute(
                    "update reminders set fired = false, fire_at = $1 where id = $2",
                    nxt, r["id"],
                )
    return len(rows)


async def nag_unacknowledged(bot) -> int:
    """Re-ping reminders nobody has stopped; give up gracefully after the cap."""
    p = db.pool()
    if p is None:
        return 0
    cfg = settings()
    rows = await p.fetch(
        "select id, text, nag_count, message_id from reminders"
        " where not acknowledged and nag_at is not null and nag_at <= now() limit 10",
    )
    owner = cfg.owner_telegram_id
    nagged = 0
    for r in rows:
        if r["nag_count"] >= cfg.reminder_max_nags:
            # enough — stop insisting, but say so where the reminder lives
            await p.execute(
                "update reminders set acknowledged = true, nag_at = null where id = $1",
                r["id"])
            if r["message_id"]:
                try:
                    await bot.edit_message_text(
                        f"⏰ {r['text']}\n\n(no response after "
                        f"{cfg.reminder_max_nags} re-pings — stopped reminding)",
                        chat_id=owner, message_id=r["message_id"])
                except Exception:
                    log.debug("could not edit reminder %s on give-up", r["id"])
            continue
        await _ping_burst(bot, owner, r["text"])
        nagged += 1
        await p.execute(
            "update reminders set nag_count = nag_count + 1, nag_at = now() + $1"
            " where id = $2",
            next_nag_delay(r["nag_count"] + 1, cfg.reminder_nag_minutes), r["id"],
        )
    return nagged


async def acknowledge(reminder_id: int) -> dict | None:
    """Stop one reminder. Returns the row, or None when it wasn't waiting."""
    p = db.pool()
    if p is None:
        return None
    row = await p.fetchrow(
        "update reminders set acknowledged = true, nag_at = null"
        " where id = $1 and not acknowledged returning id, text, message_id",
        reminder_id)
    return dict(row) if row else None


async def acknowledge_latest() -> dict | None:
    """Typed 'stop': silence the most recently fired unacknowledged reminder."""
    p = db.pool()
    if p is None:
        return None
    row = await p.fetchrow(
        "update reminders set acknowledged = true, nag_at = null"
        " where id = (select id from reminders where not acknowledged and fired"
        "             order by fire_at desc limit 1)"
        " returning id, text, message_id")
    return dict(row) if row else None


async def any_awaiting_ack() -> bool:
    p = db.pool()
    if p is None:
        return False
    return bool(await p.fetchval(
        "select exists(select 1 from reminders where not acknowledged and fired)"))


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
            await _ping_burst(bot, settings().owner_telegram_id,
                              f"focus done{label}")
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
