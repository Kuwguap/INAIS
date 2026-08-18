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
