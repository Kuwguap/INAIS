"""asyncpg pool. statement_cache_size=0 so Supabase's pgbouncer (transaction pooling) works.

A bad connection string must never take the bot down. Before this, pasting the Supabase
*project* URL instead of the Postgres URI crash-looped the whole service: no Telegram, no
logs the user would recognise, just asyncpg's DSN traceback on repeat. The bot now boots
without persistence and says exactly what is wrong, which is both the documented invariant
(missing optional env disables a feature, never the process) and far easier to diagnose.
"""

from __future__ import annotations

import logging

import asyncpg

from inais.config import settings

log = logging.getLogger(__name__)

_pool: asyncpg.Pool | None = None
_last_error: str = ""

VALID_SCHEMES = ("postgresql://", "postgres://")
# Supabase's copy button leaves this in place; pasting it verbatim is a common first mistake.
PASSWORD_PLACEHOLDERS = ("[your-password]", "<password>", "your-password", "[password]")


def dsn_problem(url: str) -> str | None:
    """Why this connection string cannot work, in words worth showing a human.

    Returns None when the DSN looks usable (which is not a promise that it connects).
    """
    url = (url or "").strip()
    if not url:
        return None
    if url.startswith(("http://", "https://")):
        return ("SUPABASE_DB_URL is a project URL, not a database connection string. "
                "In Supabase: Connect → Session pooler → copy the URI starting postgresql://")
    if not url.startswith(VALID_SCHEMES):
        scheme = url.split("://", 1)[0] if "://" in url else url[:12]
        return (f"SUPABASE_DB_URL must start with postgresql:// (got '{scheme}'). "
                "Supabase: Connect → Session pooler.")
    lowered = url.lower()
    if any(p in lowered for p in PASSWORD_PLACEHOLDERS):
        return ("SUPABASE_DB_URL still contains the placeholder password from Supabase's "
                "copy button — replace it with your real database password.")
    if "@" not in url:
        return "SUPABASE_DB_URL has no host — it should look like postgresql://user:pass@host:5432/postgres"
    return None


def last_error() -> str:
    """What went wrong with the database, for /status."""
    return _last_error


async def init_pool() -> asyncpg.Pool | None:
    """Connect if we can. Never raises: the bot must boot even with a broken DSN."""
    global _pool, _last_error
    cfg = settings()
    _last_error = ""

    if not cfg.db_enabled:
        log.warning("SUPABASE_DB_URL not set — running without persistence "
                    "(memory/email/finance/planner disabled)")
        return None

    problem = dsn_problem(cfg.supabase_db_url)
    if problem:
        _last_error = problem
        log.error("DATABASE DISABLED — %s", problem)
        return None

    try:
        _pool = await asyncpg.create_pool(
            cfg.supabase_db_url,
            min_size=1,
            max_size=5,
            statement_cache_size=0,
            command_timeout=30,
        )
    except Exception as e:
        _last_error = f"{type(e).__name__}: {e}"
        log.error("DATABASE DISABLED — could not connect: %s", _last_error)
        log.error("The bot is running without persistence. Fix SUPABASE_DB_URL and redeploy.")
        return None

    log.info("Postgres pool ready")
    return _pool


def pool() -> asyncpg.Pool | None:
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
