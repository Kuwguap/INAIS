"""Persisted runtime switches — currently the global pause.

Pause halts every *background* behaviour (Gmail polling, reminders, briefs, snapshots,
reflection, autonomous learning). Conversation keeps working: the point of the switch is
"stop doing things behind my back", not "go mute".

The flag lives in Postgres so a restart or redeploy can't silently un-pause the assistant.
Without a database there is nothing to persist to, so pausing is refused rather than faked.
"""

from __future__ import annotations

import logging

from inais import db

log = logging.getLogger(__name__)

PAUSED = "paused"

# Mirrors the DB so hot paths (every scheduler tick) don't hit Postgres.
_cache: dict[str, bool] = {}


async def load_flags() -> dict[str, bool]:
    """Read all flags into the cache. Called once at startup."""
    p = db.pool()
    if p is None:
        _cache.clear()
        return {}
    try:
        rows = await p.fetch("select key, enabled from runtime_flags")
    except Exception:
        log.exception("could not load runtime flags — assuming defaults")
        return dict(_cache)
    _cache.clear()
    _cache.update({r["key"]: r["enabled"] for r in rows})
    return dict(_cache)


def is_paused() -> bool:
    """Synchronous read of the cached flag — safe to call from any hot path."""
    return _cache.get(PAUSED, False)


async def set_flag(key: str, enabled: bool, note: str = "") -> bool:
    """Persist a flag. Returns False when there is no database to persist to."""
    p = db.pool()
    if p is None:
        return False
    await p.execute(
        "insert into runtime_flags (key, enabled, note, updated_at)"
        " values ($1, $2, $3, now())"
        " on conflict (key) do update set enabled = excluded.enabled,"
        " note = excluded.note, updated_at = now()",
        key, enabled, note or None,
    )
    _cache[key] = enabled
    log.info("runtime flag %s = %s", key, enabled)
    return True


async def set_paused(paused: bool, note: str = "") -> bool:
    return await set_flag(PAUSED, paused, note)


async def paused_since() -> str | None:
    p = db.pool()
    if p is None:
        return None
    row = await p.fetchrow(
        "select updated_at from runtime_flags where key = $1 and enabled", PAUSED)
    if row is None:
        return None
    from inais.timeutil import fmt

    return fmt(row["updated_at"])
