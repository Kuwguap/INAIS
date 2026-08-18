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
