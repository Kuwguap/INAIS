"""Client for the ogoffcl store's bot API — the outbound half of the integration.

Every call hits `{OGOFFCL_BASE_URL}/api/bot?action=...` and carries the shared secret in the
`x-bot-secret` header (the same key the store checks and the bot's inbound /store/events route
verifies). Reads are GET; admin actions (status change, mark-paid, discounts, site-lock,
product) are POST. Unlike the read-only GitHub client, this one legitimately writes — but only
what the owner taps: no money ever moves from here (refunds stay on the web admin).

Store responses are DATA: order text, customer names and analytics come from the site and are
only ever rendered as text, never followed as instructions.
"""

from __future__ import annotations

import logging
import re

import aiohttp

from inais.config import settings

log = logging.getLogger(__name__)

TIMEOUT = aiohttp.ClientTimeout(total=20)

# The order lifecycle, in pipeline order then terminal states. Index into this list is what the
# status-change buttons carry (callback_data is 64-byte capped and order ids are UUIDs).
STATUSES = ["pending", "confirmed", "processing", "shipped", "out_for_delivery",
            "delivered", "cancelled", "refunded"]
STATUS_LABELS = {
    "pending": "Pending", "confirmed": "Confirmed", "processing": "Preparing",
    "shipped": "Shipped", "out_for_delivery": "Out for delivery", "delivered": "Delivered",
    "cancelled": "Cancelled", "refunded": "Refunded",
}
STATUS_ICONS = {
    "pending": "🕓", "confirmed": "✅", "processing": "📦", "shipped": "🚚",
    "out_for_delivery": "🛵", "delivered": "🎉", "cancelled": "❌", "refunded": "↩️",
}


class OgoffclError(Exception):
    """Store unreachable, misconfigured, or rejecting the key."""


def _headers() -> dict[str, str]:
    return {"x-bot-secret": settings().ogoffcl_api_key,
            "Content-Type": "application/json",
            "User-Agent": "INAIS-personal-assistant"}


def _url() -> str:
    return f"{settings().ogoffcl_base_url.rstrip('/')}/api/bot"


async def _request(method: str, action: str, params: dict | None = None,
                   body: dict | None = None):
    cfg = settings()
    if not cfg.ogoffcl_enabled:
        raise OgoffclError("Store isn't configured — set OGOFFCL_BASE_URL and OGOFFCL_API_KEY.")
    q = {"action": action, **(params or {})}
    async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
        async with session.request(method, _url(), params=q, json=body,
                                   headers=_headers()) as resp:
            if resp.status in (401, 403):
                raise OgoffclError("the store rejected the key (check OGOFFCL_API_KEY == BOT_SECRET)")
            if resp.status == 404:
                return None
            text = await resp.text()
            if resp.status >= 400:
                raise OgoffclError(f"store returned {resp.status}: {text[:200]}")
            try:
                return await resp.json() if text else None
            except aiohttp.ContentTypeError:
                return text


async def _get(action: str, **params):
    return await _request("GET", action, params={k: v for k, v in params.items() if v is not None})


async def _post(action: str, **body):
    return await _request("POST", action, body=body)


# ---------- reads ----------

async def list_orders(status: str | None = None, limit: int = 15) -> list[dict]:
    data = await _get("orders", status=status, limit=limit)
    return data if isinstance(data, list) else (data or {}).get("orders", []) if data else []


_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                      r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


async def get_order(ref: str) -> dict | None:
    """Look up by id (from a list button) or by order number (from conversation, e.g. OG-1234)."""
    ref = str(ref).strip()
    if not ref:
        return None
    key = "id" if _UUID_RE.match(ref) else "number"
    return await _get("order", **{key: ref})


async def waitlist(source: str | None = None, limit: int = 20) -> dict:
    return await _get("waitlist", source=source, limit=limit) or {}


async def analytics(days: int = 7) -> dict:
    return await _get("analytics", days=days) or {}


async def overview() -> dict:
    return await _get("overview") or {}


async def discounts() -> list[dict]:
    data = await _get("discounts")
    return data if isinstance(data, list) else (data or {}).get("codes", []) if data else []


