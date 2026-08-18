"""Calendar tools for the planner agent (only registered when CALENDAR_ENABLED)."""

from __future__ import annotations

import logging

from inais.integrations import calendar, gmail
from inais.orchestrator.registry import Tool, ToolContext
from inais.timeutil import parse_when

log = logging.getLogger(__name__)


async def primary_token() -> str | None:
    """Calendar reads/writes go to the first authorized account's primary calendar."""
    accounts = await gmail.list_accounts()
    return accounts[0]["refresh_token"] if accounts else None


async def _get_agenda(ctx: ToolContext, args: dict) -> str:
    token = await primary_token()
    if token is None:
        return "No Google account authorized yet (run scripts/authorize_gmail.py)."
    try:
        day_offset = int(args.get("day_offset", 0))
        days = int(args.get("days", 1))
    except (TypeError, ValueError):
        day_offset, days = 0, 1
    try:
        events = await calendar.agenda(token, day_offset, days)
    except gmail.GmailAuthError:
        return ("Calendar access needs re-authorization — the calendar scope may be missing. "
                "Re-run scripts/authorize_gmail.py for this account.")
    return calendar.render_agenda(events)


async def _create_event(ctx: ToolContext, args: dict) -> str:
    token = await primary_token()
    if token is None:
        return "No Google account authorized yet."
    title = str(args.get("title", "")).strip()
    start = parse_when(args.get("start_iso"))
    end = parse_when(args.get("end_iso"))
    if not title or start is None:
        return "An event needs a title and a start time."
    if end is None or end <= start:
        from datetime import timedelta

        end = start + timedelta(hours=1)

    # read before writing so the model can warn about clashes instead of double-booking
    try:
        existing = await calendar.agenda(token, 0, 14)
    except gmail.GmailAuthError:
        return "Calendar access needs re-authorization — re-run scripts/authorize_gmail.py."
    clashes = [e["summary"] for e in existing
               if not e["all_day"] and e["start"][:16] == start.astimezone().isoformat()[:16]]

    try:
        link = await calendar.create_event(
            token, title, start.isoformat(), end.isoformat(), str(args.get("description", "")),
        )
    except Exception as e:
        log.exception("calendar create failed")
        return f"Could not create the event: {e}"
    warn = f" (note: clashes with {', '.join(clashes)})" if clashes else ""
    return f"Event created: {title}{warn} — {link}"


TOOLS = [
    Tool(
        name="get_agenda",
        description="Read the user's Google Calendar. day_offset 0 = today, 1 = tomorrow; "
                    "days = how many days to include.",
        input_schema={
            "type": "object",
            "properties": {
                "day_offset": {"type": "integer"},
                "days": {"type": "integer"},
            },
        },
        handler=_get_agenda,
    ),
    Tool(
        name="create_event",
        description="Create a calendar event. Always check get_agenda first if a clash is likely.",
        input_schema={
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "start_iso": {"type": "string", "description": "Local ISO datetime."},
                "end_iso": {"type": "string", "description": "Local ISO datetime (default +1h)."},
                "description": {"type": "string"},
            },
            "required": ["title", "start_iso"],
        },
        handler=_create_event,
    ),
]
