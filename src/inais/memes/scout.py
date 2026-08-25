"""The scout: discover new Solana pairs, screen them hard, signal the survivors.

Cost discipline: DexScreener-side checks (liquidity, age) run BEFORE any RugCheck fetch, and
RugCheck runs before any LLM spend — the screen is ordered cheapest-first on purpose.
"""

from __future__ import annotations

import logging
import time

from inais.config import settings
from inais.integrations import dexscreener, rugcheck
from inais.memes import screener, signal, store

log = logging.getLogger(__name__)

MAX_SIGNALS_PER_TICK = 3


async def run_scout(bot) -> int:
    """One pass. Returns how many signals were created (suppressed included)."""
    cfg = settings()
    now_ms = int(time.time() * 1000)
    # Two discovery lanes merged, deduped by mint: the promo feed (latest_pairs) is often empty,
    # so the always-populated trending search keeps the scout fed. Both are read-only.
    seen_mints: set[str] = set()
    pairs: list[dexscreener.Pair] = []
    for pair in (await dexscreener.latest_pairs()) + (await dexscreener.trending_pairs(limit=20)):
        if pair.mint not in seen_mints:
            seen_mints.add(pair.mint)
            pairs.append(pair)
    if not pairs:
        return 0

    # dedupe + dex-side screen first (free); RugCheck only for pairs that survive it
    candidates: list[tuple[int, dexscreener.Pair]] = []
    for pair in pairs:
        token_id = await store.note_seen(pair)
        if token_id is None:
            continue  # already known (or no DB)
        reason = screener.hard_reject(
            pair, None, min_liquidity=cfg.meme_min_liquidity_usd,
            min_age_minutes=cfg.meme_min_age_minutes,
            max_top10_pct=cfg.meme_max_top10_holder_pct, now_ms=now_ms)
        if reason is not None and reason != "no_rug_report":
            await store.mark_rejected(token_id, reason, pair, None)
            continue
        candidates.append((token_id, pair))

    # busiest first — the cap below means the rest wait for the next tick
    candidates.sort(key=lambda tp: tp[1].volume_h24 or 0, reverse=True)

    created = 0
    for token_id, pair in candidates:
        if created >= MAX_SIGNALS_PER_TICK:
            break
        report = await rugcheck.token_report(pair.mint)
        reason = screener.hard_reject(
            pair, report, min_liquidity=cfg.meme_min_liquidity_usd,
            min_age_minutes=cfg.meme_min_age_minutes,
            max_top10_pct=cfg.meme_max_top10_holder_pct, now_ms=now_ms)
        if reason is not None:
            await store.mark_rejected(token_id, reason, pair, report)
            continue
        flags = screener.soft_flags(pair, report)
        try:
            if await signal.create_and_send(bot, token_id, pair, report, flags) is not None:
                created += 1
        except Exception:
            log.exception("signal generation failed for %s", pair.symbol)
            await store.mark_screened(token_id, pair, report)
    if created:
        log.info("meme scout created %s signal(s)", created)
    return created
