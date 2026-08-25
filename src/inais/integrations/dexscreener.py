"""DexScreener client — read-only market data for the Solana meme-coin scout.

Keyless public API. Every response field — token names, symbols, socials, descriptions — is
chosen by token deployers: attacker-controlled DATA, rendered as text, never followed as
instructions. This module only ever GETs; it cannot trade, and nothing here may grow a POST.

Rate limits (documented by DexScreener): profiles/boosts lanes ~60 req/min, pairs/search/tokens
~300 req/min. Failures degrade to empty results — a scout tick must never crash on a 429.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import aiohttp

log = logging.getLogger(__name__)

API = "https://api.dexscreener.com"
TIMEOUT = aiohttp.ClientTimeout(total=15)
BATCH_MINTS = 30                 # documented cap per /tokens call
CHAIN = "solana"


class DexScreenerError(Exception):
    """Unexpected response shape or persistent failure."""


@dataclass
class Pair:
    mint: str
    pair_address: str
    symbol: str
    name: str
    price_usd: float | None
    liquidity_usd: float | None
    fdv_usd: float | None
    volume_h24: float | None
    volume_h1: float | None
    change_m5: float | None
    change_h1: float | None
    change_h24: float | None
    buys_h1: int
    sells_h1: int
    created_at_ms: int | None
    has_socials: bool
    has_website: bool

    def snapshot(self) -> dict:
        """Compact dict for the meme_tokens.dex jsonb column."""
        return {
            "price_usd": self.price_usd, "liquidity_usd": self.liquidity_usd,
            "fdv_usd": self.fdv_usd, "volume_h24": self.volume_h24,
            "change_h1": self.change_h1, "change_h24": self.change_h24,
            "buys_h1": self.buys_h1, "sells_h1": self.sells_h1,
            "created_at_ms": self.created_at_ms,
        }


def _num(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_pair(raw: dict) -> Pair | None:
    """One DexScreener pair dict → Pair. Tolerant of missing keys; None when unusable."""
    if not isinstance(raw, dict):
        return None
    base = raw.get("baseToken") or {}
    mint = str(base.get("address") or "").strip()
    pair_address = str(raw.get("pairAddress") or "").strip()
    if not mint or not pair_address:
        return None
    txns_h1 = (raw.get("txns") or {}).get("h1") or {}
    volume = raw.get("volume") or {}
    change = raw.get("priceChange") or {}
    info = raw.get("info") or {}
    liquidity = raw.get("liquidity") or {}
    created = raw.get("pairCreatedAt")
    return Pair(
        mint=mint,
        pair_address=pair_address,
        symbol=str(base.get("symbol") or "?")[:20],
        name=str(base.get("name") or "?")[:80],
        price_usd=_num(raw.get("priceUsd")),
        liquidity_usd=_num(liquidity.get("usd")),
        fdv_usd=_num(raw.get("fdv")),
        volume_h24=_num(volume.get("h24")),
        volume_h1=_num(volume.get("h1")),
        change_m5=_num(change.get("m5")),
        change_h1=_num(change.get("h1")),
        change_h24=_num(change.get("h24")),
        buys_h1=int(txns_h1.get("buys") or 0),
        sells_h1=int(txns_h1.get("sells") or 0),
        created_at_ms=int(created) if isinstance(created, (int, float)) else None,
        has_socials=bool(info.get("socials")),
        has_website=bool(info.get("websites")),
    )


def age_minutes(pair: Pair, now_ms: int) -> float | None:
    if pair.created_at_ms is None:
        return None
    return max(0.0, (now_ms - pair.created_at_ms) / 60000.0)


async def _get(session: aiohttp.ClientSession, path: str, params: dict | None = None):
    """GET with the github.py degrade posture: rate limits and 5xx become empty results."""
    async with session.get(f"{API}{path}", params=params) as resp:
        if resp.status == 429:
            log.warning("dexscreener rate limited on %s", path)
            return None
        if resp.status >= 400:
            log.warning("dexscreener returned %s for %s", resp.status, path)
            return None
        return await resp.json(content_type=None)


def _pairs_from(data) -> list[Pair]:
    if data is None:
        return []
    raw_pairs = data if isinstance(data, list) else (data.get("pairs") or [])
    out = []
    for raw in raw_pairs:
        p = parse_pair(raw)
        if p is not None and str(raw.get("chainId", CHAIN)) == CHAIN:
            out.append(p)
    return out


async def latest_pairs() -> list[Pair]:
    """Freshly-profiled/boosted Solana tokens → their pairs. The scout's discovery lane."""
    async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
        mints: list[str] = []
        for path in ("/token-profiles/latest/v1", "/token-boosts/latest/v1"):  # VERIFY at implementation
            data = await _get(session, path)
            for item in (data or []):
                if isinstance(item, dict) and item.get("chainId") == CHAIN:
                    addr = str(item.get("tokenAddress") or "").strip()
                    if addr and addr not in mints:
                        mints.append(addr)
        if not mints:
            return []
        return await _pairs_for_mints_in(session, mints[:BATCH_MINTS * 2])


