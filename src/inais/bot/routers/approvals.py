"""Human approval flow — the ONLY place in the codebase that sends email.

Buttons: apr/edt/rej on draft messages; dra/ign on important-email notifications.
Idempotency: the send is guarded by an atomic status transition in SQL, so a
double-tap or redelivered callback can never send twice.
"""

from __future__ import annotations

import asyncio
import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from inais import db
from inais.bot import keyboards
from inais.brain import signals
from inais.bot.routers.chat import typing_indicator
from inais.integrations import gmail
from inais.orchestrator import loop
from inais.textutil import split_message

log = logging.getLogger(__name__)
router = Router(name="approvals")


class EditDraft(StatesGroup):
    waiting_body = State()


async def _draft(draft_id: int):
    p = db.pool()
    if p is None:
        return None
    return await p.fetchrow("select * from drafts where id = $1", draft_id)


async def _account_token(email: str) -> str | None:
    accounts = {a["email"]: a for a in await gmail.list_accounts()}
    row = accounts.get(email)
    return row["refresh_token"] if row else None


def _cb_id(cb: CallbackQuery) -> int:
    return int((cb.data or "0:0").split(":", 1)[1])


# ---------- draft approval ----------

@router.callback_query(F.data.startswith("apr:"))
async def on_approve(cb: CallbackQuery) -> None:
    draft_id = _cb_id(cb)
    p = db.pool()
    if p is None:
        await cb.answer("No database.", show_alert=True)
        return
    # atomic claim — this is the double-send guard
    row = await p.fetchrow(
        "update drafts set status = 'sending' where id = $1 and status in ('pending','edited')"
        " returning *", draft_id,
    )
    if row is None:
        await cb.answer("Already handled.")
        return
    token = await _account_token(row["account"])
    if token is None:
        await p.execute("update drafts set status = 'pending' where id = $1", draft_id)
        await cb.answer(f"Account {row['account']} needs re-auth.", show_alert=True)
        return
    try:
        await gmail.send_draft(token, row["gmail_draft_id"])
    except Exception:
        log.exception("sending draft %s failed", draft_id)
        await p.execute("update drafts set status = 'pending' where id = $1", draft_id)
        await cb.answer("Send failed — try again.", show_alert=True)
        return
    await p.execute("update drafts set status = 'sent', sent_at = now() where id = $1", draft_id)
    await cb.answer("Sent ✅")
    if cb.message:
        await cb.message.edit_text(
            f"✅ Sent — draft #{draft_id} to {row['to_addr']}\nSubject: {row['subject']}",
        )


@router.callback_query(F.data.startswith("rej:"))
async def on_reject(cb: CallbackQuery) -> None:
    draft_id = _cb_id(cb)
    p = db.pool()
    if p is not None:
        await p.execute(
            "update drafts set status = 'rejected' where id = $1 and status in ('pending','edited')",
            draft_id,
        )
    await cb.answer("Rejected")
    if cb.message:
        await cb.message.edit_text(f"❌ Rejected — draft #{draft_id} was not sent.")


@router.callback_query(F.data.startswith("edt:"))
async def on_edit(cb: CallbackQuery, state: FSMContext) -> None:
    draft_id = _cb_id(cb)
    row = await _draft(draft_id)
    if row is None or row["status"] not in ("pending", "edited"):
        await cb.answer("Already handled.")
        return
    await state.set_state(EditDraft.waiting_body)
    await state.update_data(draft_id=draft_id)
    await cb.answer()
    if cb.message:
        await cb.message.answer(
            f"✏️ Send me the corrected body for draft #{draft_id} "
            f"(or /cancel to keep it as is).",
        )


@router.message(EditDraft.waiting_body, F.text == "/cancel")
async def on_edit_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Edit cancelled — the draft is unchanged.")


@router.message(EditDraft.waiting_body, F.text & ~F.text.startswith("/"))
async def on_edit_body(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    await state.clear()
    draft_id = int(data["draft_id"])
    row = await _draft(draft_id)
    if row is None or row["status"] not in ("pending", "edited"):
        await message.answer("That draft was already handled.")
        return
    new_body = message.text or ""
    token = await _account_token(row["account"])
    if token is None:
        await message.answer(f"Account {row['account']} needs re-auth — can't update the draft.")
        return
    raw = gmail.build_mime(row["account"], row["to_addr"], row["subject"], new_body)
    try:
        await gmail.update_draft(token, row["gmail_draft_id"], raw, row["thread_id"] or "")
    except Exception:
        log.exception("updating gmail draft %s failed", draft_id)
        await message.answer("Couldn't update the Gmail draft — try again.")
        return
    p = db.pool()
    # keep the original body; store the user's version for the nightly learning loop
    await p.execute(
        "update drafts set user_edit = $1, status = 'edited' where id = $2", new_body, draft_id,
    )
    await message.answer(
        f"📝 Draft #{draft_id} updated.\nTo: {row['to_addr']}\nSubject: {row['subject']}\n"
        f"{'─' * 20}\n{new_body[:2500]}",
        reply_markup=keyboards.draft_approval_kb(draft_id),
    )


# ---------- important-email notification buttons ----------

@router.callback_query(F.data.startswith("dra:"))
async def on_draft_reply(cb: CallbackQuery) -> None:
    event_id = _cb_id(cb)
    await cb.answer("Drafting…")
    # acting on a mail is the strongest "this mattered" label we get
    asyncio.create_task(signals.record_email_signal_from_event(event_id, important=True))
    if cb.message is None:
        return
    chat_id = cb.message.chat.id
    prompt = (f"Draft a reply to email event #{event_id}. First use read_email to see the full "
              f"message, then create_email_draft with reply_to_event_id={event_id}.")
    async with typing_indicator(cb.bot, chat_id):
        try:
            reply = (await loop.handle_text(cb.bot, chat_id, prompt)).text
        except Exception:
            log.exception("draft-reply flow failed")
            reply = "Couldn't draft the reply — try asking me directly."
    for chunk in split_message(reply):
        await cb.message.answer(chunk)


@router.callback_query(F.data.startswith("ign:"))
async def on_ignore(cb: CallbackQuery) -> None:
    asyncio.create_task(signals.record_email_signal_from_event(_cb_id(cb), important=False))
    await cb.answer("Ignored")
    if cb.message:
        await cb.message.edit_reply_markup(reply_markup=None)
