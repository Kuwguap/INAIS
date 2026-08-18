"""Apply db/migrations/*.sql to SUPABASE_DB_URL, in order, exactly once each.

Usage: python scripts/apply_migrations.py

Only SUPABASE_DB_URL is required — this runs before the bot is configured, and from CI.
"""

from __future__ import annotations

import asyncio
import pathlib
import sys

import asyncpg

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
from inais.config import db_settings  # noqa: E402

MIGRATIONS_DIR = pathlib.Path(__file__).resolve().parents[1] / "db" / "migrations"


async def main() -> None:
    cfg = db_settings()
    if not cfg.supabase_db_url:
        sys.exit("SUPABASE_DB_URL is not set (put it in .env)")
    conn = await asyncpg.connect(cfg.supabase_db_url, statement_cache_size=0)
    try:
        await conn.execute(
            "create table if not exists schema_migrations"
            " (filename text primary key, applied_at timestamptz not null default now())"
        )
        applied = {r["filename"] for r in await conn.fetch("select filename from schema_migrations")}
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if path.name in applied:
                print(f"= {path.name} (already applied)")
                continue
            sql = path.read_text(encoding="utf-8")
            async with conn.transaction():
                await conn.execute(sql)
                await conn.execute(
                    "insert into schema_migrations (filename) values ($1)", path.name
                )
            print(f"+ {path.name} applied")
        print("done.")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
