"""GitHub polling job: notify once per new item, never again."""

from __future__ import annotations

import logging

from inais import db
from inais.config import settings
from inais.integrations import github
from inais.textutil import split_message

log = logging.getLogger(__name__)

MAX_PER_NOTIFICATION = 8


async def unseen(items: list[github.GitHubItem]) -> list[github.GitHubItem]:
    """Filter to items we have never notified about. Claims them in the same step."""
    p = db.pool()
    if p is None or not items:
        return []
    fresh: list[github.GitHubItem] = []
    for item in items:
        # unique index on event_key makes this the dedupe: no row inserted = already sent
        row = await p.fetchrow(
            "insert into github_events (kind, event_key, repo, title, url)"
            " values ($1, $2, $3, $4, $5)"
            " on conflict (event_key) do nothing returning id",
            item.kind, item.key, item.repo, item.title[:500], item.url,
        )
        if row is not None:
            fresh.append(item)
    return fresh


async def poll(bot) -> int:
    """Returns how many new items were announced."""
    cfg = settings()
    if not cfg.github_enabled or db.pool() is None:
        return 0
    try:
        items = await github.fetch_all()
    except github.GitHubAuthError as e:
        log.warning("github auth failed: %s", e)
        await bot.send_message(
            cfg.owner_telegram_id,
            f"⚠️ GitHub token rejected ({e}). Check GITHUB_TOKEN — it needs read access only.")
        return 0
    except Exception:
        log.exception("github poll failed")
        return 0

    fresh = await unseen(items)
    if not fresh:
        return 0
    body = github.render_digest(fresh[:MAX_PER_NOTIFICATION])
    extra = len(fresh) - MAX_PER_NOTIFICATION
    if extra > 0:
        body += f"\n\n…and {extra} more — /github for the full list."
    for chunk in split_message(f"🐙 GitHub\n\n{body}"):
        await bot.send_message(cfg.owner_telegram_id, chunk, disable_web_page_preview=True)
    log.info("github: notified about %s new item(s)", len(fresh))
    return len(fresh)
