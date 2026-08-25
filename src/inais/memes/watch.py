"""Position watch: dip/stop/target/liquidity alarms + autonomous paper closes.

Alert discipline: every alarm kind LATCHES in the position's alert_state so a 60s tick can
never re-fire it, the dip alarm re-arms only after a real recovery (hysteresis, not
oscillation), and at most MAX_ALARMS_PER_TICK alarms fire per tick (each ping burst is ~1.6s
of awaits — an unbounded loop would wedge the tick).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from inais.config import settings
from inais.integrations import dexscreener
from inais.memes import store

log = logging.getLogger(__name__)

MAX_ALARMS_PER_TICK = 3
LIQ_DROP_FRACTION = 0.5     # liquidity halved since entry = rug in progress


@dataclass
class Alert:
    kind: str            # dip | stop | target | liq_drop
    position_id: int
    text: str


def _pct(value: float | None) -> str:
    return f"{value:+.1f}%" if value is not None else "?"


def pending_alerts(pos: dict, price: float, liquidity: float | None, *,
                   dip_pct: float) -> tuple[list[Alert], dict]:
    """Pure. (alerts to fire now, new alert_state). Latch semantics documented above."""
    state = dict(pos.get("alert_state") or {})
    alerts: list[Alert] = []
    entry = float(pos["entry_price"])
    peak = max(float(pos.get("peak_price") or entry), price)
    sym = pos.get("symbol", "?")
    change_from_entry = (price - entry) / entry * 100 if entry > 0 else None

    size = pos.get("size_usd")
    pnl_usd = ""
    if change_from_entry is not None and size:
        pnl_usd = f", ${change_from_entry / 100 * float(size):+.2f}"
    ctx = (f"now ${price:.10g} · {_pct(change_from_entry)} vs entry"
           f" ${entry:.10g}{pnl_usd}")

    # dip from peak, with hysteresis: re-arms after recovering half the dip threshold
    if peak > 0:
        drawdown = (peak - price) / peak * 100
        if state.get("dip") and drawdown <= dip_pct / 2:
            state["dip"] = False
        if not state.get("dip") and drawdown >= dip_pct:
            state["dip"] = True
            stop_txt = (f"stop sits at ${float(pos['stop_price']):.10g}"
                        if pos.get("stop_price") else "no stop set")
            alerts.append(Alert("dip", pos["id"],
                                f"📉 {sym} down {drawdown:.0f}% from peak ${peak:.10g}\n{ctx}\n"
                                f"Decide: exit via the buttons, or hold — {stop_txt}"))

    stop = pos.get("stop_price")
    if stop and price <= float(stop) and not state.get("stop"):
        state["stop"] = True
        alerts.append(Alert("stop", pos["id"],
                            f"🛑 {sym} broke invalidation ${float(stop):.10g}\n{ctx}\n"
                            f"The thesis is dead by its own rule — exit buttons below."))

    target = pos.get("target_price")
    if target and price >= float(target) and not state.get("target"):
        state["target"] = True
        alerts.append(Alert("target", pos["id"],
                            f"🎯 {sym} hit target ${float(target):.10g}\n{ctx}\n"
                            f"Take it or trail it — your call, buttons below."))

    liq_entry = pos.get("liquidity_at_entry")
    if (liquidity is not None and liq_entry
            and liquidity < float(liq_entry) * LIQ_DROP_FRACTION
            and not state.get("liq_drop")):
        state["liq_drop"] = True
        alerts.append(Alert("liq_drop", pos["id"],
                            f"🚨 {sym} liquidity ${liquidity:,.0f} — HALVED from"
                            f" ${float(liq_entry):,.0f} at entry. Rug pattern.\n{ctx}\n"
                            f"Exits get worse by the minute when this is real."))
    return alerts, state


def paper_close_reason(pos: dict, price: float, liquidity: float | None, *,
                       trail_pct: float) -> str | None:
    """Pure. Why an autonomous paper position closes now, or None to keep holding."""
    liq_entry = pos.get("liquidity_at_entry")
    if liquidity is not None and liq_entry and liquidity < float(liq_entry) * LIQ_DROP_FRACTION:
        return "liq_drop"
    stop = pos.get("stop_price")
    if stop and price <= float(stop):
        return "stop"
    target = pos.get("target_price")
    if target and price >= float(target):
        return "target"
    peak = float(pos.get("peak_price") or pos["entry_price"])
    if peak > 0 and price <= peak * (1 - trail_pct / 100):
        return "trail"
    return None


async def run_watch(bot) -> None:
    """One 60s tick over the open positions — ONE batched price call, then per-position work."""
    from inais.bot import keyboards
    from inais.jobs import reminders

    cfg = settings()
    positions = await store.open_positions()
    if not positions:
        return
    prices = await dexscreener.pairs_for_mints([p["mint"] for p in positions])
    fired = 0
    for pos in positions:
        pair = prices.get(pos["mint"])
        if pair is None or pair.price_usd is None:
            continue  # failed fetch: never act on a missing price
        price, liquidity = pair.price_usd, pair.liquidity_usd
        peak = max(float(pos.get("peak_price") or pos["entry_price"]), price)

        # autonomous paper close beats alerting about it
        if pos["kind"] == "paper" and cfg.meme_paper_enabled:
            reason = paper_close_reason(pos, price, liquidity, trail_pct=cfg.meme_trail_pct)
            if reason:
                row = await store.close_position(pos["id"], exit_price=price, reason=reason)
                if row:
                    await bot.send_message(
                        cfg.owner_telegram_id,
                        f"🧪 Paper close: {row['symbol']} {reason} · "
                        f"PnL {row['pnl_pct']:+.1f}% (${row['pnl_usd']:+.2f})")
                    try:
                        from inais.memes import learning
                        await learning.harvest_position_outcome(row)
                    except Exception:
                        log.exception("paper outcome harvest failed")
                continue

        alerts, new_state = pending_alerts(pos, price, liquidity,
                                           dip_pct=cfg.meme_dip_alert_pct)
        await store.update_position_marks(pos["id"], price, liquidity, peak, new_state)
        for alert in alerts:
            if fired >= MAX_ALARMS_PER_TICK:
                log.warning("alarm cap reached this tick — %s deferred", alert.kind)
                break
            await bot.send_message(
                cfg.owner_telegram_id, alert.text,
                reply_markup=keyboards.meme_position_kb(
                    pos["id"], pos.get("pair_address") or "", pos["mint"]),
                disable_web_page_preview=True)
            await reminders._ping_burst(bot, cfg.owner_telegram_id, alert.text[:60])
            fired += 1
