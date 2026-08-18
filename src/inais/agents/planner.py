"""Planner agent — tasks, reminders and time management for school + work."""

from __future__ import annotations

import logging

from inais import db
from inais.config import settings
from inais.orchestrator.registry import AgentDef, Tool, ToolContext, register_agent
from inais.timeutil import fmt, parse_when

log = logging.getLogger(__name__)

PROMPT = """## Your current role: planner
You manage the user's time across school, work and personal life.
- When the user mentions something they need to do, add it as a task (pick the right context).
- For "remind me in N minutes/hours" prefer set_reminder with in_minutes — it is exact.
  For a specific clock time use when_iso in the user's local timezone (naive ISO is local).
- Use recurring_cron (5-field cron) only for genuinely repeating reminders.
- When asked to plan a day or week, call list_tasks first, then propose a concrete schedule
  with times — do not invent tasks the user never mentioned.
- Confirm what you created in one short line (title + when), not a wall of text."""

CONTEXTS = ("school", "work", "personal")


# ---------- tasks ----------

async def _add_task(ctx: ToolContext, args: dict) -> str:
    p = db.pool()
    if p is None:
        return "No database configured — tasks cannot be saved."
    title = str(args.get("title", "")).strip()
    if not title:
        return "A task needs a title."
    context = str(args.get("context", "personal")).lower()
    if context not in CONTEXTS:
        context = "personal"
    due = parse_when(args.get("due_iso"), args.get("due_in_minutes"))
    try:
        priority = int(args.get("priority", 3))
    except (TypeError, ValueError):
        priority = 3
    row = await p.fetchrow(
        "insert into tasks (context, title, notes, due, priority)"
        " values ($1, $2, $3, $4, $5) returning id",
        context, title, args.get("notes"), due, priority,
    )
    return f"Task #{row['id']} added ({context}, due {fmt(due)})."


async def _list_tasks(ctx: ToolContext, args: dict) -> str:
    p = db.pool()
    if p is None:
        return "No database configured."
    context = str(args.get("context", "")).lower()
    scope = str(args.get("scope", "open")).lower()
    where = ["status = 'open'"] if scope != "all" else ["true"]
    params: list = []
    if context in CONTEXTS:
        params.append(context)
        where.append(f"context = ${len(params)}")
    if scope == "today":
        where.append("(due::date <= current_date or due is null)")
    elif scope == "overdue":
        where.append("due < now()")
    rows = await p.fetch(
        f"select id, context, title, due, priority, status from tasks"
        f" where {' and '.join(where)} order by due nulls last, priority limit 40",
        *params,
    )
    if not rows:
        return "No matching tasks."
    return "\n".join(
        f"#{r['id']} [{r['context']}] {r['title']} — due {fmt(r['due'])}"
        + ("" if r["status"] == "open" else f" ({r['status']})")
        for r in rows
    )


async def _complete_task(ctx: ToolContext, args: dict) -> str:
    p = db.pool()
    if p is None:
        return "No database configured."
    task_id = args.get("task_id")
    if task_id is not None:
        row = await p.fetchrow(
            "update tasks set status = 'done', completed_at = now()"
            " where id = $1 and status = 'open' returning title",
            int(task_id),
        )
    else:  # match on title when the user names a task instead of numbering it
        row = await p.fetchrow(
            "update tasks set status = 'done', completed_at = now() where id = ("
            "  select id from tasks where status = 'open' and title ilike $1"
            "  order by due nulls last limit 1) returning title",
            f"%{str(args.get('title', '')).strip()}%",
        )
    return f"Marked done: {row['title']}" if row else "No open task matched."


# ---------- reminders ----------

async def _set_reminder(ctx: ToolContext, args: dict) -> str:
    p = db.pool()
    if p is None:
        return "No database configured — reminders need one."
    text = str(args.get("text", "")).strip()
    if not text:
        return "A reminder needs text."
    fire_at = parse_when(args.get("when_iso"), args.get("in_minutes"))
    cron = str(args.get("recurring_cron", "")).strip() or None
    if fire_at is None and cron:
        from inais.jobs.reminders import next_cron_fire  # late import: avoids a cycle

        fire_at = next_cron_fire(cron)
    if fire_at is None:
        return "I need a time — either in_minutes, when_iso, or a valid recurring_cron."
    row = await p.fetchrow(
        "insert into reminders (text, fire_at, recurring_cron) values ($1, $2, $3) returning id",
        text, fire_at, cron,
    )
    extra = f", repeating ({cron})" if cron else ""
    return f"Reminder #{row['id']} set for {fmt(fire_at)}{extra}."


async def _list_reminders(ctx: ToolContext, args: dict) -> str:
    p = db.pool()
    if p is None:
        return "No database configured."
    rows = await p.fetch(
        "select id, text, fire_at, recurring_cron from reminders"
        " where not fired order by fire_at limit 20",
    )
    if not rows:
        return "No pending reminders."
    return "\n".join(
        f"#{r['id']} {fmt(r['fire_at'])} — {r['text']}"
        + (f" (repeats {r['recurring_cron']})" if r["recurring_cron"] else "")
        for r in rows
    )


TOOLS = [
    Tool(
        name="add_task",
        description="Add a task/todo for the user. Use for anything they need to do later.",
        input_schema={
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "context": {"type": "string", "enum": list(CONTEXTS)},
                "notes": {"type": "string"},
                "due_iso": {"type": "string",
                            "description": "Local ISO datetime, e.g. 2026-08-22T17:00. Optional."},
                "due_in_minutes": {"type": "integer",
                                   "description": "Relative alternative to due_iso."},
                "priority": {"type": "integer", "description": "1 highest to 5 lowest (default 3)."},
            },
            "required": ["title"],
        },
        handler=_add_task,
    ),
    Tool(
        name="list_tasks",
        description="List the user's tasks. Call this before planning a day or week.",
        input_schema={
            "type": "object",
            "properties": {
                "context": {"type": "string", "enum": list(CONTEXTS)},
                "scope": {"type": "string", "enum": ["open", "today", "overdue", "all"]},
            },
        },
        handler=_list_tasks,
    ),
    Tool(
        name="complete_task",
        description="Mark a task done, by id or by (partial) title.",
        input_schema={
            "type": "object",
            "properties": {"task_id": {"type": "integer"}, "title": {"type": "string"}},
        },
        handler=_complete_task,
    ),
    Tool(
        name="set_reminder",
        description="Schedule a Telegram ping at a specific time. Prefer in_minutes for "
                    "relative times ('in 20 minutes'); when_iso for clock times.",
        input_schema={
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "in_minutes": {"type": "integer"},
                "when_iso": {"type": "string", "description": "Local ISO datetime."},
                "recurring_cron": {"type": "string",
                                   "description": "5-field cron for repeats, e.g. '0 8 * * 1-5'."},
            },
            "required": ["text"],
        },
        handler=_set_reminder,
    ),
    Tool(
        name="list_reminders",
        description="Show pending reminders.",
        input_schema={"type": "object", "properties": {}},
        handler=_list_reminders,
    ),
]


def _calendar_tools() -> list[Tool]:
    if not settings().calendar_enabled:
        return []
    from inais.agents import calendar_tools

    return calendar_tools.TOOLS


register_agent(AgentDef(name="planner", prompt=PROMPT, tools=[*TOOLS, *_calendar_tools()]))
