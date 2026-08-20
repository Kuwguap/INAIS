"""Store admin — run the ogoffcl shop from Telegram. Owner-only (global middleware).

Everything goes through the shared-key `/api/bot` endpoint via integrations/ogoffcl.py.
No money moves from here: there is no refund path (owner's choice); mark-paid only flips a
flag. Store data is rendered as text, never followed as instructions.
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from inais.bot import keyboards
from inais.config import settings
from inais.integrations import ogoffcl
from inais.integrations.ogoffcl import STATUSES
from inais.textutil import split_message

log = logging.getLogger(__name__)
router = Router(name="store")


class StoreStates(StatesGroup):
    waiting_discount = State()


async def _guard(message: Message) -> bool:
    if not settings().ogoffcl_enabled:
        await message.answer("Store isn't configured — set OGOFFCL_BASE_URL and OGOFFCL_API_KEY.")
        return False
    return True


async def _safe_edit(message, text: str, markup=None) -> None:
    try:
        await message.edit_text(text, reply_markup=markup, disable_web_page_preview=True)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise


async def _run(target, coro):
    """Run a store call, funnelling both error kinds to a user-visible message. Returns the
    result, or None on failure (caller should stop)."""
    try:
        return await coro
    except ogoffcl.OgoffclError as e:
        await target.answer(f"⚠️ {e}")
    except Exception:
        log.exception("store call failed")
        await target.answer("Couldn't reach the store — check the logs.")
    return None


# ---------- commands ----------

@router.message(Command("store"))
async def cmd_store(message: Message) -> None:
    if not await _guard(message):
        return
    data = await _run(message, ogoffcl.overview())
    if data is not None:
        await message.answer(ogoffcl.render_overview(data))


@router.message(Command("orders"))
async def cmd_orders(message: Message) -> None:
    if not await _guard(message):
        return
    orders = await _run(message, ogoffcl.list_orders())
    if orders is not None:
        await message.answer(ogoffcl.render_orders(orders),
                             reply_markup=keyboards.orders_list_kb(orders),
                             disable_web_page_preview=True)


@router.message(Command("waitlist"))
async def cmd_waitlist(message: Message) -> None:
    if not await _guard(message):
        return
    data = await _run(message, ogoffcl.waitlist())
    if data is not None:
        for chunk in split_message(ogoffcl.render_waitlist(data)):
            await message.answer(chunk)


@router.message(Command("analytics"))
async def cmd_analytics(message: Message) -> None:
    if not await _guard(message):
        return
    data = await _run(message, ogoffcl.analytics(7))
    if data is not None:
        await message.answer(ogoffcl.render_analytics(data))


@router.message(Command("discounts"))
async def cmd_discounts(message: Message) -> None:
    if not await _guard(message):
        return
    codes = await _run(message, ogoffcl.discounts())
    if codes is not None:
        await message.answer(ogoffcl.render_discounts(codes),
                             reply_markup=keyboards.discounts_kb(codes))


@router.message(Command("lock"))
async def cmd_lock(message: Message) -> None:
    if not await _guard(message):
        return
    data = await _run(message, ogoffcl.site_lock())
    if data is not None:
        await message.answer(ogoffcl.render_site_lock(data),
                             reply_markup=keyboards.site_lock_kb(bool(data.get("locked"))))


@router.message(Command("products"))
async def cmd_products(message: Message) -> None:
    if not await _guard(message):
        return
    products = await _run(message, ogoffcl.product_list())
    if products is not None:
        await message.answer(ogoffcl.render_products(products),
                             reply_markup=keyboards.products_list_kb(products))


# ---------- order callbacks ----------

@router.callback_query(F.data.startswith("ordl:"))
async def on_orders_page(cb: CallbackQuery) -> None:
    await cb.answer()
    if cb.message is None:
        return
    orders = await _run(cb.message, ogoffcl.list_orders())
    if orders is not None:
        await _safe_edit(cb.message, ogoffcl.render_orders(orders),
                         keyboards.orders_list_kb(orders))


async def _render_detail(message, order_id: str) -> None:
    order = await _run(message, ogoffcl.get_order(order_id))
    if not order:
        await message.answer("Order not found.")
        return
    paid = order.get("payment_status") == "paid"
    await _safe_edit(message, ogoffcl.render_order(order),
                     keyboards.order_status_kb(order_id, order.get("status", ""), paid=paid))


@router.callback_query(F.data.startswith("ordst:"))
async def on_order_status(cb: CallbackQuery) -> None:
    try:
        _, order_id, idx = cb.data.split(":", 2)
        status = STATUSES[int(idx)]
    except (ValueError, IndexError):
        await cb.answer("Bad status.")
        return
    await cb.answer(f"→ {ogoffcl.STATUS_LABELS.get(status, status)}")
    if cb.message is None:
        return
    if await _run(cb.message, ogoffcl.set_status(order_id, status)) is not None:
        await _render_detail(cb.message, order_id)


@router.callback_query(F.data.startswith("paid:"))
async def on_mark_paid(cb: CallbackQuery) -> None:
    order_id = (cb.data or "paid:").split(":", 1)[1]
    await cb.answer("Marking paid…")
    if cb.message is None:
        return
    if await _run(cb.message, ogoffcl.mark_paid(order_id)) is not None:
        await _render_detail(cb.message, order_id)


@router.callback_query(F.data.startswith("ord:"))
async def on_order_detail(cb: CallbackQuery) -> None:
    order_id = (cb.data or "ord:").split(":", 1)[1]
    await cb.answer("Loading…")
    if cb.message is not None:
        await _render_detail(cb.message, order_id)


# ---------- discounts ----------

@router.callback_query(F.data.startswith("disc:"))
async def on_discount(cb: CallbackQuery, state: FSMContext) -> None:
    rest = (cb.data or "disc:").split(":", 1)[1]
    if rest == "new":
        await cb.answer()
        await state.set_state(StoreStates.waiting_discount)
        if cb.message:
            await cb.message.answer("Send the new code as: CODE PERCENT  (e.g. DROP20 20)")
        return
    if rest.startswith("tog:"):
        code_id = rest.split(":", 1)[1]
        await cb.answer("Toggled")
        if cb.message is None:
            return
        if await _run(cb.message, ogoffcl.toggle_discount(code_id)) is not None:
            codes = await _run(cb.message, ogoffcl.discounts())
            if codes is not None:
                await _safe_edit(cb.message, ogoffcl.render_discounts(codes),
                                 keyboards.discounts_kb(codes))


@router.message(StoreStates.waiting_discount, F.text & ~F.text.startswith("/"))
async def on_discount_input(message: Message, state: FSMContext) -> None:
    await state.clear()
    parts = (message.text or "").split()
    pct_raw = parts[1].rstrip("%") if len(parts) > 1 else ""
    if len(parts) < 2 or not pct_raw.isdigit():
        await message.answer("Format: CODE PERCENT — e.g. DROP20 20")
        return
    code, pct = parts[0].upper(), int(pct_raw)
    if await _run(message, ogoffcl.create_discount(code, pct)) is not None:
        await message.answer(f"🏷 Created {code} — {pct}% off.")


# ---------- site lock ----------

@router.callback_query(F.data.startswith("lock:"))
async def on_lock(cb: CallbackQuery) -> None:
    locked = (cb.data or "lock:off").split(":", 1)[1] == "on"
    await cb.answer("Store locked 🔒" if locked else "Store unlocked 🔓")
    if cb.message is None:
        return
    if await _run(cb.message, ogoffcl.set_site_lock(locked)) is not None:
        await _safe_edit(cb.message, ogoffcl.render_site_lock({"locked": locked}),
                         keyboards.site_lock_kb(locked))


# ---------- products ----------

async def _render_product(message, product: dict) -> None:
    await _safe_edit(message, ogoffcl.render_product(product), keyboards.product_kb(product))


@router.callback_query(F.data.startswith("prod:"))
async def on_product(cb: CallbackQuery) -> None:
    rest = (cb.data or "prod:").split(":", 1)[1]
    if cb.message is None:
        await cb.answer()
        return
    if rest == "list":
        await cb.answer()
        products = await _run(cb.message, ogoffcl.product_list())
        if products is not None:
            await _safe_edit(cb.message, ogoffcl.render_products(products),
                             keyboards.products_list_kb(products))
        return
    if rest.startswith(("inc:", "dec:", "vis:")):
        verb, pid = rest.split(":", 1)
        await cb.answer("Updating…")
        if verb == "vis":
            product = await _run(cb.message, ogoffcl.product_toggle(pid))
        else:
            product = await _run(cb.message, ogoffcl.product_adjust(pid, 1 if verb == "inc" else -1))
        if product:
            await _render_product(cb.message, product)
        return
    # plain "prod:{id}" — open the product
    await cb.answer("Loading…")
    product = await _run(cb.message, ogoffcl.get_product(rest))
    if product:
        await _render_product(cb.message, product)
