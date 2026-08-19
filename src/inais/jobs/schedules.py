"""All periodic work, in-process (APScheduler) — no external cron needed."""

from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from inais import controls, db
from inais.agents import email_agent, finance
from inais.bot import keyboards
from inais.brain import autonomy, curiosity, nn
from inais.config import settings
from inais.integrations import binance, gmail
from inais.jobs import brief, github_watch, reminders, weekly
from inais.memory import reflection
from inais.textutil import split_message

log = logging.getLogger(__name__)

# Module-level handle so /pause and /resume can reach the running scheduler.
_scheduler: AsyncIOScheduler | None = None


def get_scheduler() -> AsyncIOScheduler | None:
    return _scheduler


async def pause_jobs(note: str = "") -> bool:
    """Stop all background work and remember it across restarts."""
    persisted = await controls.set_paused(True, note)
    if _scheduler is not None and _scheduler.running:
        _scheduler.pause()
    return persisted


async def resume_jobs() -> bool:
    persisted = await controls.set_paused(False)
    if _scheduler is not None and _scheduler.running:
        _scheduler.resume()
    return persisted


def job_overview(limit: int = 8) -> list[str]:
    """Next run times, for /status."""
    if _scheduler is None:
        return []
    from inais.timeutil import fmt

    jobs = _scheduler.get_jobs()
    # next_run_time is None for every job while the scheduler is paused
    scheduled = sorted((j for j in jobs if j.next_run_time), key=lambda j: j.next_run_time)
    rows = [f"{j.id}: {fmt(j.next_run_time)}" for j in scheduled[:limit]]
    idle = [j.id for j in jobs if not j.next_run_time]
    if idle:
        rows.append(f"paused: {len(idle)} job(s)")
    return rows


