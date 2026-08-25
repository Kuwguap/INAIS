"""Signal settlement — the outcomes that become the meme_signal head's training labels.

KNOWN LIMIT (documented + tested): the price path between polls is unobserved. If both the
stop and the target were crossed between polls, the signal settles on whichever level the
current poll shows — bounded by the 5-minute scout cadence. A failed price fetch NEVER
settles anything: the row stays open for the next poll.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from inais.config import settings
from inais.integrations import dexscreener
from inais.memes import store

log = logging.getLogger(__name__)


def settle_state(entry: float, stop: float, target: float, price: float,
                 age_hours: float, window_hours: float) -> str | None:
    """Pure. Level checks come BEFORE the window check so a final poll at the deadline can
    still settle a real win/loss instead of expiring it."""
    if price <= 0:
        return None
    if price >= target:
        return "win"
    if price <= stop:
        return "loss"
    if age_hours >= window_hours:
        return "expired"
    return None


async def run_settlement(bot) -> int:
    """Settle every open signal whose outcome is now known. Returns how many settled."""
    cfg = settings()
    signals = await store.open_signals()
    if not signals:
        return 0
    prices = await dexscreener.pairs_for_mints([s["mint"] for s in signals])
    now = datetime.now(UTC)
    settled = 0
    for sig in signals:
        pair = prices.get(sig["mint"])
        if pair is None or pair.price_usd is None:
            continue  # failed fetch: leave open, never expire on missing data
        age_hours = (now - sig["created_at"]).total_seconds() / 3600
        state = settle_state(float(sig["entry_price"]), float(sig["stop_price"]),
                             float(sig["target_price"]), pair.price_usd,
                             age_hours, cfg.meme_signal_window_hours)
        if state is None:
            continue
        await store.settle_signal(sig["id"], state, pair.price_usd)
        settled += 1
    if settled:
        log.info("settled %s meme signal(s)", settled)
    return settled
