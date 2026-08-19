"""Application pipeline: what the user applied to, and where each one now stands.

Status comes from the inbox — a confirmation, an interview invite, an assessment link or a
rejection each move the same row forward. Emails arrive out of order and Gmail can redeliver,
so `advance` refuses to walk a pipeline backwards: a stray "thanks for applying" footer in a
later mail must not undo a recorded interview.
"""

from __future__ import annotations

import logging
from datetime import datetime

from inais import db
from inais.orchestrator.registry import Tool, ToolContext, register_common_tool
from inais.timeutil import fmt, parse_when

log = logging.getLogger(__name__)

APPLICATION_KINDS = ("job", "internship", "scholarship", "grant", "program", "other")
APPLICATION_STATUSES = ("applied", "assessment", "interview", "offer", "rejected", "withdrawn")

# How far along the pipeline each status sits. Terminal states are handled separately.
STATUS_RANK = {"applied": 0, "assessment": 1, "interview": 2, "offer": 3}
TERMINAL = {"rejected", "withdrawn"}

STATUS_ICONS = {"applied": "📮", "assessment": "📝", "interview": "🎤",
                "offer": "🎉", "rejected": "❌", "withdrawn": "🚪"}


def should_advance(current: str, incoming: str) -> bool:
    """Would `incoming` move this application forward (or to a terminal state)?

    Rejections and withdrawals always win — they are the end of the story. Otherwise only
    forward moves count, so out-of-order or repeated mail cannot regress the pipeline.
    """
    if incoming not in APPLICATION_STATUSES or incoming == current:
        return False
    if incoming in TERMINAL:
        return True
    if current in TERMINAL:
        return False   # a rejected application does not go back to "interview"
    return STATUS_RANK.get(incoming, -1) > STATUS_RANK.get(current, -1)


async def upsert(org: str, role: str | None, kind: str, status: str,
                 deadline: datetime | None = None, source_email_id: int | None = None,
                 notes: str | None = None) -> tuple[int, str] | None:
    """Create or move an application. Returns (id, action) where action is new|advanced|noop."""
    p = db.pool()
    if p is None or not org.strip():
        return None
    existing = await p.fetchrow(
        "select id, status from applications"
        " where lower(org) = lower($1) and lower(coalesce(role, '')) = lower($2)",
        org, role or "")

    if existing is None:
        row = await p.fetchrow(
            "insert into applications (org, role, kind, status, deadline, source_email_id, notes)"
            " values ($1, $2, $3, $4, $5, $6, $7) returning id",
            org[:200], role, kind, status, deadline, source_email_id, notes)
        return row["id"], "new"

    if not should_advance(existing["status"], status):
        if deadline is not None:   # still worth capturing a newly stated deadline
            await p.execute(
                "update applications set deadline = coalesce(deadline, $1), updated_at = now()"
                " where id = $2", deadline, existing["id"])
        return existing["id"], "noop"

    await p.execute(
        "update applications set status = $1, deadline = coalesce($2, deadline),"
        " updated_at = now() where id = $3", status, deadline, existing["id"])
    return existing["id"], "advanced"


async def set_status(app_id: int, status: str) -> dict | None:
    """Manual override from an inline button — the user is always allowed to correct us."""
    p = db.pool()
    if p is None or status not in APPLICATION_STATUSES:
        return None
    row = await p.fetchrow(
        "update applications set status = $1, updated_at = now() where id = $2"
        " returning id, org, role, status", status, app_id)
    return dict(row) if row else None


async def get(app_id: int) -> dict | None:
    p = db.pool()
    if p is None:
        return None
    row = await p.fetchrow("select * from applications where id = $1", app_id)
    return dict(row) if row else None


async def delete(app_id: int) -> str | None:
    p = db.pool()
    if p is None:
        return None
    row = await p.fetchrow("delete from applications where id = $1 returning org", app_id)
    return row["org"] if row else None


async def pipeline(include_closed: bool = False) -> list[dict]:
    p = db.pool()
    if p is None:
        return []
    where = "" if include_closed else " where status not in ('rejected', 'withdrawn')"
    rows = await p.fetch(
        f"select id, org, role, kind, status, deadline, applied_at from applications{where}"
        " order by case status when 'offer' then 0 when 'interview' then 1"
        "   when 'assessment' then 2 when 'applied' then 3 else 4 end,"
        " deadline nulls last, updated_at desc limit 40")
    return [dict(r) for r in rows]