async def search_pairs(query: str) -> list[Pair]:
    async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
        data = await _get(session, "/latest/dex/search", {"q": query})
        return _pairs_from(data)


# Broad terms whose search results are dominated by active Solana meme pairs. The /latest/dex
# /search endpoint is the reliable, always-populated lane (unlike the promo-only profiles feed).
TRENDING_QUERIES = ("SOL", "pump", "bonk", "wif")
# Majors/quotes that are not meme coins — kept out of the trending list.
_NOT_MEMES = {"SOL", "WSOL", "USDC", "USDT", "USDH", "JLP", "JUP", "JITOSOL", "MSOL", "BSOL", "ETH", "BTC"}


def rank_trending(pairs: list[Pair], limit: int) -> list[Pair]:
    """Pure: dedupe by mint (keep deepest liquidity), drop majors, rank by 24h volume."""
    best: dict[str, Pair] = {}
    for p in pairs:
        if p.symbol.upper() in _NOT_MEMES:
            continue
        cur = best.get(p.mint)
        if cur is None or (p.liquidity_usd or 0) > (cur.liquidity_usd or 0):
            best[p.mint] = p
    return sorted(best.values(), key=lambda p: p.volume_h24 or 0, reverse=True)[:limit]


async def trending_pairs(limit: int = 12) -> list[Pair]:
    """What's actually moving on Solana right now, by 24h volume — a live 'show me coins'
    list independent of the scout pipeline. Merges a few broad searches; degrades to []."""
    collected: list[Pair] = []
    async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
        for q in TRENDING_QUERIES:
            collected.extend(_pairs_from(await _get(session, "/latest/dex/search", {"q": q})))
    return rank_trending(collected, limit)


async def get_pair(pair_address: str) -> Pair | None:
    async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
        data = await _get(session, f"/latest/dex/pairs/{CHAIN}/{pair_address}")  # VERIFY at implementation
        pairs = _pairs_from(data)
        return pairs[0] if pairs else None


async def _pairs_for_mints_in(session: aiohttp.ClientSession, mints: list[str]) -> list[Pair]:
    out: list[Pair] = []
    for i in range(0, len(mints), BATCH_MINTS):
        chunk = mints[i:i + BATCH_MINTS]
        data = await _get(session, f"/tokens/v1/{CHAIN}/{','.join(chunk)}")  # VERIFY at implementation
        out.extend(_pairs_from(data))
    return out


async def pairs_for_mints(mints: list[str]) -> dict[str, Pair]:
    """Best pair per mint (highest liquidity) — the ONE batched call per watch tick."""
    if not mints:
        return {}
    async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
        pairs = await _pairs_for_mints_in(session, list(dict.fromkeys(mints)))
    best: dict[str, Pair] = {}
    for p in pairs:
        cur = best.get(p.mint)
        if cur is None or (p.liquidity_usd or 0) > (cur.liquidity_usd or 0):
            best[p.mint] = p
    return best
