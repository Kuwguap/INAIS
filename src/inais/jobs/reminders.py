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
    """Fire every reminder whose time has come. Returns how many fired.

    The whole state transition — claim, arm the nag machine, re-arm a recurring fire_at —
    happens in ONE transaction per row, so a crash at any point either leaves the row
    unclaimed (retried next tick) or fully armed. The old batch shape claimed ten rows
    up front and then mutated them one by one: a mid-batch error stranded the rest as
    fired-but-never-sent, matching no query in the codebase, forever.

    The Telegram sends happen after commit. A failed send is not a lost reminder: the nag
    machine is armed and treats a missing message_id as "the reminder itself still needs
    delivering" (see nag_unacknowledged).
    """
    p = db.pool()
    if p is None:
        return 0
    cfg = settings()
    fired: list[dict] = []
    async with p.acquire() as conn:
        for _ in range(10):  # per-tick cap
            async with conn.transaction():
                row = await conn.fetchrow(
                    "select id, text, recurring_cron from reminders"
                    " where not fired and fire_at <= now()"
                    " order by fire_at limit 1 for update skip locked")
                if row is None:
                    break
                nxt = (next_cron_fire(row["recurring_cron"])
                       if row["recurring_cron"] else None)
                await conn.execute(
                    "update reminders set"
                    " fired = $1, fire_at = coalesce($2, fire_at),"
                    " acknowledged = false, nag_count = 0, message_id = null,"
                    " nag_at = now() + $3, last_fired_at = now()"
                    " where id = $4",
                    nxt is None,          # recurring stays fireable at its next slot
                    nxt,
                    next_nag_delay(0, cfg.reminder_nag_minutes),
                    row["id"],
                )
            fired.append(dict(row))

    owner = cfg.owner_telegram_id
    for r in fired:
        await _send_reminder(bot, owner, r["id"], r["text"])
        await _ping_burst(bot, owner, r["text"])
    return len(fired)


async def _send_reminder(bot, owner: int, reminder_id: int, text: str) -> bool:
    """Send the durable reminder message and record its id. False on failure."""
    from inais.bot import keyboards  # late import (bot package imports jobs indirectly)

    p = db.pool()
    try:
        msg = await bot.send_message(
            owner, f"⏰ {text}", reply_markup=keyboards.reminder_stop_kb(reminder_id))
    except Exception:
        log.exception("failed to deliver reminder %s — nag pass will retry", reminder_id)
        return False
    if p is not None:
        await p.execute("update reminders set message_id = $1 where id = $2",
                        msg.message_id, reminder_id)
    return True


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
            farewell = (f"⏰ {r['text']}\n\n(no response after "
                        f"{cfg.reminder_max_nags} re-pings — stopped reminding)")
            try:
                if r["message_id"]:
                    await bot.edit_message_text(farewell, chat_id=owner,
                                                message_id=r["message_id"])
                else:
                    # the durable message never made it out; the farewell must, or the
                    # reminder's content was never delivered at all
                    await bot.send_message(owner, farewell)
            except Exception:
                log.debug("could not close out reminder %s", r["id"])
            continue
        if not r["message_id"]:
            # the initial send failed — this nag IS the delivery retry, buttons and all
            await _send_reminder(bot, owner, r["id"], r["text"])
        await _ping_burst(bot, owner, r["text"])
        nagged += 1
        await p.execute(
            "update reminders set nag_count = nag_count + 1, nag_at = now() + $1"
            " where id = $2",
            next_nag_delay(r["nag_count"] + 1, cfg.reminder_nag_minutes), r["id"],
        )
    return nagged


async def snooze(reminder_id: int, minutes: int) -> dict | None:
    """Push a ringing reminder out by `minutes`. Returns the row, or None if it's gone.

    One-shot: re-arm in place (fire_at forward, back to pending, silence the current ring) so
    deliver_due re-fires it. Recurring: NEVER overwrite fire_at (that's the next occurrence) —
    stop the current ring and insert a decoupled one-shot copy for the snoozed slot."""
    p = db.pool()
    if p is None:
        return None
    fire_at = datetime.now(UTC) + timedelta(minutes=max(1, int(minutes)))
    async with p.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "select id, text, recurring_cron, message_id from reminders where id = $1",
                reminder_id)
            if row is None:
                return None
            if row["recurring_cron"] is None:
                await conn.execute(
                    "update reminders set fire_at = $1, fired = false, acknowledged = true,"
                    " nag_at = null, nag_count = 0, message_id = null where id = $2",
                    fire_at, reminder_id)
            else:
                await conn.execute(
                    "update reminders set acknowledged = true, nag_at = null where id = $1",
                    reminder_id)
                await conn.execute(
                    "insert into reminders (text, fire_at, recurring_cron) values ($1, $2, null)",
                    row["text"], fire_at)
    return {"id": reminder_id, "text": row["text"], "message_id": row["message_id"]}


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
    # order by when it actually RANG: a recurring reminder's fire_at is already re-armed
    # to the next occurrence while it rings, so fire_at would pick the wrong one
    row = await p.fetchrow(
        "update reminders set acknowledged = true, nag_at = null"
        " where id = (select id from reminders where not acknowledged"
        "             order by last_fired_at desc nulls last limit 1)"
        " returning id, text, message_id")
    return dict(row) if row else None


async def any_awaiting_ack() -> bool:
    p = db.pool()
    if p is None:
        return False
    return bool(await p.fetchval(
        "select exists(select 1 from reminders where not acknowledged)"))


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
    zone = settings().timezone
    row = await p.fetchrow(
        "select"
        " count(*) filter (where completed and (started_at at time zone $1)::date"
        "   = (now() at time zone $1)::date) as today,"
        " count(*) filter (where completed and (started_at at time zone $1)::date"
        "   >= (now() at time zone $1)::date - 6) as week,"
        " coalesce(sum(minutes) filter (where completed and (started_at at time zone $1)::date"
        "   = (now() at time zone $1)::date), 0) as mins_today"
        " from pomodoro_sessions",
        zone,
    )
    days = await p.fetch(
        "select distinct (started_at at time zone $1)::date as d from pomodoro_sessions"
        " where completed order by d desc limit 60",
        zone,
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
