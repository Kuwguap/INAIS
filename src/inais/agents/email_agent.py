"""Email agent: Gmail triage pipeline (called by the scheduler) + tools for the brain.

SECURITY INVARIANT: there is no send tool here. create_email_draft creates a Gmail draft
and asks the owner on Telegram; the actual send lives in bot/routers/approvals.py.
"""

from __future__ import annotations

import logging
import re

from inais import db, llm
from inais.config import settings
from inais.integrations import gmail
from inais.orchestrator.registry import AgentDef, Tool, ToolContext, register_agent

log = logging.getLogger(__name__)

PROMPT = """## Your current role: email agent
You manage the user's Gmail accounts: summarize, search, and draft replies.
- You can only CREATE DRAFTS. Every draft goes to the user on Telegram for approval;
  the user (not you) sends it. Never claim an email was sent.
- Match the user's writing style (see standing rules) — concise by default.
- When drafting a reply, use reply_to_event_id so threading headers are correct.
- If several Gmail accounts exist, confirm which one to use unless it's obvious."""

SKIP_LABELS = {"CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL", "SPAM", "TRASH", "DRAFT", "SENT"}

_SECURITY_RE = re.compile(
    r"login|log-in|sign-in|signin|new device|password|withdraw|security|verification|2fa|authoriz",
    re.IGNORECASE,
)


def is_binance_security(meta: dict) -> bool:
    return gmail.sender_domain(meta["from"]).endswith("binance.com") and bool(
        _SECURITY_RE.search(meta["subject"] + " " + meta["snippet"])
    )


def prefilter(meta: dict) -> bool:
    """True = worth classifying. Gmail's own signals drop the obvious noise for free."""
    if SKIP_LABELS & set(meta["labels"]):
        return False
    return "INBOX" in meta["labels"]


async def classify(meta: dict) -> dict:
    """LLM triage on headers + snippet (~250 tokens). Gmail IMPORTANT label short-circuits."""
    if is_binance_security(meta):
        return {"importance": "high", "reason": "Binance security alert", "needs_reply": False}
    if "IMPORTANT" not in meta["labels"]:
        # Gmail's own priority ML says not important — cheap default, no LLM call
        return {"importance": "low", "reason": "not marked important by Gmail", "needs_reply": False}
    data = await llm.openai_json(
        model=settings().triage_model,
        system=(
            "Triage an email for a busy student/developer. Return JSON "
            '{"importance": "high|normal|low", "reason": "short", "needs_reply": true|false}. '
            "high = time-sensitive, personal, academic, financial or security related; "
            "low = newsletters, receipts, automated notifications."
        ),
        user=f"From: {meta['from']}\nSubject: {meta['subject']}\nSnippet: {meta['snippet']}",
        purpose="email-triage",
        max_completion_tokens=120,
    )
    if data.get("importance") not in ("high", "normal", "low"):
        data["importance"] = "normal"
    return data


async def _record_event(account: str, meta: dict, importance: str) -> int | None:
    p = db.pool()
    if p is None:
        return None
    row = await p.fetchrow(
        "insert into email_events (account, gmail_msg_id, thread_id, from_, subject, snippet, importance)"
        " values ($1, $2, $3, $4, $5, $6, $7)"
        " on conflict (account, gmail_msg_id) do nothing returning id",
        account, meta["id"], meta["thread_id"], meta["from"][:400], meta["subject"][:500],
        meta["snippet"][:800], importance,
    )
    return row["id"] if row else None  # None = already seen


async def triage_account(bot, account: dict) -> None:
    from inais.bot import keyboards  # late import (bot package imports agents)

    email_addr, token = account["email"], account["refresh_token"]
    if not account["last_history_id"]:
        # first run: baseline only — don't backfill the whole mailbox
        hid = await gmail.current_history_id(token)
        await gmail.set_history_id(email_addr, hid)
        log.info("gmail %s baselined at history %s", email_addr, hid)
        return

    try:
        msg_ids, new_hid = await gmail.fetch_history(token, account["last_history_id"])
    except gmail.HistoryGoneError:
        hid = await gmail.current_history_id(token)
        await gmail.set_history_id(email_addr, hid)
        log.warning("gmail %s history expired — re-baselined", email_addr)
        return

    for msg_id in msg_ids[:30]:  # sanity cap per poll
        try:
            meta = await gmail.get_meta(token, msg_id)
        except Exception:
            log.exception("get_meta failed for %s/%s", email_addr, msg_id)
            continue
        if not prefilter(meta):
            continue
        verdict = await classify(meta)
        event_id = await _record_event(email_addr, meta, verdict["importance"])
        if event_id is None:
            continue
        if verdict["importance"] == "high":
            icon = "🔐" if is_binance_security(meta) else "📬"
            text = (f"{icon} {email_addr}\n"
                    f"From: {meta['from']}\n"
                    f"Subject: {meta['subject']}\n\n"
                    f"{meta['snippet']}\n\n({verdict.get('reason', '')})")
            await bot.send_message(
                settings().owner_telegram_id, text,
                reply_markup=keyboards.email_notification_kb(event_id),
            )
    await gmail.set_history_id(email_addr, new_hid)


async def poll_all(bot) -> None:
    """Scheduler entrypoint. Auth failures mark the account and DM the owner once."""
    for account in await gmail.list_accounts():
        try:
            await triage_account(bot, account)
        except gmail.GmailAuthError:
            await gmail.mark_needs_reauth(account["email"])
            await bot.send_message(
                settings().owner_telegram_id,
                f"⚠️ Gmail access for {account['email']} died (invalid_grant — did you change "
                f"your Google password?). Re-run locally:\n"
                f"python scripts/authorize_gmail.py {account['email']}",
            )
        except Exception:
            log.exception("gmail poll failed for %s", account["email"])


