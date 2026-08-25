"""Inline keyboards. callback_data is capped at 64 bytes — always short prefix + DB id."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def draft_approval_kb(draft_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Approve & send", callback_data=f"apr:{draft_id}"),
        InlineKeyboardButton(text="✏️ Edit", callback_data=f"edt:{draft_id}"),
        InlineKeyboardButton(text="❌ Reject", callback_data=f"rej:{draft_id}"),
    ]])


def email_notification_kb(event_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✍️ Draft reply", callback_data=f"dra:{event_id}"),
        InlineKeyboardButton(text="🔕 Ignore", callback_data=f"ign:{event_id}"),
    ]])


def quiz_kb(item_id: int, choices: list[str]) -> InlineKeyboardMarkup:
    """One row per choice. callback_data = qz:<item_id>:<choice_index> (well under 64 bytes)."""
    rows = [
        [InlineKeyboardButton(
            text=f"{chr(65 + i)}. {choice[:55]}", callback_data=f"qz:{item_id}:{i}")]
        for i, choice in enumerate(choices[:6])
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def study_plan_kb(plan_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Done", callback_data=f"spd:{plan_id}"),
    ]])


def knowledge_kb(knowledge_id: int) -> InlineKeyboardMarkup:
    """Feedback on something the assistant taught itself — trains the interest network."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="👍 More like this", callback_data=f"kup:{knowledge_id}"),
        InlineKeyboardButton(text="👎 Not useful", callback_data=f"kdn:{knowledge_id}"),
    ]])


def facts_kb(facts: list[dict], offset: int, total: int, page_size: int) -> InlineKeyboardMarkup:
    """Per-fact delete/fix buttons plus pagination. callback_data stays well under 64 bytes."""
    rows = [
        [
            InlineKeyboardButton(text=f"🗑 #{f['id']}", callback_data=f"fdel:{f['id']}:{offset}"),
            InlineKeyboardButton(text=f"✏️ #{f['id']}", callback_data=f"fsup:{f['id']}"),
        ]
        for f in facts
    ]
    nav: list[InlineKeyboardButton] = []
    if offset > 0:
        nav.append(InlineKeyboardButton(
            text="◀ Prev", callback_data=f"fpg:{max(0, offset - page_size)}"))
    page = offset // page_size + 1
    pages = max(1, (total + page_size - 1) // page_size)
    nav.append(InlineKeyboardButton(text=f"{page}/{pages}", callback_data="fnop"))
    if offset + page_size < total:
        nav.append(InlineKeyboardButton(text="Next ▶", callback_data=f"fpg:{offset + page_size}"))
    if nav:
        rows.append(nav)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def application_kb(app_id: int, deadline: bool = False) -> InlineKeyboardMarkup:
    """Shown when triage detects an application. Correcting a false positive must be one tap."""
    rows = [[
        InlineKeyboardButton(text="✏️ Change stage", callback_data=f"appmenu:{app_id}"),
        InlineKeyboardButton(text="🗑 Not an application", callback_data=f"appdel:{app_id}"),
    ]]
    if deadline:
        rows.insert(0, [InlineKeyboardButton(
            text="📅 Add deadline task", callback_data=f"apptask:{app_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def application_status_kb(app_id: int) -> InlineKeyboardMarkup:
    from inais.agents.applications import APPLICATION_STATUSES, STATUS_ICONS

    buttons = [
        InlineKeyboardButton(text=f"{STATUS_ICONS[s]} {s.title()}",
                             callback_data=f"appst:{app_id}:{s}")
        for s in APPLICATION_STATUSES
    ]
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    rows.append([InlineKeyboardButton(text="◀ Back", callback_data="appsback")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def apps_list_kb(apps: list[dict], include_closed: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"✏️ #{a['id']} {a['org'][:24]}",
                              callback_data=f"appmenu:{a['id']}")]
        for a in apps[:8]
    ]
    rows.append([InlineKeyboardButton(
        text="📂 Show active only" if include_closed else "📁 Include closed",
        callback_data=f"appsall:{0 if include_closed else 1}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def expense_kb(expense_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🏷 Category", callback_data=f"expcat:{expense_id}"),
        InlineKeyboardButton(text="🗑 Not an expense", callback_data=f"expdel:{expense_id}"),
    ]])


def expense_category_kb(expense_id: int) -> InlineKeyboardMarkup:
    from inais.agents.expenses import EXPENSE_CATEGORIES

    buttons = [
        InlineKeyboardButton(text=c.title(), callback_data=f"expset:{expense_id}:{c}")
        for c in EXPENSE_CATEGORIES
    ]
    rows = [buttons[i:i + 3] for i in range(0, len(buttons), 3)]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def spend_kb(offset: int) -> InlineKeyboardMarkup:
    row = [InlineKeyboardButton(text="◀ Earlier", callback_data=f"spend:{offset + 1}")]
    if offset > 0:
        row.append(InlineKeyboardButton(text="Later ▶", callback_data=f"spend:{offset - 1}"))
    return InlineKeyboardMarkup(inline_keyboard=[row])


def syllabus_kb(document_id: int, items: list[dict]) -> InlineKeyboardMarkup:
    """Approval gate for extracted syllabus dates — nothing enters the planner untapped."""
    rows = [[
        InlineKeyboardButton(text=f"✅ Add all ({len(items)})",
                             callback_data=f"sylall:{document_id}"),
        InlineKeyboardButton(text="❌ Skip", callback_data=f"syldis:{document_id}"),
    ]]
    rows += [
        [InlineKeyboardButton(text=f"➕ {i['title'][:40]}", callback_data=f"syladd:{i['id']}")]
        for i in items[:8]
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def review_card_kb(card_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="👁 Show answer", callback_data=f"cardshow:{card_id}"),
    ]])


def review_grade_kb(card_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Got it", callback_data=f"cardok:{card_id}"),
        InlineKeyboardButton(text="❌ Missed", callback_data=f"cardno:{card_id}"),
    ]])


