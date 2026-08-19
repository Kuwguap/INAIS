"""Commitments — things the USER said they'd do.

Captured by the note_commitment tool when the user commits to an action ("I'll email my
advisor tomorrow"), so the assistant can follow through: surfaced in the morning brief and in
proactive check-ins, listed and closed via /commitments. Mirrors the contacts.follow_up_at
pattern — a nullable due date plus a partial index over the open ones.

This is a memory tool, never a send path: the handler only writes to the commitments table
and never touches ctx.bot (keeps it off every send-capable path — a security invariant).
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

from inais import db
from inais.orchestrator.registry import Tool, ToolContext, register_common_tool
from inais.timeutil import now_local

log = logging.getLogger(__name__)

MAX_TEXT = 500


def _due_date(args: dict) -> date | None:
    """Days-offset only — models are reliable at 'in 3 days', unreliable at clock dates."""
    days = args.get("due_in_days")
    if days is None:
        return None
    try:
        return (now_local() + timedelta(days=max(0, int(days)))).date()
    except (TypeError, ValueError):
        return None


async def note_commitment_row(text: str, due_at: date | None,
                              source_msg: str | None = None) -> int | None:
    p = db.pool()
    text = (text or "").strip()
    if p is None or not text:
        return None
    row = await p.fetchrow(
        "insert into commitments (text, due_at, source_msg) values ($1, $2, $3) returning id",
        text[:MAX_TEXT], due_at, source_msg or None)
    return row["id"] if row else None


async def due_commitments(limit: int = 5) -> list[dict]:
    """Open commitments that are due today or overdue — for the brief and proactive check-in."""
    p = db.pool()
    if p is None:
        return []
    rows = await p.fetch(
        "select id, text, due_at from commitments"
        " where not done and due_at is not null and due_at <= current_date"
        " order by due_at limit $1", limit)
    return [dict(r) for r in rows]


async def open_commitments(limit: int = 30) -> list[dict]:
    p = db.pool()
    if p is None:
        return []
    rows = await p.fetch(
        "select id, text, due_at from commitments where not done"
        " order by due_at nulls last, created_at limit $1", limit)
    return [dict(r) for r in rows]


async def mark_done(commitment_id: int) -> str | None:
    p = db.pool()
    if p is None:
        return None
    row = await p.fetchrow(
        "update commitments set done = true where id = $1 and not done returning text",
        commitment_id)
    return row["text"] if row else None


def render(items: list[dict]) -> str:
    if not items:
        return "🤝 No open commitments. When you tell me you'll do something, I'll track it."
    lines = ["🤝 Open commitments", ""]
    for c in items:
        due = f" — due {c['due_at']:%d %b}" if c.get("due_at") else ""
        lines.append(f"#{c['id']} {c['text']}{due}")
    return "\n".join(lines)


async def _note_commitment(ctx: ToolContext, args: dict) -> str:
    text = str(args.get("text", "")).strip()
    if not text:
        return "A commitment needs a description of what they'll do."
    due = _due_date(args)
    cid = await note_commitment_row(text, due, source_msg=None)
    if cid is None:
        return "Couldn't save that commitment (no database?)."
    when = f" (by {due:%d %b})" if due else ""
    return f"Noted — I'll follow up on that{when}."


TOOLS = [
    Tool(
        name="note_commitment",
        description="Record something the USER said they will do — a promise or intention like "
                    "'I'll email my advisor' or 'I'll finish the draft this weekend'. Use it "
                    "when they commit to an action so you can follow up. Do NOT use it for "
                    "things they ask YOU to remind them about (that's set_reminder/add_task).",
        input_schema={
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "What the user committed to do."},
                "due_in_days": {"type": "integer",
                                "description": "Days until it should be done, if they said."},
            },
            "required": ["text"],
        },
        handler=_note_commitment,
    ),
]

for _tool in TOOLS:
    register_common_tool(_tool)
