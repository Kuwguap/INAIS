"""Inline-button handling for applications and expenses.

Both trackers are populated by a model reading someone else's email template, so every
detection is one tap from being corrected or deleted. That matters more than usual here:
a wrong expense silently distorts the month's spending, and a wrong stage makes the
pipeline lie about where the user stands.
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from inais.agents import applications, expenses
from inais.bot import keyboards
from inais.textutil import split_message

log = logging.getLogger(__name__)
router = Router(name="tracking")


async def _safe_edit(message, text: str, markup=None) -> None:
    """Editing to identical content is not an error worth showing the user."""
    try:
        await message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise


# ---------- applications ----------

@router.message(Command("apps"))
async def cmd_apps(message: Message) -> None:
    include_closed = "all" in (message.text or "").lower()
    apps = await applications.pipeline(include_closed)
    text = applications.render_pipeline(apps, include_closed)
    markup = keyboards.apps_list_kb(apps, include_closed) if apps else None
    for chunk in split_message(text)[:-1]:
        await message.answer(chunk)
    await message.answer(split_message(text)[-1], reply_markup=markup)


@router.callback_query(F.data.startswith("appsall:"))
async def on_apps_toggle(cb: CallbackQuery) -> None:
    include_closed = (cb.data or "").endswith(":1")
    apps = await applications.pipeline(include_closed)
    await cb.answer()
    if cb.message:
        await _safe_edit(cb.message, applications.render_pipeline(apps, include_closed),
                         keyboards.apps_list_kb(apps, include_closed) if apps else None)


@router.callback_query(F.data.startswith("appmenu:"))
async def on_app_menu(cb: CallbackQuery) -> None:
    try:
        app_id = int((cb.data or "appmenu:0").split(":", 1)[1])
    except ValueError:
        await cb.answer("Malformed.")
        return
    app = await applications.get(app_id)
    if app is None:
        await cb.answer("That application is gone.")
        return
    await cb.answer()
    if cb.message:
        role = f" — {app['role']}" if app["role"] else ""
        await _safe_edit(
            cb.message,
            f"📋 #{app['id']} {app['org']}{role}\nCurrently: {app['status']}\n\nMove it to:",
            keyboards.application_status_kb(app_id))


@router.callback_query(F.data.startswith("appst:"))
async def on_app_status(cb: CallbackQuery) -> None:
    parts = (cb.data or "").split(":")
    if len(parts) != 3:
        await cb.answer("Malformed.")
        return
    try:
        app_id = int(parts[1])
    except ValueError:
        await cb.answer("Malformed.")
        return
    updated = await applications.set_status(app_id, parts[2])
    if updated is None:
        await cb.answer("Couldn't update that.", show_alert=True)
        return
    await cb.answer(f"→ {updated['status']}")
    if cb.message:
        icon = applications.STATUS_ICONS.get(updated["status"], "📋")
        role = f" — {updated['role']}" if updated["role"] else ""
        await _safe_edit(cb.message,
                         f"{icon} #{updated['id']} {updated['org']}{role}\n"
                         f"Stage: {updated['status']}")


@router.callback_query(F.data == "appsback")
async def on_apps_back(cb: CallbackQuery) -> None:
    apps = await applications.pipeline()
    await cb.answer()
    if cb.message:
        await _safe_edit(cb.message, applications.render_pipeline(apps),
                         keyboards.apps_list_kb(apps) if apps else None)


@router.callback_query(F.data.startswith("appdel:"))
async def on_app_delete(cb: CallbackQuery) -> None:
    try:
        app_id = int((cb.data or "appdel:0").split(":", 1)[1])
    except ValueError:
        await cb.answer("Malformed.")
        return
    org = await applications.delete(app_id)
    await cb.answer("Removed — I won't track that." if org else "Already gone.")
    if cb.message:
        await _safe_edit(cb.message, f"🗑 Not an application: {org or app_id} — removed.")


@router.callback_query(F.data.startswith("apptask:"))
async def on_app_task(cb: CallbackQuery) -> None:
    """Turn an application deadline into a real task in the planner."""
    try:
        app_id = int((cb.data or "apptask:0").split(":", 1)[1])
    except ValueError:
        await cb.answer("Malformed.")
        return
    app = await applications.get(app_id)
    if app is None or app["deadline"] is None:
        await cb.answer("No deadline on that one.")
        return
    if await applications.has_task(app_id):
        await cb.answer("Task already created.")
        return

    from inais import db

    p = db.pool()
    if p is None:
        await cb.answer("No database.", show_alert=True)
        return
    role = f" ({app['role']})" if app["role"] else ""
    row = await p.fetchrow(
        "insert into tasks (context, title, due, priority, notes)"
        " values ('work', $1, $2, 2, $3) returning id",
        f"Deadline: {app['org']}{role}", app["deadline"], f"application #{app_id}")
    await applications.link_task(app_id, row["id"])
    await cb.answer("Added to your tasks ✅")
    if cb.message:
        await cb.message.answer(f"📅 Task #{row['id']} created for {app['org']} — see /tasks.")


# ---------- expenses ----------

@router.message(Command("spend"))
async def cmd_spend(message: Message) -> None:
    text = await expenses.month_summary(0)
    await message.answer(text, reply_markup=keyboards.spend_kb(0))


@router.callback_query(F.data.startswith("spend:"))
async def on_spend_page(cb: CallbackQuery) -> None:
    try:
        offset = max(0, min(int((cb.data or "spend:0").split(":", 1)[1]), 24))
    except ValueError:
        await cb.answer("Malformed.")
        return
    await cb.answer()
    if cb.message:
        await _safe_edit(cb.message, await expenses.month_summary(offset),
                         keyboards.spend_kb(offset))


@router.callback_query(F.data.startswith("expcat:"))
async def on_expense_category_menu(cb: CallbackQuery) -> None:
    try:
        expense_id = int((cb.data or "expcat:0").split(":", 1)[1])
    except ValueError:
        await cb.answer("Malformed.")
        return
    if await expenses.get(expense_id) is None:
        await cb.answer("That expense is gone.")
        return
    await cb.answer()
    if cb.message:
        await cb.message.edit_reply_markup(
            reply_markup=keyboards.expense_category_kb(expense_id))


@router.callback_query(F.data.startswith("expset:"))
async def on_expense_category_set(cb: CallbackQuery) -> None:
    parts = (cb.data or "").split(":")
    if len(parts) != 3:
        await cb.answer("Malformed.")
        return
    try:
        expense_id = int(parts[1])
    except ValueError:
        await cb.answer("Malformed.")
        return
    if not await expenses.set_category(expense_id, parts[2]):
        await cb.answer("Couldn't update that.", show_alert=True)
        return
    await cb.answer(f"→ {parts[2]}")
    row = await expenses.get(expense_id)
    if cb.message and row:
        await _safe_edit(
            cb.message,
            f"💳 {row['merchant']} — {row['currency']} {row['amount']:,.2f}\n"
            f"Category: {row['category']}",
            keyboards.expense_kb(expense_id))


@router.callback_query(F.data.startswith("expdel:"))
async def on_expense_delete(cb: CallbackQuery) -> None:
    try:
        expense_id = int((cb.data or "expdel:0").split(":", 1)[1])
    except ValueError:
        await cb.answer("Malformed.")
        return
    merchant = await expenses.delete(expense_id)
    await cb.answer("Removed from your spending." if merchant else "Already gone.")
    if cb.message:
        await _safe_edit(cb.message, f"🗑 Not an expense: {merchant or expense_id} — removed.")