def drill_kb(question_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="⏭ Another question", callback_data="drillnext"),
        InlineKeyboardButton(text="🛑 Stop", callback_data="drillstop"),
    ]])


def reminder_stop_kb(reminder_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛑 Stop", callback_data=f"rstop:{reminder_id}")],
        [InlineKeyboardButton(text="💤 10 min", callback_data=f"rsnz:{reminder_id}:10"),
         InlineKeyboardButton(text="💤 1 hr", callback_data=f"rsnz:{reminder_id}:60")],
    ])


def commitments_kb(items: list[dict]) -> InlineKeyboardMarkup | None:
    """A '✅ Done' button per open commitment. callback_data = cmtdone:{id} (well under 64B)."""
    if not items:
        return None
    rows = [
        [InlineKeyboardButton(text=f"✅ {c['text'][:45]}", callback_data=f"cmtdone:{c['id']}")]
        for c in items[:10]
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def persona_kb(current: dict) -> InlineKeyboardMarkup:
    """Tune tone / brevity / humour. callback_data = psona:<field>:<value> (well under 64 B).
    The active value in each row is marked with a ✅."""
    from inais.persona import BREVITY_VALUES, HUMOUR_VALUES, TONE_PRESETS

    def row(field: str, values) -> list[InlineKeyboardButton]:
        return [
            InlineKeyboardButton(
                text=("✅ " if str(current.get(field, "")).lower() == v else "") + v,
                callback_data=f"psona:{field}:{v}")
            for v in values
        ]

    return InlineKeyboardMarkup(inline_keyboard=[
        row("tone", TONE_PRESETS),
        row("brevity", BREVITY_VALUES),
        row("humour", HUMOUR_VALUES),
    ])


# ---------- Store (ogoffcl) — prefixes: ord: ordl: ordst: paid: disc: lock: prod: ----------

def store_order_kb(order_id: str) -> InlineKeyboardMarkup:
    """Attached to a paid-order push notification — jump straight to the order."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🧾 View order", callback_data=f"ord:{order_id}"),
    ]])


def orders_list_kb(orders: list[dict], offset: int = 0, page: int = 8) -> InlineKeyboardMarkup:
    from inais.integrations.ogoffcl import STATUS_LABELS

    rows = []
    for o in orders[:page]:
        paid = "💰" if o.get("payment_status") == "paid" else "🕓"
        label = f"{paid} {str(o.get('order_number', o.get('id')))[:18]} · {STATUS_LABELS.get(o.get('status', ''), '')}"
        rows.append([InlineKeyboardButton(text=label[:60], callback_data=f"ord:{o['id']}")])
    if len(orders) > page:
        rows.append([InlineKeyboardButton(text="More ▸", callback_data=f"ordl:{offset + page}")])
    return InlineKeyboardMarkup(inline_keyboard=rows or [[
        InlineKeyboardButton(text="↻ Refresh", callback_data="ordl:0")]])


def order_status_kb(order_id: str, current: str = "", paid: bool = True) -> InlineKeyboardMarkup:
    from inais.integrations.ogoffcl import STATUS_ICONS, STATUS_LABELS, STATUSES

    btns = [
        InlineKeyboardButton(
            text=("• " if s == current else "") + f"{STATUS_ICONS[s]} {STATUS_LABELS[s]}",
            callback_data=f"ordst:{order_id}:{i}")
        for i, s in enumerate(STATUSES)
    ]
    rows = [btns[i:i + 2] for i in range(0, len(btns), 2)]
    if not paid:
        rows.append([InlineKeyboardButton(text="💰 Mark paid", callback_data=f"paid:{order_id}")])
    rows.append([InlineKeyboardButton(text="◀ Orders", callback_data="ordl:0")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def discounts_kb(codes: list[dict]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(
            text=("⏸ " if c.get("is_active") else "✅ ") + str(c.get("code", ""))[:20],
            callback_data=f"disc:tog:{c['id']}")]
        for c in codes[:8]
    ]
    rows.append([InlineKeyboardButton(text="➕ New code", callback_data="disc:new")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def site_lock_kb(locked: bool) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="🔓 Unlock store" if locked else "🔒 Lock store (show waitlist)",
            callback_data="lock:off" if locked else "lock:on"),
    ]])


def products_list_kb(products: list[dict]) -> InlineKeyboardMarkup:
    rows = []
    for p in products[:8]:
        stock = "∞" if p.get("stock") is None else p.get("stock")
        eye = "👁" if p.get("is_active", True) else "🙈"
        rows.append([InlineKeyboardButton(
            text=f"{eye} {str(p.get('name', '?'))[:22]} · {stock}",
            callback_data=f"prod:{p['id']}")])
    return InlineKeyboardMarkup(inline_keyboard=rows or [[
        InlineKeyboardButton(text="↻ Refresh", callback_data="prod:list")]])


def product_kb(product: dict) -> InlineKeyboardMarkup:
    pid = product["id"]
    active = product.get("is_active", True)
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➖ Stock", callback_data=f"prod:dec:{pid}"),
         InlineKeyboardButton(text="➕ Stock", callback_data=f"prod:inc:{pid}")],
        [InlineKeyboardButton(text="🙈 Hide" if active else "👁 Show",
                              callback_data=f"prod:vis:{pid}")],
        [InlineKeyboardButton(text="◀ Products", callback_data="prod:list")],
    ])


# ---------- Guap Books — prefixes: gbk: (book), gbdl: (download), gblib (library) ----------

def books_library_kb(books: list[dict]) -> InlineKeyboardMarkup:
    rows = []
    for b in books[:10]:
        pages = f" · {b['pages']}p" if b.get("pages") else ""
        rows.append([InlineKeyboardButton(
            text=f"📗 {str(b.get('title', '?'))[:40]}{pages}"[:60],
            callback_data=f"gbk:{b['id']}")])
    return InlineKeyboardMarkup(inline_keyboard=rows or [[
        InlineKeyboardButton(text="↻ Refresh", callback_data="gblib")]])


def book_actions_kb(book: dict) -> InlineKeyboardMarkup:
    bid = book["id"]
    rows = []
    top = []
    if book.get("pdf_path"):
        top.append(InlineKeyboardButton(text="📕 Download PDF", callback_data=f"gbdl:pdf:{bid}"))
    if book.get("cover_path"):
        top.append(InlineKeyboardButton(text="🖼 Cover", callback_data=f"gbdl:cover:{bid}"))
    if top:
        rows.append(top)
    if book.get("flyer_path"):
        rows.append([InlineKeyboardButton(text="🚀 Promo flyer", callback_data=f"gbdl:flyer:{bid}")])
    rows.append([InlineKeyboardButton(text="◀ Library", callback_data="gblib")])
    return InlineKeyboardMarkup(inline_keyboard=rows)
