"""Expenses extracted from receipts and payment mail, plus the /spend view.

Amounts arrive from an LLM reading somebody else's receipt template, so everything is
validated here: a wrong number silently mis-states the user's spending for the whole month.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation

from inais import db
from inais.config import settings
from inais.orchestrator.registry import Tool, ToolContext, register_common_tool
from inais.timeutil import now_local

log = logging.getLogger(__name__)

EXPENSE_CATEGORIES = ("food", "transport", "subscription", "shopping",
                      "bills", "health", "education", "other")
MAX_AMOUNT = Decimal("1000000")   # beyond this it is a parse error, not a purchase

CURRENCY_SYMBOLS = {"$": "USD", "£": "GBP", "€": "EUR", "₵": "GHS", "₦": "NGN",
                    "¥": "JPY", "₹": "INR", "R": "ZAR"}


def parse_amount(raw) -> Decimal | None:
    """Coerce whatever the model returned into a positive money value.

    Handles 12.34, "12.34", "$1,234.56" and "1 234,56" (European grouping). Returns None
    for anything that is not a sane amount — the caller then records nothing.
    """
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, int | float | Decimal):
        try:
            value = Decimal(str(raw))
        except InvalidOperation:
            return None
    else:
        text = re.sub(r"[^\d.,-]", "", str(raw)).strip()
        if not text:
            return None
        if "," in text and "." in text:
            # whichever separator comes last is the decimal point
            text = (text.replace(",", "") if text.rfind(".") > text.rfind(",")
                    else text.replace(".", "").replace(",", "."))
        elif text.count(",") == 1 and len(text.split(",")[-1]) in (1, 2):
            text = text.replace(",", ".")     # 1234,56
        else:
            text = text.replace(",", "")      # 1,234
        try:
            value = Decimal(text)
        except InvalidOperation:
            return None
    if value <= 0 or value > MAX_AMOUNT:
        return None
    return value.quantize(Decimal("0.01"))


def parse_currency(raw, fallback: str = "") -> str:
    """ISO code from a code or a symbol; falls back to the configured default."""
    fallback = fallback or settings().default_currency
    if not raw:
        return fallback
    text = str(raw).strip()
    if text.upper() in {"USD", "EUR", "GBP", "GHS", "NGN", "KES", "ZAR", "INR", "JPY", "CAD",
                        "AUD", "CHF", "CNY"}:
        return text.upper()
    for symbol, code in CURRENCY_SYMBOLS.items():
        if symbol in text:
            return code
    letters = re.sub(r"[^A-Za-z]", "", text).upper()
    return letters[:3] if len(letters) >= 3 else fallback


async def record(merchant: str, amount: Decimal, currency: str, category: str,
                 occurred_at: datetime | None = None, source_email_id: int | None = None,
                 note: str | None = None) -> int | None:
    """Insert one expense. Returns None when it is a duplicate of the same source email."""
    p = db.pool()
    if p is None:
        return None
    row = await p.fetchrow(
        "insert into expenses (merchant, amount, currency, category, occurred_at,"
        " source_email_id, note) values ($1, $2, $3, $4, $5, $6, $7)"
        " on conflict (source_email_id) where source_email_id is not null do nothing"
        " returning id",
        merchant[:200], amount, currency[:3], category,
        occurred_at or datetime.now(UTC), source_email_id, note,
    )
    return row["id"] if row else None


async def get(expense_id: int) -> dict | None:
    p = db.pool()
    if p is None:
        return None
    row = await p.fetchrow("select * from expenses where id = $1", expense_id)
    return dict(row) if row else None


async def set_category(expense_id: int, category: str) -> bool:
    p = db.pool()
    if p is None or category not in EXPENSE_CATEGORIES:
        return False
    row = await p.fetchrow(
        "update expenses set category = $1 where id = $2 returning id", category, expense_id)
    return row is not None


async def delete(expense_id: int) -> str | None:
    p = db.pool()
    if p is None:
        return None
    row = await p.fetchrow("delete from expenses where id = $1 returning merchant", expense_id)
    return row["merchant"] if row else None


def month_bounds(offset: int = 0, today: date | None = None) -> tuple[date, date, str]:
    """(start, end_exclusive, label) for the month `offset` months back."""
    today = today or now_local().date()
    year, month = today.year, today.month - offset
    while month <= 0:
        month += 12
        year -= 1
    while month > 12:
        month -= 12
        year += 1
    start = date(year, month, 1)
    end = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return start, end, start.strftime("%B %Y")


async def month_summary(offset: int = 0) -> str:
    p = db.pool()
    if p is None:
        return "No database configured — expenses need one."
    start, end, label = month_bounds(offset)
    rows = await p.fetch(
        "select category, currency, sum(amount) as total, count(*) as n from expenses"
        " where occurred_at >= $1 and occurred_at < $2"
        " group by category, currency order by total desc", start, end)
    if not rows:
        return f"💳 {label}\nNothing recorded yet."

    totals: dict[str, Decimal] = {}
    lines = [f"💳 {label}"]
    for r in rows:
        lines.append(f"• {r['category']}: {r['currency']} {r['total']:,.2f} ({r['n']})")
        totals[r["currency"]] = totals.get(r["currency"], Decimal(0)) + r["total"]
    lines.append("")
    lines += [f"Total: {cur} {amt:,.2f}" for cur, amt in totals.items()]

    top = await p.fetch(
        "select merchant, sum(amount) as total, currency from expenses"
        " where occurred_at >= $1 and occurred_at < $2"
        " group by merchant, currency order by total desc limit 3", start, end)
    if top:
        lines.append("\nBiggest: " + ", ".join(
            f"{t['merchant']} ({t['currency']} {t['total']:,.2f})" for t in top))
    return "\n".join(lines)


async def month_total_line() -> str:
    """One line for the daily finance summary, so spending sits next to the portfolio."""
    p = db.pool()
    if p is None:
        return ""
    start, end, _ = month_bounds(0)
    rows = await p.fetch(
        "select currency, sum(amount) as total from expenses"
        " where occurred_at >= $1 and occurred_at < $2 group by currency", start, end)
    if not rows:
        return ""
    spent = ", ".join(f"{r['currency']} {r['total']:,.2f}" for r in rows)
    return f"💳 Spent this month: {spent} (/spend)"


# ---------- tools ----------

async def _log_expense(ctx: ToolContext, args: dict) -> str:
    amount = parse_amount(args.get("amount"))
    merchant = str(args.get("merchant", "")).strip()
    if not merchant or amount is None:
        return "I need a merchant and a positive amount."
    category = str(args.get("category", "other")).lower()
    expense_id = await record(
        merchant=merchant,
        amount=amount,
        currency=parse_currency(args.get("currency")),
        category=category if category in EXPENSE_CATEGORIES else "other",
        note=str(args.get("note", "")).strip() or None,
    )
    if expense_id is None:
        return "Could not save that expense (no database)."
    return f"Logged expense #{expense_id}: {merchant} {amount}."


async def _spend_summary(ctx: ToolContext, args: dict) -> str:
    try:
        offset = max(0, int(args.get("months_ago", 0)))
    except (TypeError, ValueError):
        offset = 0
    return await month_summary(offset)


TOOLS = [
    Tool(
        name="log_expense",
        description="Record something the user spent money on (they mention a purchase, or "
                    "you read it from a receipt). Amounts only — never guess.",
        input_schema={
            "type": "object",
            "properties": {
                "merchant": {"type": "string"},
                "amount": {"type": "number"},
                "currency": {"type": "string", "description": "ISO code, e.g. USD, GHS."},
                "category": {"type": "string", "enum": list(EXPENSE_CATEGORIES)},
                "note": {"type": "string"},
            },
            "required": ["merchant", "amount"],
        },
        handler=_log_expense,
    ),
    Tool(
        name="spend_summary",
        description="Spending for a month, broken down by category. months_ago 0 = this month.",
        input_schema={
            "type": "object",
            "properties": {"months_ago": {"type": "integer"}},
        },
        handler=_spend_summary,
    ),
]

for _tool in TOOLS:
    register_common_tool(_tool)
