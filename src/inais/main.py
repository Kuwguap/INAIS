"""Entrypoint.

RUN_MODE=local → long polling (dev; no tunnel needed).
RUN_MODE=web   → aiohttp webhook server for Render: ACK 200 instantly, process updates
                 in background tasks (Telegram redelivers slow ACKs — see AGENTS.md).
"""

from __future__ import annotations

import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.types import Update
from aiohttp import web

import inais.agents  # noqa: F401  — registers agents/toolsets
from inais import controls, db
from inais.bot import create_dispatcher
from inais.config import settings
from inais.integrations import binance
from inais.jobs import schedules

log = logging.getLogger("inais")

_bg_tasks: set[asyncio.Task] = set()


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


async def _startup(bot: Bot) -> None:
    await db.init_pool()
    cfg = settings()
    log.info(
        "INAIS starting (mode=%s, db=%s, brain=%s, binance=%s)",
        cfg.run_mode, cfg.db_enabled, cfg.brain_enabled, cfg.binance_enabled,
    )
    if cfg.db_enabled and db.pool() is None:
        # the bot still runs, but almost every feature needs storage — say so where it is seen
        _spawn(bot.send_message(
            cfg.owner_telegram_id,
            "⚠️ Started WITHOUT a database — memory, email, tasks and tracking are off.\n\n"
            f"{db.last_error()}\n\n"
            "Fix SUPABASE_DB_URL and redeploy; /status shows the state."))

    scheduler = schedules.setup(bot)
    scheduler.start()
    await controls.load_flags()
    if controls.is_paused():
        scheduler.pause()
        log.warning("INAIS is PAUSED (persisted flag) — background jobs are held. /resume to start.")


async def _shutdown() -> None:
    await binance.close()
    await db.close_pool()


async def run_local(bot: Bot, dp: Dispatcher) -> None:
    await _startup(bot)
    await bot.delete_webhook(drop_pending_updates=False)
    try:
        await dp.start_polling(bot)
    finally:
        await _shutdown()


async def run_web(bot: Bot, dp: Dispatcher) -> None:
    cfg = settings()
    await _startup(bot)

    if not cfg.render_external_url:
        log.error("RUN_MODE=web but RENDER_EXTERNAL_URL is not set")
        sys.exit(1)
    webhook_url = f"{cfg.render_external_url.rstrip('/')}/wh/{cfg.webhook_path}"
    webhook_ok = True
    try:
        await bot.set_webhook(
            url=webhook_url,
            secret_token=cfg.webhook_secret,
            allowed_updates=["message", "callback_query"],
        )
        log.info("webhook set: %s", webhook_url)
    except Exception as e:
        # A rejected webhook used to exit the process, so the platform restarted it and
        # tried again forever. Long polling reaches the same Telegram either way — take it,
        # stay up, and say what happened.
        webhook_ok = False
        log.error("set_webhook failed (%s) — falling back to long polling", e)

    async def handle_webhook(request: web.Request) -> web.Response:
        # same property that was handed to setWebhook, so the two can never disagree
        if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != cfg.webhook_secret:
            return web.Response(status=403)
        try:
            data = await request.json()
            update = Update.model_validate(data, context={"bot": bot})
        except Exception:
            log.exception("bad webhook payload")
            return web.Response(status=200)  # never make Telegram retry garbage
        _spawn(dp.feed_update(bot, update))  # ACK immediately, process in background
        return web.Response(text="ok")

    async def healthz(request: web.Request) -> web.Response:
        return web.Response(text="ok")

    async def oauth_callback(request: web.Request) -> web.Response:
        """Public endpoint. Everything here is untrusted until the state signature checks out."""
        from inais.integrations import google_oauth

        if not google_oauth.verify_state(request.query.get("state", "")):
            log.warning("oauth callback with a bad or expired state")
            return web.Response(status=400, text="Expired or invalid link. Send /connect again.")
        error = request.query.get("error")
        if error:
            return web.Response(text=f"Google returned: {error}. You can close this tab.")
        code = request.query.get("code", "")
        if not code:
            return web.Response(status=400, text="No authorization code. Send /connect again.")
        try:
            email, refresh_token = await google_oauth.exchange(code)
            await google_oauth.store_account(email, refresh_token)
        except Exception as e:
            log.exception("oauth exchange failed")
            _spawn(bot.send_message(cfg.owner_telegram_id, f"❌ Gmail connection failed: {e}"))
            return web.Response(status=500, text=f"Connection failed: {e}")
        _spawn(bot.send_message(
            cfg.owner_telegram_id,
            f"✅ Connected {email}. I'll baseline the mailbox on the next poll and start "
            f"flagging important mail."))
        return web.Response(
            content_type="text/html",
            text=f"<h2>Connected {email}</h2><p>Done — - you can close this tab and go back "
                 f"to Telegram.</p>")

    app = web.Application()
    app.router.add_post(f"/wh/{cfg.webhook_path}", handle_webhook)
    app.router.add_get("/healthz", healthz)
    app.router.add_get("/oauth/callback", oauth_callback)
    app.router.add_get("/", healthz)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=cfg.port)
    await site.start()
    log.info("listening on :%s", cfg.port)
    try:
        if webhook_ok:
            await asyncio.Event().wait()  # updates arrive on the webhook route
        else:
            # keep serving /healthz (the platform needs the port bound) and take updates
            # by polling instead, so a webhook problem degrades rather than kills the bot
            _spawn(bot.send_message(
                cfg.owner_telegram_id,
                "⚠️ Couldn't register the webhook, so I'm running on long polling instead.\n"
                "I still work normally. Check TELEGRAM_WEBHOOK_SECRET "
                "(letters, digits, _ and - only) and RENDER_EXTERNAL_URL."))
            await bot.delete_webhook(drop_pending_updates=False)
            await dp.start_polling(bot)
    finally:
        await runner.cleanup()
        await _shutdown()


def main() -> None:
    cfg = settings()
    logging.basicConfig(
        level=getattr(logging, cfg.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    bot = Bot(token=cfg.telegram_bot_token)
    dp = create_dispatcher()
    if cfg.run_mode == "web":
        asyncio.run(run_web(bot, dp))
    else:
        asyncio.run(run_local(bot, dp))


if __name__ == "__main__":
    main()
