"""Finance agent — read-only Binance data + planning. Never trades or moves funds."""

from __future__ import annotations

import datetime as dt
import logging

from inais.config import settings
from inais.integrations import binance
from inais.orchestrator.registry import AgentDef, Tool, ToolContext, register_agent

log = logging.getLogger(__name__)

PROMPT = """## Your current role: finance agent
You analyze the user's crypto portfolio using READ-ONLY Binance data and help them plan.
- You cannot trade, transfer or withdraw — and you never suggest you can.
- You are not a licensed financial advisor: frame analysis as information, not directives.
- Prices/values come from tools; never invent numbers. Round sensibly.
- When asked "how am I doing", combine get_portfolio with get_recent_trades and transfers."""


def _fmt_usd(v: float | None) -> str:
    return f"${v:,.2f}" if v is not None else "?"


async def _get_portfolio(ctx: ToolContext, args: dict) -> str:
    if not settings().binance_enabled:
        return "Binance is not configured (set BINANCE_API_KEY / BINANCE_API_SECRET)."
    pf = await binance.portfolio()
    lines = [f"Total ≈ {_fmt_usd(pf['total_usdt'])}"]
    for a in pf["assets"][:25]:
        lines.append(f"- {a['asset']}: {a['qty']:g}" + (f" ({_fmt_usd(a['usd'])})" if a["usd"] is not None else ""))
    return "\n".join(lines)


async def _get_recent_trades(ctx: ToolContext, args: dict) -> str:
    if not settings().binance_enabled:
        return "Binance is not configured."
    trades = await binance.recent_trades(limit_per_symbol=int(args.get("limit", 10)))
    if not trades:
        return f"No recent trades found for configured symbols ({', '.join(settings().symbols)})."
    lines = []
    for t in trades[:30]:
        when = dt.datetime.fromtimestamp(t["time"] / 1000, dt.UTC).strftime("%Y-%m-%d %H:%M")
        lines.append(f"- {when} {t['side'].upper()} {t['qty']:g} {t['symbol']} @ {t['price']:g}"
                     f" (≈{_fmt_usd(t['quote_qty'])})")
    return "\n".join(lines)


async def _get_transfers(ctx: ToolContext, args: dict) -> str:
    if not settings().binance_enabled:
        return "Binance is not configured."
    days = min(int(args.get("days", 30)), 90)
    tr = await binance.transfers(days=days)
    lines = [f"Deposits (last {days}d):"]
    lines += [f"- {d['amount']:g} {d['coin']}" for d in tr["deposits"][:15]] or ["- none"]
    lines.append(f"Withdrawals (last {days}d):")
    lines += [f"- {w['amount']:g} {w['coin']}" for w in tr["withdrawals"][:15]] or ["- none"]
    return "\n".join(lines)


register_agent(AgentDef(
    name="finance",
    prompt=PROMPT,
    tools=[
        Tool(
            name="get_portfolio",
            description="Current Binance balances with USD values and portfolio total.",
            input_schema={"type": "object", "properties": {}},
            handler=_get_portfolio,
        ),
        Tool(
            name="get_recent_trades",
            description="Recent spot trades for the user's configured symbols.",
            input_schema={"type": "object", "properties": {
                "limit": {"type": "integer", "description": "max trades per symbol (default 10)"}}},
            handler=_get_recent_trades,
        ),
        Tool(
            name="get_transfers",
            description="Deposit and withdrawal history for the last N days (max 90).",
            input_schema={"type": "object", "properties": {
                "days": {"type": "integer", "description": "lookback window, default 30"}}},
            handler=_get_transfers,
        ),
    ],
))


async def build_daily_summary() -> str | None:
    """Composed by the scheduler each morning; None when Binance is off or empty."""
    if not settings().binance_enabled:
        return None
    pf = await binance.portfolio()
    now_total, old_total = await binance.total_change_24h()
    delta = ""
    if now_total is not None and old_total not in (None, 0):
        pct = (now_total - old_total) / old_total * 100
        arrow = "📈" if pct >= 0 else "📉"
        delta = f" {arrow} {pct:+.2f}% vs yesterday"
    top = ", ".join(f"{a['asset']} {_fmt_usd(a['usd'])}" for a in pf["assets"][:5] if a["usd"])
    return (f"☀️ Daily portfolio summary\n"
            f"Total ≈ {_fmt_usd(pf['total_usdt'])}{delta}\n"
            f"Top holdings: {top or '—'}")
