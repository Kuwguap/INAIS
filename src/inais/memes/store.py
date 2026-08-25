"""All SQL for the meme feature. Raw asyncpg, no ORM (project convention).

Nothing here moves money: 'real' positions are the owner's own trades, logged by them; 'paper'
positions are simulated. Scraped snapshots go into jsonb as DATA, never instructions.
"""

from __future__ import annotations

import json
import logging

from inais import db
from inais.config import settings
from inais.integrations.dexscreener import Pair
from inais.integrations.rugcheck import RugReport

log = logging.getLogger(__name__)


def _pool():
    return db.pool()


# ---------- tokens (scout ledger) ----------

async def note_seen(pair: Pair) -> int | None:
    """Record a token the first time it is seen. Returns the new id, or None when already
    known (or no database) — the scout's dedupe."""
    p = _pool()
    if p is None:
        return None
    row = await p.fetchrow(
        "insert into meme_tokens (mint, pair_address, symbol, name)"
        " values ($1, $2, $3, $4) on conflict (mint) do nothing returning id",
        pair.mint, pair.pair_address, pair.symbol, pair.name)
    return row["id"] if row else None


async def mark_rejected(token_id: int, reason: str, pair: Pair,
                        report: RugReport | None) -> None:
    await _pool().execute(
        "update meme_tokens set status = 'rejected', reject_reason = $2,"
        " dex = $3::jsonb, risk = $4::jsonb, last_checked_at = now() where id = $1",
        token_id, reason[:80], json.dumps(pair.snapshot()),
        json.dumps(report.snapshot()) if report else None)


async def mark_screened(token_id: int, pair: Pair, report: RugReport | None,
                        signaled: bool = False) -> None:
    await _pool().execute(
        "update meme_tokens set status = $2, dex = $3::jsonb, risk = $4::jsonb,"
        " last_checked_at = now() where id = $1",
        token_id, "signaled" if signaled else "screened",
        json.dumps(pair.snapshot()), json.dumps(report.snapshot()) if report else None)


async def scout_stats() -> dict:
    p = _pool()
    if p is None:
        return {}
    row = await p.fetchrow(
        "select count(*) as seen,"
        " count(*) filter (where status = 'rejected') as rejected,"
        " count(*) filter (where status = 'signaled') as signaled,"
        " count(*) filter (where first_seen_at >= current_date) as seen_today"
        " from meme_tokens")
    return dict(row) if row else {}


# ---------- signals ----------

async def insert_signal(token_id: int, pair: Pair, *, thesis: str, confidence: float,
                        entry: float, stop: float, target: float,
                        features: list[float], feature_version: int,
                        nn_score: float | None, suppressed: bool) -> int:
    row = await _pool().fetchrow(
        "insert into meme_signals (token_id, mint, pair_address, symbol, thesis, confidence,"
        " entry_price, stop_price, target_price, price_at_signal, liquidity_at_signal,"
        " features, feature_version, nn_score, suppressed)"
        " values ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15) returning id",
        token_id, pair.mint, pair.pair_address, pair.symbol, thesis[:2000], confidence,
        entry, stop, target, pair.price_usd, pair.liquidity_usd,
        [float(x) for x in features], feature_version, nn_score, suppressed)
    return row["id"]


async def set_signal_message(signal_id: int, message_id: int) -> None:
    await _pool().execute(
        "update meme_signals set message_id = $2 where id = $1", signal_id, message_id)


async def get_signal(signal_id: int) -> dict | None:
    p = _pool()
    if p is None:
        return None
    row = await p.fetchrow("select * from meme_signals where id = $1", signal_id)
    return dict(row) if row else None


async def open_signals(limit: int = 50) -> list[dict]:
    """Unsettled signals (suppressed included — they settle and train too)."""
    rows = await _pool().fetch(
        "select id, mint, symbol, entry_price, stop_price, target_price, created_at"
        " from meme_signals where status = 'open' order by created_at limit $1", limit)
    return [dict(r) for r in rows]


async def settle_signal(signal_id: int, status: str, settle_price: float) -> None:
    if status not in ("win", "loss", "expired"):
        return
    await _pool().execute(
        "update meme_signals set status = $2, settle_price = $3, settled_at = now()"
        " where id = $1 and status = 'open'", signal_id, status, settle_price)


