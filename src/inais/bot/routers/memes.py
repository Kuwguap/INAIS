"""Meme intelligence — commands + the signal/position buttons. Owner-only via middleware.

The bot never executes a trade here: 'I'm in' and 'Close' only LOG what the owner did in
their own wallet; the venue buttons are URL deep links. Paper positions are simulation.
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from inais import db
from inais.bot import keyboards
from inais.config import settings
from inais.integrations import dexscreener, memejobs
from inais.memes import learning, links, store
from inais.textutil import split_message

log = logging.getLogger(__name__)
router = Router(name="memes")


class MemeStates(StatesGroup):
    waiting_size = State()


async def _guard(message: Message) -> bool:
    cfg = settings()
    if not cfg.meme_enabled:
        await message.answer("Meme intelligence is off — set MEME_ENABLED=true to turn it on.")
        return False
    if db.pool() is None:
        await message.answer("The meme feature needs the database — SUPABASE_DB_URL isn't working.")
        return False
    return True


async def _live_price(mint: str) -> float | None:
    try:
        pair = (await dexscreener.pairs_for_mints([mint])).get(mint)
        return pair.price_usd if pair else None
    except Exception:
        log.exception("live price fetch failed")
        return None


# ---------- commands ----------

@router.message(Command("memes"))
async def cmd_memes(message: Message) -> None:
    if not await _guard(message):
        return
    s = await store.scout_stats()
    recent = await store.recent_signals()
    lines = [
        "🎯 Meme scout",
        "",
        f"Seen: {s.get('seen', 0)} tokens ({s.get('seen_today', 0)} today)"
        f" · rejected {s.get('rejected', 0)} · signaled {s.get('signaled', 0)}",
    ]
    if recent:
        lines.append("\nRecent signals:")
        for r in recent:
            vetoed = " (vetoed)" if r["suppressed"] else ""
            lines.append(f"• {r['symbol']} — {r['status']}, conf {r['confidence']:.0%}{vetoed}")
    else:
        lines.append("\nNo signals yet — the scout reports here when something survives the screen.")
    await message.answer("\n".join(lines))


@router.message(Command("positions"))
async def cmd_positions(message: Message) -> None:
    if not await _guard(message):
        return
    positions = await store.open_positions()
    if not positions:
        await message.answer("📒 No open positions. Log one from a signal card, or let paper "
                             "trading open them automatically.")
        return
    lines = ["📒 Open positions", ""]
    for p in positions:
        kind = "🧪" if p["kind"] == "paper" else "📒"
        entry, last = p["entry_price"], p.get("last_price")
        pnl = f" · {((last - entry) / entry * 100):+.1f}%" if last and entry else ""
        lines.append(f"{kind} {p['symbol']} · in ${entry:.10g} · ${p['size_usd']:.0f}{pnl}")
    await message.answer("\n".join(lines),
                         reply_markup=keyboards.meme_positions_list_kb(positions))


@router.message(Command("paper"))
async def cmd_paper(message: Message) -> None:
    if not await _guard(message):
        return
    r = await store.paper_report()
    closed = r.get("closed", 0) or 0
    hit = f"{(r.get('wins', 0) or 0) / closed:.0%}" if closed else "—"
    await message.answer(
        "🧪 Paper book\n\n"
        f"Bankroll: ${r.get('bankroll', 0):,.2f}"
        f" (realized {float(r.get('realized') or 0):+,.2f})\n"
        f"Closed: {closed} ({hit} winners) · open {r.get('open', 0)}"
        f" (${float(r.get('exposure') or 0):,.0f} exposure)")


@router.message(Command("memestats"))
async def cmd_memestats(message: Message) -> None:
    if not await _guard(message):
        return
    text = learning.render_stats(await store.stats(), await store.paper_report(),
                                 await learning.head_line())
    for chunk in split_message(text):
        await message.answer(chunk)


@router.message(Command("memescan"))
async def cmd_memescan(message: Message) -> None:
    """/memescan <mint> — deep dive · /memescan trends — regime review ·
    /memescan scout — hunt ahead · /memescan learn — trade post-mortem."""
    if not await _guard(message):
        return
    arg = (message.text or "").partition(" ")[2].strip()
    word_kinds = {"trends": "regime", "regime": "regime", "scout": "scout", "learn": "learn"}
    if arg.lower() in word_kinds:
        kind, payload = word_kinds[arg.lower()], {}
        what = {"regime": "a market trends/regime review",
                "scout": "a scout-ahead hunt for fresh candidates",
                "learn": "a post-mortem over your settled trades"}[kind]
    elif links.valid_address(arg):
        kind, payload, what = "deep_dive", {"mint": arg}, "a deep dive"
    else:
        await message.answer("Usage: /memescan <base58 mint>  ·  or one of: trends, scout, learn")
        return
    try:
        await memejobs.queue_job(kind, payload, message.chat.id)
    except memejobs.MemeJobsError as e:
        await message.answer(f"⚠️ {e}")
        return
    jobs = await memejobs.jobs_for_chat(message.chat.id, limit=5)
    await message.answer(
        f"🔬 Queued {what} — the meme-studio skill picks it up next time it runs, and the "
        "report lands here.\n\n" + memejobs.render_jobs(jobs))


# ---------- signal buttons ----------

@router.callback_query(F.data.startswith("mmsk:"))
async def on_skip(cb: CallbackQuery) -> None:
    await cb.answer("Skipped — I'll still track how it plays out.")
    if cb.message:
        try:
            await cb.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass


@router.callback_query(F.data.startswith("mmpa:"))
async def on_paper_entry(cb: CallbackQuery) -> None:
    try:
        signal_id = int((cb.data or "mmpa:0").split(":", 1)[1])
    except ValueError:
        await cb.answer("Malformed.")
        return
    sig = await store.get_signal(signal_id)
    if not sig:
        await cb.answer("Signal's gone.")
        return
    price = await _live_price(sig["mint"])
    if price is None:
        await cb.answer("No live price right now — try again in a minute.", show_alert=True)
        return
    await store.open_position(
        signal_id=signal_id, token_id=sig["token_id"], mint=sig["mint"],
        pair_address=sig["pair_address"], symbol=sig["symbol"], kind="paper",
        entry_price=price, size_usd=settings().meme_paper_size_usd,
        stop=sig["stop_price"], target=sig["target_price"], liquidity=None)
    await cb.answer(f"🧪 Paper position opened at ${price:.10g}")


@router.callback_query(F.data.startswith("mmin:"))
async def on_real_entry(cb: CallbackQuery, state: FSMContext) -> None:
    try:
        signal_id = int((cb.data or "mmin:0").split(":", 1)[1])
    except ValueError:
        await cb.answer("Malformed.")
        return
    await cb.answer()
    await state.set_state(MemeStates.waiting_size)
    await state.update_data(signal_id=signal_id)
    if cb.message:
        await cb.message.answer(
            "📒 Logging your entry (I record it — the trade itself happened in your wallet).\n"
            "How much, in USD? e.g. `75`")


@router.message(MemeStates.waiting_size, F.text & ~F.text.startswith("/"))
async def on_size_input(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    await state.clear()
    raw = (message.text or "").replace("$", "").strip()
    try:
        size = float(raw)
        if not (0 < size < 1_000_000):
            raise ValueError
    except ValueError:
        await message.answer("Couldn't read that as a USD amount — tap the button again.")
        return
    sig = await store.get_signal(int(data.get("signal_id", 0)))
    if not sig:
        await message.answer("That signal is gone.")
        return
    price = await _live_price(sig["mint"])
    if price is None:
        await message.answer("No live price right now — tap the button again in a minute.")
        return
    pos_id = await store.open_position(
        signal_id=sig["id"], token_id=sig["token_id"], mint=sig["mint"],
        pair_address=sig["pair_address"], symbol=sig["symbol"], kind="real",
        entry_price=price, size_usd=size,
        stop=sig["stop_price"], target=sig["target_price"], liquidity=None)
    await message.answer(
        f"📒 Logged: {sig['symbol']} ${size:.0f} @ ${price:.10g}. I'm watching it — "
        f"you'll get loud alerts on dips, stop and target.",
        reply_markup=keyboards.meme_position_kb(pos_id, sig.get("pair_address") or "",
                                                sig["mint"]))


# ---------- position close ----------

@router.callback_query(F.data.startswith("mmcl:"))
async def on_close(cb: CallbackQuery) -> None:
    try:
        position_id = int((cb.data or "mmcl:0").split(":", 1)[1])
    except ValueError:
        await cb.answer("Malformed.")
        return
    positions = {p["id"]: p for p in await store.open_positions()}
    pos = positions.get(position_id)
    if pos is None:
        await cb.answer("Already closed.")
        return
    price = await _live_price(pos["mint"]) or pos.get("last_price")
    if price is None:
        await cb.answer("No price available to close against — try again shortly.",
                        show_alert=True)
        return
    row = await store.close_position(position_id, exit_price=float(price), reason="manual")
    if row is None:
        await cb.answer("Already closed.")
        return
    await cb.answer("Closed ✅")
    try:
        await learning.harvest_position_outcome(row)
    except Exception:
        log.exception("close harvest failed")
    if cb.message:
        await cb.message.answer(
            f"✅ Closed {row['symbol']} ({row['kind']}) @ ${float(price):.10g} · "
            f"PnL {row['pnl_pct']:+.1f}% (${row['pnl_usd']:+.2f})")
