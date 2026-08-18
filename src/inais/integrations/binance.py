"""Read-only Binance client. SECURITY: the API key must have 'Enable Reading' only.

Regular polling (hourly snapshot) also keeps the key alive past Binance's 30-day
inactivity auto-delete for non-whitelisted keys.
"""

from __future__ import annotations

import json
import logging
import time

from binance import AsyncClient

from inais import db
from inais.config import settings

log = logging.getLogger(__name__)

_client: AsyncClient | None = None

STABLES = {"USDT", "USDC", "FDUSD", "DAI", "TUSD", "BUSD"}
DUST_USD = 1.0  # hide balances worth less than this


async def client() -> AsyncClient:
    global _client
    if _client is None:
        cfg = settings()
        _client = await AsyncClient.create(cfg.binance_api_key, cfg.binance_api_secret)
    return _client


async def close() -> None:
    global _client
    if _client is not None:
        await _client.close_connection()
        _client = None


async def _price_map(c: AsyncClient) -> dict[str, float]:
    tickers = await c.get_all_tickers()
    return {t["symbol"]: float(t["price"]) for t in tickers}


def _usd_value(asset: str, qty: float, prices: dict[str, float]) -> float | None:
    if asset in STABLES:
        return qty
    if f"{asset}USDT" in prices:
        return qty * prices[f"{asset}USDT"]
    if f"{asset}BTC" in prices and "BTCUSDT" in prices:
        return qty * prices[f"{asset}BTC"] * prices["BTCUSDT"]
    return None


async def portfolio() -> dict:
    """Non-dust balances with USD values. {'assets': [...], 'total_usdt': float}"""
    c = await client()
    acct = await c.get_account()
    prices = await _price_map(c)
    assets = []
    total = 0.0
    for b in acct.get("balances", []):
        qty = float(b["free"]) + float(b["locked"])
        if qty <= 0:
            continue
        val = _usd_value(b["asset"], qty, prices)
        if val is not None and val < DUST_USD:
            continue
        assets.append({"asset": b["asset"], "qty": qty, "usd": round(val, 2) if val is not None else None})
        total += val or 0.0
    assets.sort(key=lambda a: -(a["usd"] or 0))
    return {"assets": assets, "total_usdt": round(total, 2)}


async def recent_trades(limit_per_symbol: int = 10) -> list[dict]:
    c = await client()
    out: list[dict] = []
    for symbol in settings().symbols:
        try:
            trades = await c.get_my_trades(symbol=symbol, limit=limit_per_symbol)
        except Exception as e:
            log.warning("myTrades(%s) failed: %s", symbol, e)
            continue
        for t in trades:
            out.append({
                "symbol": symbol,
                "side": "buy" if t.get("isBuyer") else "sell",
                "qty": float(t["qty"]),
                "price": float(t["price"]),
                "quote_qty": float(t.get("quoteQty", 0)),
                "time": int(t["time"]),
            })
    out.sort(key=lambda t: -t["time"])
    return out


async def transfers(days: int = 30) -> dict:
    c = await client()
    start_ms = int((time.time() - days * 86400) * 1000)
    deposits = await c.get_deposit_history(startTime=start_ms)
    withdrawals = await c.get_withdraw_history(startTime=start_ms)
    return {
        "deposits": [
            {"coin": d.get("coin"), "amount": float(d.get("amount", 0)), "time": d.get("insertTime")}
            for d in deposits or []
        ],
        "withdrawals": [
            {"coin": w.get("coin"), "amount": float(w.get("amount", 0)), "time": w.get("applyTime")}
            for w in withdrawals or []
        ],
    }


async def take_snapshot() -> float | None:
    """Hourly job: store balances + total. Returns total_usdt."""
    if not settings().binance_enabled:
        return None
    pf = await portfolio()
    p = db.pool()
    if p is not None:
        await p.execute(
            "insert into finance_snapshots (balances, total_usdt) values ($1::jsonb, $2)",
            json.dumps(pf["assets"]), pf["total_usdt"],
        )
    return pf["total_usdt"]


async def total_change_24h() -> tuple[float | None, float | None]:
    """(current_total, total_roughly_24h_ago) from stored snapshots."""
    p = db.pool()
    if p is None:
        return None, None
    now_row = await p.fetchrow("select total_usdt from finance_snapshots order by ts desc limit 1")
    old_row = await p.fetchrow(
        "select total_usdt from finance_snapshots where ts <= now() - interval '23 hours'"
        " order by ts desc limit 1",
    )
    return (
        float(now_row["total_usdt"]) if now_row else None,
        float(old_row["total_usdt"]) if old_row else None,
    )
