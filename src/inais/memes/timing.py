"""Trading-session awareness — WHEN the meme market is actually active.

These are activity/liquidity windows (observable market structure), not advice: Solana meme
volume follows US waking hours, launches cluster into them, and thin sessions move on less
size. Shown on every signal card and in /memes so timing context is always in view.
"""

from __future__ import annotations

from datetime import datetime

# UTC hours. The meme market's center of gravity is the US day: volume builds from the US
# East-Coast morning and dies off after the US evening.
PEAK_START_UTC = 13   # ~9am New York — launches + volume ramp
PEAK_END_UTC = 22     # ~6pm New York — activity tails off


def trading_session(now_utc: datetime) -> tuple[str, bool]:
    """(label, is_prime). is_prime = weekday US session — the deepest, fastest window."""
    h, weekend = now_utc.hour, now_utc.weekday() >= 5
    if PEAK_START_UTC <= h < PEAK_END_UTC:
        if weekend:
            return "US hours but weekend — decent activity, thinner and easier to manipulate", False
        return "US session — peak volume and launch window", True
    if 6 <= h < PEAK_START_UTC:
        return "EU morning — warming up toward the US open", False
    return "Asia/overnight — thinnest liquidity, moves exaggerate both ways", False


def session_line(now_utc: datetime) -> str:
    label, prime = trading_session(now_utc)
    return f"{'🟢' if prime else '🕒'} {label}"
