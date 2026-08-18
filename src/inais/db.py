"""asyncpg pool. statement_cache_size=0 so Supabase's pgbouncer (transaction pooling) works."""

from __future__ import annotations

import logging

import asyncpg

from inais.config import settings

log = logging.getLogger(__name__)

_pool: asyncpg.Pool | None = None


async def init_pool() -> asyncpg.Pool | None:
    global _pool
    cfg = settings()
    if not cfg.db_enabled:
        log.warning("SUPABASE_DB_URL not set — running without persistence (memory/email/finance disabled)")
        return None
    _pool = await asyncpg.create_pool(
        cfg.supabase_db_url,
        min_size=1,
        max_size=5,
        statement_cache_size=0,
        command_timeout=30,
    )
    log.info("Postgres pool ready")
    return _pool


def pool() -> asyncpg.Pool | None:
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