async def site_lock() -> dict:
    return await _get("site-lock") or {}


async def product_list(query: str | None = None, limit: int = 20) -> list[dict]:
    data = await _get("product", q=query, limit=limit)
    return data if isinstance(data, list) else (data or {}).get("products", []) if data else []


async def get_product(product_id: str) -> dict | None:
    return await _get("product", id=product_id)


# ---------- writes (owner-tapped; no money movement) ----------

async def set_status(order_id: str, status: str, note: str | None = None) -> dict:
    return await _post("order-status", id=order_id, status=status, note=note) or {}


async def mark_paid(order_id: str) -> dict:
    return await _post("mark-paid", id=order_id) or {}


async def toggle_discount(code_id: str) -> dict:
    return await _post("discount-toggle", id=code_id) or {}


async def create_discount(code: str, percentage: int) -> dict:
    return await _post("discount-create", code=code, percentage=percentage) or {}


async def set_site_lock(locked: bool) -> dict:
    return await _post("site-lock", locked=locked) or {}


async def product_adjust(product_id: str, stock_delta: int) -> dict:
    """Nudge a product's stock by ±N server-side (avoids a read-modify-write race). Returns
    the updated product row."""
    return await _post("product", id=product_id, stock_delta=stock_delta) or {}


async def product_toggle(product_id: str) -> dict:
    """Flip a product's visibility server-side. Returns the updated product row."""
    return await _post("product", id=product_id, toggle_active=True) or {}


# ---------- render helpers (pure) ----------

def _cedis(amount) -> str:
    try:
        return f"GH₵{float(amount):,.2f}"
    except (TypeError, ValueError):
        return "GH₵0.00"


def status_line(status: str) -> str:
    return f"{STATUS_ICONS.get(status, '•')} {STATUS_LABELS.get(status, status)}"


def render_orders(orders: list[dict], title: str = "📦 Orders") -> str:
    if not orders:
        return f"{title}\n\nNo orders to show."
    lines = [title, ""]
    for o in orders:
        paid = "💰" if o.get("payment_status") == "paid" else "🕓"
        lines.append(
            f"{paid} {o.get('order_number', o.get('id', '?'))} · {_cedis(o.get('total_amount'))}"
            f" · {status_line(o.get('status', '?'))}\n"
            f"   {o.get('customer_name', '—')}")
    return "\n".join(lines)


def render_order(order: dict) -> str:
    if not order:
        return "Order not found."
    o = order
    items = o.get("items") or o.get("order_items") or []
    lines = [
        f"🧾 {o.get('order_number', o.get('id'))}",
        f"{status_line(o.get('status', '?'))} · "
        f"{'💰 paid' if o.get('payment_status') == 'paid' else '🕓 unpaid'}",
        "",
        f"Customer: {o.get('customer_name', '—')}",
        f"Phone: {o.get('customer_phone', '—')}",
        f"Email: {o.get('customer_email', '—')}",
        f"Ship to: {o.get('shipping_address', '—')}",
        "",
        "Items:",
    ]
    for it in items:
        size = f" ({it['size']})" if it.get("size") else ""
        lines.append(f"  • {it.get('product_name', '?')}{size} ×{it.get('quantity', 1)}"
                     f" — {_cedis(it.get('price') or it.get('unit_price'))}")
    lines.append("")
    lines.append(f"Total: {_cedis(o.get('total_amount'))}")
    if o.get("discount_code"):
        lines.append(f"Discount: {o['discount_code']} (−{_cedis(o.get('discount_amount'))})")
    return "\n".join(lines)


def render_waitlist(data: dict) -> str:
    rows = data.get("rows") or data.get("subscribers") or []
    total = data.get("count", len(rows))
    lines = [f"⏳ Waitlist — {total} total", ""]
    for r in rows[:30]:
        src = f" · {r['source']}" if r.get("source") else ""
        lines.append(f"• {r.get('email', '?')}{src}")
    if not rows:
        lines.append("No signups yet.")
    return "\n".join(lines)


