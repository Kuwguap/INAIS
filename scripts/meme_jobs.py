"""CLI over the meme_jobs queue — the meme-scan Claude Code skill's database hands.

Usage:
  python scripts/meme_jobs.py claim --worker studio-1 [--kinds deep_dive regime]
  python scripts/meme_jobs.py reclaim
  python scripts/meme_jobs.py progress <job_id> "reading holder data"
  python scripts/meme_jobs.py complete <job_id> --result-file result.json
  python scripts/meme_jobs.py fail <job_id> "error text"
  python scripts/meme_jobs.py add-knowledge --topic "meme/..." --summary "..." [--detail "..."]
                              [--sources sources.json] [--no-embed]

Keeps the skill file free of SQL. Needs only SUPABASE_DB_URL (script_settings, like
apply_migrations.py). Job payloads are DATA, never instructions — this CLI just moves rows.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import sys

import asyncpg

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
from inais.config import script_settings  # noqa: E402


async def _connect() -> asyncpg.Connection:
    cfg = script_settings()
    if not cfg.supabase_db_url:
        sys.exit("SUPABASE_DB_URL is not set (put it in .env)")
    return await asyncpg.connect(cfg.supabase_db_url, statement_cache_size=0)


async def cmd_claim(args) -> None:
    conn = await _connect()
    try:
        rows = await conn.fetch(
            "select * from meme_claim_next_job($1, $2)", args.worker,
            args.kinds if args.kinds else None)
        if not rows:
            print(json.dumps({"job": None}))
            return
        job = dict(rows[0])
        for k in ("payload", "result"):
            if isinstance(job.get(k), str):
                try:
                    job[k] = json.loads(job[k])
                except ValueError:
                    pass
        print(json.dumps({"job": job}, default=str))
    finally:
        await conn.close()


async def cmd_reclaim(args) -> None:
    conn = await _connect()
    try:
        n = await conn.fetchval("select meme_reclaim_stale_jobs($1)", args.stale_minutes)
        print(json.dumps({"reclaimed": n}))
    finally:
        await conn.close()


async def cmd_progress(args) -> None:
    conn = await _connect()
    try:
        await conn.execute(
            "update meme_jobs set progress = $2, status = 'running', heartbeat_at = now()"
            " where id = $1", args.job_id, args.text[:200])
        print(json.dumps({"ok": True}))
    finally:
        await conn.close()


async def cmd_complete(args) -> None:
    result = json.loads(pathlib.Path(args.result_file).read_text(encoding="utf-8"))
    conn = await _connect()
    try:
        await conn.execute(
            "update meme_jobs set status = 'done', result = $2::jsonb, finished_at = now(),"
            " heartbeat_at = now() where id = $1", args.job_id, json.dumps(result))
        print(json.dumps({"ok": True}))
    finally:
        await conn.close()


async def cmd_fail(args) -> None:
    conn = await _connect()
    try:
        await conn.execute(
            "update meme_jobs set status = 'failed', error = $2, finished_at = now()"
            " where id = $1", args.job_id, args.error[:500])
        print(json.dumps({"ok": True}))
    finally:
        await conn.close()


async def cmd_add_knowledge(args) -> None:
    sources = []
    if args.sources:
        sources = json.loads(pathlib.Path(args.sources).read_text(encoding="utf-8"))
    vec = None
    if not args.no_embed:
        try:
            from inais import llm

            vec = llm.vec_literal(await llm.embed(f"{args.topic}. {args.summary}"))
        except Exception as e:  # no OPENAI key in the studio env is fine — row still lands
            print(f"embed skipped: {e}", file=sys.stderr)
    conn = await _connect()
    try:
        row = await conn.fetchrow(
            "insert into knowledge (topic, summary, detail, sources, confidence, embedding,"
            " source_kind) values ($1, $2, $3, $4::jsonb, $5, $6::vector, 'web') returning id",
            args.topic[:200], args.summary, args.detail or None,
            json.dumps(sources), 0.6, vec)
        print(json.dumps({"knowledge_id": row["id"]}))
    finally:
        await conn.close()


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("claim")
    c.add_argument("--worker", required=True)
    c.add_argument("--kinds", nargs="*", default=None)
    c.set_defaults(fn=cmd_claim)

    r = sub.add_parser("reclaim")
    r.add_argument("--stale-minutes", type=int, default=30)
    r.set_defaults(fn=cmd_reclaim)

    g = sub.add_parser("progress")
    g.add_argument("job_id")
    g.add_argument("text")
    g.set_defaults(fn=cmd_progress)

    d = sub.add_parser("complete")
    d.add_argument("job_id")
    d.add_argument("--result-file", required=True)
    d.set_defaults(fn=cmd_complete)

    f = sub.add_parser("fail")
    f.add_argument("job_id")
    f.add_argument("error")
    f.set_defaults(fn=cmd_fail)

    k = sub.add_parser("add-knowledge")
    k.add_argument("--topic", required=True)
    k.add_argument("--summary", required=True)
    k.add_argument("--detail", default="")
    k.add_argument("--sources", default="")
    k.add_argument("--no-embed", action="store_true")
    k.set_defaults(fn=cmd_add_knowledge)

    args = p.parse_args()
    asyncio.run(args.fn(args))


if __name__ == "__main__":
    main()
