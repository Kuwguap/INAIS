"""Google Calendar (optional, CALENDAR_ENABLED).

Reuses the Gmail OAuth client and the same per-account refresh tokens, so authorizing an
account with scripts/authorize_gmail.py (which requests the calendar.events scope) covers both.
The google client libraries are sync, so every call is pushed to a worker thread.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from inais.integrations.gmail import GmailAuthError, _client_conf
from inais.timeutil import to_local, tz

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/calendar.events"]


def _service(refresh_token: str):
    client_id, client_secret = _client_conf()
    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=client_id,
        client_secret=client_secret,
        scopes=SCOPES,
    )
    try:
        creds.refresh(Request())
    except RefreshError as e:
        raise GmailAuthError(str(e)) from e
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def _sync_agenda(refresh_token: str, day_offset: int, days: int) -> list[dict]:
    start_local = (datetime.now(tz()) + timedelta(days=day_offset)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    end_local = start_local + timedelta(days=max(1, days))
    svc = _service(refresh_token)
    resp = svc.events().list(
        calendarId="primary",
        timeMin=start_local.astimezone(UTC).isoformat(),
        timeMax=end_local.astimezone(UTC).isoformat(),
        singleEvents=True,
        orderBy="startTime",
        maxResults=25,
    ).execute()
    out = []
    for e in resp.get("items", []):
        start = e.get("start", {})
        out.append({
            "id": e.get("id", ""),
            "summary": e.get("summary", "(no title)"),
            "start": start.get("dateTime") or start.get("date", ""),
            "all_day": "date" in start and "dateTime" not in start,
            "location": e.get("location", ""),
        })
    return out


def _sync_create(refresh_token: str, title: str, start_iso: str, end_iso: str,
                 description: str) -> str:
    svc = _service(refresh_token)
    body = {
        "summary": title,
        "description": description or "",
        "start": {"dateTime": start_iso},
        "end": {"dateTime": end_iso},
    }
    created = svc.events().insert(calendarId="primary", body=body).execute()
    return created.get("htmlLink", created.get("id", "created"))


async def _run(fn, *args):
    try:
        return await asyncio.to_thread(fn, *args)
    except RefreshError as e:
        raise GmailAuthError(str(e)) from e


async def agenda(refresh_token: str, day_offset: int = 0, days: int = 1) -> list[dict]:
    return await _run(_sync_agenda, refresh_token, day_offset, days)


async def create_event(refresh_token: str, title: str, start_iso: str, end_iso: str,
                       description: str = "") -> str:
    return await _run(_sync_create, refresh_token, title, start_iso, end_iso, description)


def render_agenda(events: list[dict]) -> str:
    if not events:
        return "Nothing on the calendar."
    lines = []
    for e in events:
        if e["all_day"]:
            when = "all day"
        else:
            try:
                when = to_local(datetime.fromisoformat(e["start"])).strftime("%a %H:%M")
            except ValueError:
                when = e["start"]
        loc = f" @ {e['location']}" if e["location"] else ""
        lines.append(f"• {when} — {e['summary']}{loc}")
    return "\n".join(lines)
