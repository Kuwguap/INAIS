"""Meme intelligence — scheduler entrypoints. Thin on purpose: gating lives here, domain
logic lives in inais.memes.*. Every tick checks controls.is_paused() before touching the
network (books_watch precedent), so /pause silences the whole feature.
"""

from __future__ import annotations

import logging

from inais import controls, db
from inais.config import settings

log = logging.getLogger(__name__)


def _gated() -> bool:
    cfg = settings()
    return not cfg.meme_enabled or db.pool() is None or controls.is_paused()


async def scout_tick(bot) -> None:
    if _gated():
        return
    from inais.memes import scout, settle

    try:
        await scout.run_scout(bot)
    except Exception:
        log.exception("meme scout tick failed")
    try:
        await settle.run_settlement(bot)
    except Exception:
        log.exception("meme settlement failed")


async def watch_tick(bot) -> None:
    if _gated():
        return
    from inais.memes import watch

    try:
        await watch.run_watch(bot)
    except Exception:
        log.exception("meme watch tick failed")


async def research_poll(bot) -> None:
    """Deliver finished deep-research jobs (send FIRST, mark delivered AFTER — a crash in
    between re-delivers next tick; a rare duplicate beats a dropped report)."""
    if _gated():
        return
    from inais.integrations import memejobs
    from inais.textutil import split_message

    try:
        jobs = await memejobs.ready_undelivered()
    except Exception:
        log.exception("meme research poll failed")
        return
    for job in jobs:
        try:
            for chunk in split_message(memejobs.render_report(job)):
                await bot.send_message(job["requester_chat_id"], chunk,
                                       disable_web_page_preview=True)
            await memejobs.mark_delivered(str(job["id"]))
        except Exception:
            log.exception("delivery failed for meme job %s", job.get("id"))


async def reflect(bot) -> None:
    if _gated():
        return
    from inais.memes import learning

    try:
        await learning.reflect_patterns()
    except Exception:
        log.exception("meme reflection failed")
