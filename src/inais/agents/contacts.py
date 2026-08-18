"""Contact memory — people, how the user met them, and when to follow up.

Every contact also writes a linked fact, so people surface through ordinary semantic search
("who was the guy from the robotics lab?") and not just exact-name lookup. The fact is the
searchable sentence; the row is the structured state (last contact, follow-up date).

Registered as common tools: knowing who someone is matters just as much when drafting an
email as when planning a week, and prompt caching makes the extra definitions ~free.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from inais import db
from inais.memory import store
from inais.orchestrator.registry import Tool, ToolContext, register_common_tool
from inais.timeutil import now_local, parse_when

log = logging.getLogger(__name__)

MAX_NOTES_CHARS = 4000


def _follow_up_date(args: dict):
    """Accept either a day offset or an ISO date; offsets are what models get right."""
    days = args.get("follow_up_in_days")
    if days is not None:
        try:
            from datetime import timedelta

            return (now_local() + timedelta(days=max(0, int(days)))).date()
        except (TypeError, ValueError):
            return None
    when = parse_when(args.get("follow_up_iso"))
    return when.astimezone().date() if when else None


def append_note(existing: str | None, note: str, when: datetime | None = None) -> str:
    """Newest note last, dated, capped so a long history can't bloat every prompt."""
    stamp = (when or now_local()).strftime("%Y-%m-%d")
    line = f"[{stamp}] {note.strip()}"
    combined = f"{existing.strip()}\n{line}" if existing and existing.strip() else line
    if len(combined) > MAX_NOTES_CHARS:
        combined = combined[-MAX_NOTES_CHARS:]
        combined = combined[combined.find("\n") + 1:]  # never leave a half line at the top
    return combined


async def _add_contact(ctx: ToolContext, args: dict) -> str:
    p = db.pool()
    if p is None:
        return "No database configured — contacts need one."
    name = str(args.get("name", "")).strip()
    if not name:
        return "A contact needs a name."
    org = str(args.get("org", "")).strip() or None
    how_met = str(args.get("how_met", "")).strip() or None
    notes = str(args.get("notes", "")).strip() or None
    follow_up = _follow_up_date(args)

    # the linked fact is what makes contacts searchable in ordinary conversation
    sentence = ", ".join(filter(None, [
        f"{name} is a contact",
        f"works at {org}" if org else "",
        f"met via {how_met}" if how_met else "",
    ]))
    fact_id = None
    try:
        fact_id = await store.add_fact(sentence, category="contacts", confidence=0.9)
    except Exception:
        log.exception("could not create the linked fact for %s", name)

    row = await p.fetchrow(
        "insert into contacts (name, org, how_met, notes, follow_up_at, fact_id)"
        " values ($1, $2, $3, $4, $5, $6)"
        " on conflict (lower(name)) do update set"
        "   org = coalesce(excluded.org, contacts.org),"
        "   how_met = coalesce(excluded.how_met, contacts.how_met),"
        "   follow_up_at = coalesce(excluded.follow_up_at, contacts.follow_up_at)"
        " returning id, (xmax = 0) as inserted",
        name, org, how_met, notes, follow_up, fact_id,
    )
    verb = "Added" if row["inserted"] else "Updated"
    tail = f", follow up {follow_up}" if follow_up else ""
    return f"{verb} contact #{row['id']}: {name}{f' ({org})' if org else ''}{tail}."


async def _find_contact(ctx: ToolContext, args: dict) -> str:
    p = db.pool()
    if p is None:
        return "No database configured."
    query = str(args.get("query", "")).strip()
    if not query:
        return "What name or detail should I look for?"
    rows = await p.fetch(
        "select id, name, org, how_met, last_contact, follow_up_at, notes from contacts"
        " where name ilike $1 or org ilike $1 or how_met ilike $1 or notes ilike $1"
        " order by (name ilike $2) desc, last_contact desc nulls last limit 5",
        f"%{query}%", f"{query}%",
    )
    if not rows:
        return f"No contact matches '{query}'. search_memory may still know them."
    out = []
    for r in rows:
        bits = [f"#{r['id']} {r['name']}"]
        if r["org"]:
            bits.append(f"at {r['org']}")
        if r["how_met"]:
            bits.append(f"met via {r['how_met']}")
        line = " · ".join(bits)
        if r["last_contact"]:
            line += f"\n  last contact: {r['last_contact']:%Y-%m-%d}"
        if r["follow_up_at"]:
            line += f"\n  follow up: {r['follow_up_at']}"
        if r["notes"]:
            line += f"\n  notes: {r['notes'][-500:]}"
        out.append(line)
    return "\n\n".join(out)


async def _log_interaction(ctx: ToolContext, args: dict) -> str:
    p = db.pool()
    if p is None:
        return "No database configured."
    name = str(args.get("name", "")).strip()
    note = str(args.get("note", "")).strip()
    if not name or not note:
        return "I need who you spoke to and a short note about it."
    row = await p.fetchrow(
        "select id, name, notes from contacts where name ilike $1"
        " order by (name ilike $2) desc limit 1", f"%{name}%", f"{name}%")
    if row is None:
        return f"No contact called '{name}' yet — use add_contact first."
    follow_up = _follow_up_date(args)
    await p.execute(
        "update contacts set last_contact = $1, notes = $2,"
        " follow_up_at = coalesce($3, follow_up_at) where id = $4",
        datetime.now(UTC), append_note(row["notes"], note), follow_up, row["id"],
    )
    tail = f" Follow-up set for {follow_up}." if follow_up else ""
    return f"Logged against {row['name']}.{tail}"


TOOLS = [
    Tool(
        name="add_contact",
        description="Remember a person: name, where they work, how the user met them, and "
                    "optionally when to follow up. Use when the user mentions meeting someone.",
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "org": {"type": "string", "description": "Company, lab, course, team."},
                "how_met": {"type": "string", "description": "Where/how they met."},
                "notes": {"type": "string"},
                "follow_up_in_days": {"type": "integer",
                                      "description": "Preferred over follow_up_iso."},
                "follow_up_iso": {"type": "string", "description": "Local ISO date."},
            },
            "required": ["name"],
        },
        handler=_add_contact,
    ),
    Tool(
        name="find_contact",
        description="Look someone up by name, organisation or how they were met. Use before "
                    "drafting a message to a person, or when the user asks who someone is.",
        input_schema={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
        handler=_find_contact,
    ),
    Tool(
        name="log_interaction",
        description="Record that the user spoke to a contact (call, coffee, email) and "
                    "optionally schedule the next follow-up.",
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "note": {"type": "string", "description": "One line: what was discussed."},
                "follow_up_in_days": {"type": "integer"},
                "follow_up_iso": {"type": "string"},
            },
            "required": ["name", "note"],
        },
        handler=_log_interaction,
    ),
]

for _tool in TOOLS:
    register_common_tool(_tool)


# ---------- surfaced by the morning brief ----------

async def due_follow_ups(limit: int = 5) -> list[dict]:
    p = db.pool()
    if p is None:
        return []
    rows = await p.fetch(
        "select id, name, org, follow_up_at, last_contact from contacts"
        " where follow_up_at is not null and follow_up_at <= current_date"
        " order by follow_up_at limit $1", limit)
    return [dict(r) for r in rows]
