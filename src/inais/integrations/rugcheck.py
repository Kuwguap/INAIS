"""RugCheck client — read-only Solana token risk reports for the scam screener.

Keyless public API. Report contents (risk names, descriptions) are external DATA rendered as
text, never instructions. GET only; a missing or failed report means the screener FAILS CLOSED
(the token is rejected), so this client returning None is safe.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import aiohttp

log = logging.getLogger(__name__)

API = "https://api.rugcheck.xyz/v1"
TIMEOUT = aiohttp.ClientTimeout(total=15)


class RugCheckError(Exception):
    pass


@dataclass
class RugReport:
    score: float | None                 # RugCheck risk score (higher = riskier)
    mint_authority_active: bool
    freeze_authority_active: bool
    lp_locked_pct: float | None
    top10_holder_pct: float | None
    holder_count: int | None
    risks: list[str] = field(default_factory=list)

    def snapshot(self) -> dict:
        return {
            "score": self.score,
            "mint_authority_active": self.mint_authority_active,
            "freeze_authority_active": self.freeze_authority_active,
            "lp_locked_pct": self.lp_locked_pct,
            "top10_holder_pct": self.top10_holder_pct,
            "holder_count": self.holder_count,
            "risks": self.risks[:10],
        }


def _num(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_report(raw: dict) -> RugReport:
    """RugCheck summary JSON → RugReport. Tolerant: missing keys become conservative defaults
    (authorities assumed ACTIVE when unknown, so unknowns screen as risky, not safe)."""
    if not isinstance(raw, dict):
        raw = {}
    markets = raw.get("markets") or []
    lp_locked = None
    for m in markets:
        pct = _num((m.get("lp") or {}).get("lpLockedPct"))
        if pct is not None:
            lp_locked = max(lp_locked or 0.0, pct)
    top10 = None
    holders = raw.get("topHolders") or []
    if holders:
        top10 = sum(_num(h.get("pct")) or 0.0 for h in holders[:10])
    risks = [str(r.get("name", ""))[:80] for r in (raw.get("risks") or []) if isinstance(r, dict)]
    # Key present with null = authority renounced (safe). Key ABSENT = unknown — and unknown
    # must screen as risky, so it reads as active. "in raw" is the load-bearing distinction.
    mint_active = raw["mintAuthority"] is not None if "mintAuthority" in raw else True
    freeze_active = raw["freezeAuthority"] is not None if "freezeAuthority" in raw else True
    return RugReport(
        score=_num(raw.get("score_normalised", raw.get("score"))),
        mint_authority_active=mint_active,
        freeze_authority_active=freeze_active,
        lp_locked_pct=lp_locked,
        top10_holder_pct=top10,
        holder_count=int(raw["totalHolders"]) if isinstance(raw.get("totalHolders"), (int, float)) else None,
        risks=[r for r in risks if r],
    )


async def token_report(mint: str) -> RugReport | None:
    """Fetch one token's risk report. None on any failure — callers treat that as reject."""
    try:
        async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
            async with session.get(f"{API}/tokens/{mint}/report") as resp:  # VERIFY at implementation
                if resp.status >= 400:
                    log.info("rugcheck returned %s for %s", resp.status, mint[:12])
                    return None
                raw = await resp.json(content_type=None)
    except Exception:
        log.warning("rugcheck fetch failed for %s", mint[:12])
        return None
    return parse_report(raw)