def render_pipeline(apps: list[dict], include_closed: bool = False) -> str:
    if not apps:
        return ("📋 No applications tracked yet.\n"
                "They appear automatically when confirmations or interview invites arrive.")
    by_status: dict[str, list[dict]] = {}
    for a in apps:
        by_status.setdefault(a["status"], []).append(a)
    order = [s for s in ("offer", "interview", "assessment", "applied", "rejected", "withdrawn")
             if s in by_status]
    lines = [f"📋 Applications ({len(apps)})"]
    for status in order:
        lines.append(f"\n{STATUS_ICONS[status]} {status.title()}")
        for a in by_status[status]:
            role = f" — {a['role']}" if a["role"] else ""
            due = f" · deadline {fmt(a['deadline'])}" if a.get("deadline") else ""
            lines.append(f"#{a['id']} {a['org']}{role}{due}")
    if not include_closed:
        lines.append("\n(/apps all to include rejected)")
    return "\n".join(lines)


async def due_deadlines(limit: int = 5) -> list[dict]:
    p = db.pool()
    if p is None:
        return []
    rows = await p.fetch(
        "select id, org, role, deadline from applications"
        " where deadline is not null and deadline >= now()"
        "   and deadline < now() + interval '7 days'"
        "   and status not in ('rejected', 'withdrawn')"
        " order by deadline limit $1", limit)
    return [dict(r) for r in rows]


STALE_APPLICATION_DAYS = 14


async def stale_applications(days: int = STALE_APPLICATION_DAYS, limit: int = 3) -> list[dict]:
    """Live applications with no update in `days` — a nudge to send a follow-up."""
    p = db.pool()
    if p is None:
        return []
    rows = await p.fetch(
        "select id, org, role, status, updated_at from applications"
        " where status not in ('rejected', 'withdrawn')"
        "   and updated_at < now() - make_interval(days => $1)"
        " order by updated_at limit $2", days, limit)
    return [dict(r) for r in rows]


async def link_task(app_id: int, task_id: int) -> None:
    p = db.pool()
    if p is not None:
        await p.execute("update applications set task_id = $1 where id = $2", task_id, app_id)


async def has_task(app_id: int) -> bool:
    p = db.pool()
    if p is None:
        return False
    row = await p.fetchrow("select task_id from applications where id = $1", app_id)
    return bool(row and row["task_id"])


# ---------- tools ----------

async def _add_application(ctx: ToolContext, args: dict) -> str:
    org = str(args.get("org", "")).strip()
    if not org:
        return "Which organisation is the application to?"
    kind = str(args.get("kind", "job")).lower()
    status = str(args.get("status", "applied")).lower()
    result = await upsert(
        org=org,
        role=str(args.get("role", "")).strip() or None,
        kind=kind if kind in APPLICATION_KINDS else "other",
        status=status if status in APPLICATION_STATUSES else "applied",
        deadline=parse_when(args.get("deadline_iso")),
        notes=str(args.get("notes", "")).strip() or None,
    )
    if result is None:
        return "Could not save that (no database)."
    app_id, action = result
    verb = {"new": "Tracking", "advanced": "Updated", "noop": "Already tracking"}[action]
    return f"{verb} application #{app_id}: {org} ({status})."


async def _list_applications(ctx: ToolContext, args: dict) -> str:
    include_closed = bool(args.get("include_closed"))
    return render_pipeline(await pipeline(include_closed), include_closed)


async def _update_application(ctx: ToolContext, args: dict) -> str:
    status = str(args.get("status", "")).lower()
    org = str(args.get("org", "")).strip()
    if status not in APPLICATION_STATUSES:
        return f"Status must be one of: {', '.join(APPLICATION_STATUSES)}."
    p = db.pool()
    if p is None:
        return "No database configured."
    row = await p.fetchrow(
        "select id from applications where org ilike $1 order by updated_at desc limit 1",
        f"%{org}%")
    if row is None:
        return f"No application tracked for '{org}'."
    updated = await set_status(row["id"], status)
    return f"#{updated['id']} {updated['org']} → {status}." if updated else "Could not update."


TOOLS = [
    Tool(
        name="add_application",
        description="Track an application the user made (job, internship, scholarship, grant). "
                    "Use when they mention applying somewhere.",
        input_schema={
            "type": "object",
            "properties": {
                "org": {"type": "string"},
                "role": {"type": "string"},
                "kind": {"type": "string", "enum": list(APPLICATION_KINDS)},
                "status": {"type": "string", "enum": list(APPLICATION_STATUSES)},
                "deadline_iso": {"type": "string", "description": "Local ISO date."},
                "notes": {"type": "string"},
            },
            "required": ["org"],
        },
        handler=_add_application,
    ),
    Tool(
        name="list_applications",
        description="Show the application pipeline grouped by stage.",
        input_schema={
            "type": "object",
            "properties": {"include_closed": {"type": "boolean"}},
        },
        handler=_list_applications,
    ),
    Tool(
        name="update_application",
        description="Move an application to a new stage when the user tells you what happened.",
        input_schema={
            "type": "object",
            "properties": {
                "org": {"type": "string"},
                "status": {"type": "string", "enum": list(APPLICATION_STATUSES)},
            },
            "required": ["org", "status"],
        },
        handler=_update_application,
    ),
]

for _tool in TOOLS:
    register_common_tool(_tool)
