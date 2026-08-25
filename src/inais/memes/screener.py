"""Hard scam/quality screen — pure rules that kill obvious rugs before any LLM spend.

FAILS CLOSED: a missing RugCheck report is a rejection, not a pass. A screener that fails
open is not a screener.
"""

from __future__ import annotations

from inais.integrations.dexscreener import Pair, age_minutes
from inais.integrations.rugcheck import RugReport

RUG_SCORE_DANGER = 60.0   # RugCheck normalised score above this = reject
MIN_LP_LOCKED_PCT = 50.0


def hard_reject(pair: Pair, report: RugReport | None, *, min_liquidity: float,
                min_age_minutes: float, max_top10_pct: float, now_ms: int) -> str | None:
    """Reject reason string, or None when the candidate survives every rule."""
    if (pair.liquidity_usd or 0) < min_liquidity:
        return "low_liquidity"
    age = age_minutes(pair, now_ms)
    if age is None or age < min_age_minutes:
        return "too_young"
    if report is None:
        return "no_rug_report"
    if report.mint_authority_active:
        return "mint_authority"
    if report.freeze_authority_active:
        return "freeze_authority"
    if (report.lp_locked_pct or 0) < MIN_LP_LOCKED_PCT:
        return "lp_unlocked"
    if report.top10_holder_pct is not None and report.top10_holder_pct > max_top10_pct:
        return "holder_concentration"
    if report.score is not None and report.score > RUG_SCORE_DANGER:
        return "rug_score"
    return None


def soft_flags(pair: Pair, report: RugReport | None) -> list[str]:
    """Non-fatal cautions surfaced to the analyst prompt and the signal card."""
    flags: list[str] = []
    if not (pair.has_socials or pair.has_website):
        flags.append("no socials/website")
    if pair.created_at_ms is not None and (pair.volume_h1 or 0) == 0:
        flags.append("no volume last hour")
    if report is not None:
        if report.holder_count is not None and report.holder_count < 200:
            flags.append(f"only {report.holder_count} holders")
        flags.extend(f"rugcheck: {r}" for r in report.risks[:3])
    total = pair.buys_h1 + pair.sells_h1
    if total >= 20 and pair.sells_h1 > pair.buys_h1 * 2:
        flags.append("heavy selling")
    return flags