def setup(bot) -> AsyncIOScheduler:
    cfg = settings()
    scheduler = AsyncIOScheduler(timezone=cfg.timezone)

    async def gmail_poll() -> None:
        if db.pool() is not None:
            await email_agent.poll_all(bot)

    async def binance_snapshot() -> None:
        try:
            total = await binance.take_snapshot()
            if total is not None:
                log.info("binance snapshot: total ≈ $%.2f", total)
        except Exception:
            log.exception("binance snapshot failed")

    async def daily_summary() -> None:
        try:
            text = await finance.build_daily_summary()
            if text:
                await bot.send_message(cfg.owner_telegram_id, text)
        except Exception:
            log.exception("daily summary failed")

    async def nightly_reflection() -> None:
        try:
            await reflection.run_reflection()
        except Exception:
            log.exception("nightly reflection failed")

    async def budget_check() -> None:
        p = db.pool()
        if p is None:
            return
        row = await p.fetchrow(
            "select coalesce(sum(cost_usd), 0) as total from llm_usage"
            " where ts >= date_trunc('month', now())",
        )
        total = float(row["total"])
        if total > cfg.monthly_budget_usd:
            await bot.send_message(
                cfg.owner_telegram_id,
                f"🚨 AI spend this month is ${total:.2f} — over your ${cfg.monthly_budget_usd:.0f} "
                f"budget. Check /usage.",
            )

    async def token_health() -> None:
        """Weekly: exercise each Gmail refresh token so failures surface proactively."""
        for account in await gmail.list_accounts():
            try:
                await gmail.current_history_id(account["refresh_token"])
            except gmail.GmailAuthError:
                await gmail.mark_needs_reauth(account["email"])
                await bot.send_message(
                    cfg.owner_telegram_id,
                    f"⚠️ Weekly check: Gmail token for {account['email']} is dead. Re-run:\n"
                    f"python scripts/authorize_gmail.py {account['email']}",
                )
            except Exception:
                log.exception("token health check failed for %s", account["email"])

    # ---------- planner (M8) ----------

    async def reminder_tick() -> None:
        try:
            await reminders.deliver_due(bot)
            await reminders.finish_due(bot)
        except Exception:
            log.exception("reminder tick failed")

    async def morning_brief() -> None:
        try:
            text = await brief.build_morning_brief()
            if text:
                await bot.send_message(cfg.owner_telegram_id, text)
        except Exception:
            log.exception("morning brief failed")

    # ---------- study (M9) ----------

    async def study_nudge() -> None:
        try:
            nudge = await brief.build_study_nudge()
            if nudge is None:
                return
            text, plan_id = nudge
            await bot.send_message(cfg.owner_telegram_id, text,
                                   reply_markup=keyboards.study_plan_kb(plan_id))
        except Exception:
            log.exception("study nudge failed")

    async def github_poll() -> None:
        try:
            await github_watch.poll(bot)
        except Exception:
            log.exception("github poll failed")

    async def weekly_review() -> None:
        try:
            text = await weekly.build_weekly_review()
            if text:
                for chunk in split_message(text):
                    await bot.send_message(cfg.owner_telegram_id, chunk)
        except Exception:
            log.exception("weekly review failed")

    async def review_card() -> None:
        """One card a day; the session continues from the buttons if more are due."""
        try:
            from inais.bot.routers.drills import send_card

            if not await send_card(bot, cfg.owner_telegram_id):
                log.debug("no review cards due today")
        except Exception:
            log.exception("daily review card failed")

    # ---------- growing brain (M11) ----------

    async def learning_cycle() -> None:
        await autonomy.scheduled_cycle(bot)

    async def curiosity_scout() -> None:
        if not cfg.learning_enabled:
            return
        try:
            await curiosity.scout()
        except Exception:
            log.exception("curiosity scouting failed")

    async def train_networks() -> None:
        if not cfg.nn_enabled or db.pool() is None:
            return
        try:
            log.info("nightly training: %s", await nn.train_all())
        except Exception:
            log.exception("nightly network training failed")

    scheduler.add_job(gmail_poll, "interval", seconds=cfg.gmail_poll_seconds,
                      id="gmail_poll", max_instances=1, coalesce=True)
    scheduler.add_job(binance_snapshot, "interval", hours=1, id="binance_snapshot",
                      max_instances=1, coalesce=True)
    scheduler.add_job(daily_summary, "cron", hour=cfg.daily_summary_hour, minute=0,
                      id="daily_summary")
    scheduler.add_job(nightly_reflection, "cron", hour=3, minute=0, id="nightly_reflection")
    scheduler.add_job(budget_check, "cron", hour=9, minute=5, id="budget_check")
    scheduler.add_job(token_health, "cron", day_of_week="sun", hour=8, minute=0, id="token_health")

    scheduler.add_job(reminder_tick, "interval", seconds=30, id="reminder_tick",
                      max_instances=1, coalesce=True)
    scheduler.add_job(morning_brief, "cron", hour=cfg.morning_brief_hour, minute=0,
                      id="morning_brief")
    scheduler.add_job(study_nudge, "cron", hour=cfg.study_nudge_hour, minute=0, id="study_nudge")
    scheduler.add_job(review_card, "cron", hour=cfg.review_card_hour, minute=0, id="review_card")
    scheduler.add_job(weekly_review, "cron", day_of_week=cfg.weekly_review_day,
                      hour=cfg.weekly_review_hour, minute=0, id="weekly_review")
    if cfg.github_enabled:
        scheduler.add_job(github_poll, "interval", minutes=cfg.github_poll_minutes,
                          id="github_poll", max_instances=1, coalesce=True)

    # The brain trains after reflection (which may add facts the scout reads) and learns
    # whenever the user has been quiet for a while.
    scheduler.add_job(learning_cycle, "interval", minutes=cfg.autonomy_interval_minutes,
                      id="learning_cycle", max_instances=1, coalesce=True)
    scheduler.add_job(curiosity_scout, "cron", hour=2, minute=30, id="curiosity_scout")
    scheduler.add_job(train_networks, "cron", hour=3, minute=30, id="train_networks")

    global _scheduler
    _scheduler = scheduler
    return scheduler