async def signals_today(include_suppressed: bool = True) -> int:
    p = _pool()
    if p is None:
        return 0
    extra = "" if include_suppressed else " and not suppressed"
    return int(await p.fetchval(
        f"select count(*) from meme_signals where created_at >= current_date{extra}") or 0)


async def recent_signals(limit: int = 8) -> list[dict]:
    p = _pool()
    if p is None:
        return []
    rows = await p.fetch(
        "select id, symbol, confidence, status, suppressed, nn_score, created_at,"
        "       price_at_signal, entry_price, stop_price, target_price, settle_price,"
        "       liquidity_at_signal"
        " from meme_signals order by created_at desc limit $1", limit)
    return [dict(r) for r in rows]


async def unharvested_settled(feature_version: int, limit: int = 25) -> list[dict]:
    rows = await _pool().fetch(
        "select id, thesis, status, entry_price, settle_price, features"
        " from meme_signals where status <> 'open' and not harvested"
        "   and feature_version = $1 limit $2", feature_version, limit)
    return [dict(r) for r in rows]


async def mark_harvested(signal_id: int) -> None:
    await _pool().execute(
        "update meme_signals set harvested = true where id = $1", signal_id)


# ---------- positions ----------

async def open_position(*, signal_id: int | None, token_id: int, mint: str,
                        pair_address: str | None, symbol: str, kind: str,
                        entry_price: float, size_usd: float,
                        stop: float | None, target: float | None,
                        liquidity: float | None) -> int:
    row = await _pool().fetchrow(
        "insert into meme_positions (signal_id, token_id, mint, pair_address, symbol, kind,"
        " entry_price, size_usd, stop_price, target_price, peak_price, last_price,"
        " liquidity_at_entry, last_liquidity_usd)"
        " values ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$7,$7,$11,$11) returning id",
        signal_id, token_id, mint, pair_address, symbol, kind,
        entry_price, size_usd, stop, target, liquidity)
    return row["id"]


async def open_positions() -> list[dict]:
    p = _pool()
    if p is None:
        return []
    rows = await p.fetch("select * from meme_positions where status = 'open' order by opened_at")
    return [dict(r) for r in rows]


async def open_position_count() -> int:
    p = _pool()
    if p is None:
        return 0
    return int(await p.fetchval(
        "select count(*) from meme_positions where status = 'open'") or 0)


async def update_position_marks(position_id: int, price: float, liquidity: float | None,
                                peak: float, alert_state: dict) -> None:
    await _pool().execute(
        "update meme_positions set last_price = $2, last_liquidity_usd = $3, peak_price = $4,"
        " alert_state = $5::jsonb where id = $1",
        position_id, price, liquidity, peak, json.dumps(alert_state))


async def close_position(position_id: int, *, exit_price: float, reason: str) -> dict | None:
    """Close once, PnL computed in SQL. Returns the closed row, or None if already closed."""
    row = await _pool().fetchrow(
        "update meme_positions set status = 'closed', close_reason = $3, exit_price = $2,"
        " pnl_pct = case when entry_price > 0 then ($2 - entry_price) / entry_price * 100 end,"
        " pnl_usd = case when entry_price > 0"
        "   then ($2 - entry_price) / entry_price * size_usd end,"
        " closed_at = now()"
        " where id = $1 and status = 'open' returning *",
        position_id, exit_price, reason[:30])
    return dict(row) if row else None


async def paper_report() -> dict:
    p = _pool()
    if p is None:
        return {}
    row = await p.fetchrow(
        "select coalesce(sum(pnl_usd) filter (where status = 'closed'), 0) as realized,"
        " count(*) filter (where status = 'closed') as closed,"
        " count(*) filter (where status = 'closed' and pnl_usd > 0) as wins,"
        " coalesce(sum(size_usd) filter (where status = 'open'), 0) as exposure,"
        " count(*) filter (where status = 'open') as open"
        " from meme_positions where kind = 'paper'")
    out = dict(row) if row else {}
    out["bankroll"] = settings().meme_paper_bankroll + float(out.get("realized") or 0)
    return out


async def stats() -> dict:
    p = _pool()
    if p is None:
        return {}
    row = await p.fetchrow(
        "select count(*) as signals,"
        " count(*) filter (where status = 'win') as wins,"
        " count(*) filter (where status = 'loss') as losses,"
        " count(*) filter (where status = 'expired') as expired,"
        " count(*) filter (where suppressed) as suppressed,"
        " count(*) filter (where status = 'open') as open"
        " from meme_signals")
    return dict(row) if row else {}