# ---------- tools ----------

async def _account_row(email_hint: str) -> dict | None:
    accounts = await gmail.list_accounts()
    if not accounts:
        return None
    if email_hint:
        for a in accounts:
            if a["email"].lower() == email_hint.lower():
                return a
    return accounts[0] if len(accounts) == 1 or not email_hint else None


async def _list_gmail_accounts(ctx: ToolContext, args: dict) -> str:
    accounts = await gmail.list_accounts(only_active=False)
    if not accounts:
        return "No Gmail accounts connected. Run: python scripts/authorize_gmail.py you@gmail.com"
    return "\n".join(f"- {a['email']} ({a['status']})" for a in accounts)


async def _list_recent_emails(ctx: ToolContext, args: dict) -> str:
    p = db.pool()
    if p is None:
        return "No database configured."
    importance = args.get("importance", "")
    q = ("select id, account, from_, subject, snippet, importance, ts from email_events "
         + ("where importance = $2 " if importance else "")
         + "order by id desc limit $1")
    rows = await (p.fetch(q, 15, importance) if importance else p.fetch(q, 15))
    if not rows:
        return "No triaged emails yet."
    return "\n\n".join(
        f"[event #{r['id']}] {r['account']} · {r['importance']}\nFrom: {r['from_']}\n"
        f"Subject: {r['subject']}\n{r['snippet'][:200]}"
        for r in rows
    )


async def _read_email(ctx: ToolContext, args: dict) -> str:
    p = db.pool()
    if p is None:
        return "No database configured."
    event = await p.fetchrow("select * from email_events where id = $1", int(args["event_id"]))
    if not event:
        return "No such email event."
    accounts = {a["email"]: a for a in await gmail.list_accounts()}
    account = accounts.get(event["account"])
    if not account:
        return f"Account {event['account']} is not active."
    body = await gmail.get_body(account["refresh_token"], event["gmail_msg_id"])
    return (f"From: {event['from_']}\nSubject: {event['subject']}\n\n{body or event['snippet']}")


async def _create_email_draft(ctx: ToolContext, args: dict) -> str:
    from inais.bot import keyboards  # late import

    p = db.pool()
    if p is None:
        return "No database configured — cannot create drafts."
    account = await _account_row(str(args.get("account", "")))
    if account is None:
        accounts = await gmail.list_accounts()
        return ("Which account? Connected: " + ", ".join(a["email"] for a in accounts)) if accounts \
            else "No Gmail accounts connected."

    to, subject, body = str(args.get("to", "")), str(args.get("subject", "")), str(args.get("body", ""))
    thread_id, in_reply_to = "", ""
    if args.get("reply_to_event_id"):
        event = await p.fetchrow("select * from email_events where id = $1",
                                 int(args["reply_to_event_id"]))
        if event:
            meta = await gmail.get_meta(account["refresh_token"], event["gmail_msg_id"])
            thread_id = meta["thread_id"]
            in_reply_to = meta["message_id_header"]
            to = to or meta["from"]
            if not subject:
                subject = meta["subject"] if meta["subject"].lower().startswith("re:") \
                    else f"Re: {meta['subject']}"
    if not (to and subject and body):
        return "Need at least: to, subject, body (or a reply_to_event_id)."

    raw = gmail.build_mime(account["email"], to, subject, body, in_reply_to)
    gmail_draft_id = await gmail.create_draft(account["refresh_token"], raw, thread_id)
    row = await p.fetchrow(
        "insert into drafts (kind, account, gmail_draft_id, thread_id, to_addr, subject, body)"
        " values ('email', $1, $2, $3, $4, $5, $6) returning id",
        account["email"], gmail_draft_id, thread_id, to, subject, body,
    )
    draft_id = row["id"]
    preview = (f"📝 Draft #{draft_id} from {account['email']}\nTo: {to}\nSubject: {subject}\n"
               f"{'─' * 20}\n{body[:2500]}")
    await ctx.bot.send_message(settings().owner_telegram_id, preview,
                               reply_markup=keyboards.draft_approval_kb(draft_id))
    return (f"Draft #{draft_id} created and sent to the user on Telegram for approval. "
            f"It is NOT sent yet — the user decides.")


register_agent(AgentDef(
    name="email",
    prompt=PROMPT,
    tools=[
        Tool(
            name="list_gmail_accounts",
            description="List the user's connected Gmail accounts and their status.",
            input_schema={"type": "object", "properties": {}},
            handler=_list_gmail_accounts,
        ),
        Tool(
            name="list_recent_emails",
            description="List recently triaged inbox emails (newest first), optionally by importance.",
            input_schema={"type": "object", "properties": {
                "importance": {"type": "string", "enum": ["high", "normal", "low"]}}},
            handler=_list_recent_emails,
        ),
        Tool(
            name="read_email",
            description="Read the full body of a triaged email by its event id.",
            input_schema={"type": "object", "properties": {
                "event_id": {"type": "integer"}}, "required": ["event_id"]},
            handler=_read_email,
        ),
        Tool(
            name="create_email_draft",
            description="Create a Gmail draft and ask the user on Telegram to approve sending it. "
                        "For replies pass reply_to_event_id (threading + recipient handled for you).",
            input_schema={"type": "object", "properties": {
                "account": {"type": "string", "description": "which Gmail address to send from"},
                "to": {"type": "string"},
                "subject": {"type": "string"},
                "body": {"type": "string", "description": "plain-text email body"},
                "reply_to_event_id": {"type": "integer"},
            }, "required": ["body"]},
            handler=_create_email_draft,
        ),
    ],
))