def render_analytics(data: dict) -> str:
    if not data:
        return "No analytics available."
    days = data.get("days", 7)
    lines = [
        f"📈 Traffic — last {days} days", "",
        f"Views: {data.get('views', 0)}",
        f"Visitors: {data.get('visits', 0)}",
        f"Mobile: {data.get('mobile_pct', 0)}%",
    ]
    top = data.get("top_pages") or []
    if top:
        lines.append("\nTop pages:")
        lines += [f"  {p.get('path', '?')} ({p.get('count', 0)})" for p in top[:6]]
    refs = data.get("referrers") or []
    if refs:
        lines.append("\nReferrers:")
        lines += [f"  {r.get('source', '?')} ({r.get('count', 0)})" for r in refs[:6]]
    return "\n".join(lines)


def render_overview(data: dict) -> str:
    if not data:
        return "No store data available."
    return (
        "🛒 Store overview\n\n"
        f"Products: {data.get('products', 0)}\n"
        f"Orders: {data.get('orders', 0)} ({data.get('unpaid', 0)} unpaid)\n"
        f"Paid revenue: {_cedis(data.get('revenue'))}")


# ---------- inbound push (site → bot) ----------

def event_key(payload: dict) -> str:
    """Stable identity for a store push, for dedupe."""
    return f"{payload.get('event', 'event')}:{payload.get('id', payload.get('email', ''))}"


async def claim_event(key: str) -> bool:
    """True if this event is new (and records it), False if already notified. Without a DB it
    returns True — a rare duplicate alert beats a dropped one."""
    from inais import db

    p = db.pool()
    if p is None:
        return True
    row = await p.fetchrow(
        "insert into store_events (event_key, kind) values ($1, $2)"
        " on conflict (event_key) do nothing returning id",
        key[:200], key.split(":", 1)[0][:40])
    return row is not None


def format_event(payload: dict) -> tuple[str, str | None]:
    """A store push → (owner message, order_id). order_id is set only for order.paid so the
    caller can attach View / Change-status buttons."""
    event = str(payload.get("event", ""))
    if event == "waitlist.joined":
        email = payload.get("email", "someone")
        src = payload.get("source")
        src_tail = f" · {src}" if src and src != "waitlist" else ""
        count = payload.get("count")
        total_tail = f" (total: {count})" if count is not None else ""
        return f"🎉 New waitlist signup: {email}{src_tail}{total_tail}", None
    if event == "order.paid":
        num = payload.get("order_number", payload.get("id", "?"))
        name = payload.get("customer_name", "customer")
        n = payload.get("item_count")
        items_tail = f" · {n} item(s)" if n is not None else ""
        return (f"💰 New paid order {num}\n{_cedis(payload.get('total_amount'))}"
                f" · {name}{items_tail}", payload.get("id"))
    return f"🛒 Store event: {event}", None


def render_discounts(codes: list[dict]) -> str:
    if not codes:
        return "🏷 No discount codes yet."
    lines = ["🏷 Discount codes", ""]
    for c in codes:
        state = "✅" if c.get("is_active") else "⏸"
        lines.append(f"{state} {c.get('code', '?')} — {c.get('percentage', 0)}% off")
    return "\n".join(lines)


def render_products(products: list[dict]) -> str:
    if not products:
        return "🧢 No products."
    lines = ["🧢 Products", ""]
    for p in products[:12]:
        stock = "∞" if p.get("stock") is None else p.get("stock")
        eye = "👁" if p.get("is_active", True) else "🙈"
        lines.append(f"{eye} {p.get('name', '?')} — {_cedis(p.get('price'))} · stock {stock}")
    return "\n".join(lines)


def render_product(p: dict) -> str:
    if not p:
        return "Product not found."
    stock = "untracked" if p.get("stock") is None else p.get("stock")
    vis = "visible 👁" if p.get("is_active", True) else "hidden 🙈"
    return f"🧢 {p.get('name', '?')}\n{_cedis(p.get('price'))} · stock: {stock} · {vis}"


def render_site_lock(data: dict) -> str:
    locked = bool(data.get("locked"))
    return ("🔒 Store is LOCKED — every visitor sees the waitlist screen."
            if locked else "🔓 Store is OPEN — shopping normally.")
