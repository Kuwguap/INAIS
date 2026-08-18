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
