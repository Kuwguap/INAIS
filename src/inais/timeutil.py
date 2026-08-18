"""Timezone-aware time helpers. Everything the user sees is in settings().timezone."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from inais.config import settings

log = logging.getLogger(__name__)


def tz() -> ZoneInfo:
    name = settings().timezone
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        log.warning("unknown TIMEZONE %r — falling back to UTC", name)
        return ZoneInfo("UTC")


def now_local() -> datetime:
    return datetime.now(tz())


def to_local(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(tz())


def fmt(dt: datetime | None, with_date: bool = True) -> str:
    if dt is None:
        return "no date"
    local = to_local(dt)
    return local.strftime("%a %d %b, %H:%M") if with_date else local.strftime("%H:%M")


def parse_when(when_iso: str | None = None, in_minutes: int | None = None) -> datetime | None:
    """Resolve a tool-provided time to an aware UTC datetime.

    `in_minutes` wins when both are given — models are far more reliable at "in 20 minutes"
    than at computing wall-clock arithmetic. Naive ISO strings are read as local time.
    """
    if in_minutes is not None:
        try:
            return datetime.now(UTC) + timedelta(minutes=max(0, int(in_minutes)))
        except (TypeError, ValueError):
            return None
    if not when_iso:
        return None
    raw = str(when_iso).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        try:  # bare date → 09:00 local that day
            dt = datetime.combine(datetime.fromisoformat(raw[:10]).date(),
                                  datetime.min.time().replace(hour=9))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz())
    return dt.astimezone(UTC)


def prompt_now() -> str:
    """The 'current time' line injected into every agent system prompt."""
    local = now_local()
    return f"Current date and time: {local.strftime('%A %d %B %Y, %H:%M')} ({settings().timezone})."
