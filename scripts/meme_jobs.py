"""CLI over the meme_jobs queue — the meme skill suite's database hands (meme-studio et al).

Usage:
  python scripts/meme_jobs.py claim --worker studio-1 [--kinds deep_dive regime scout learn]
  python scripts/meme_jobs.py reclaim
  python scripts/meme_jobs.py progress <job_id> "reading holder data"
  python scripts/meme_jobs.py complete <job_id> --result-file result.json
  python scripts/meme_jobs.py fail <job_id> "error text"
  python scripts/meme_jobs.py queue --kind deep_dive --payload '{"mint": "..."}' [--chat 123]
  python scripts/meme_jobs.py outcomes [--days 14]
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


async def cmd_queue(args) -> None:
    """Queue a follow-up job (e.g. the scout skill chaining deep dives on its finds)."""
    payload = json.loads(args.payload or "{}")
    conn = await _connect()
    try:
        row = await conn.fetchrow(
            "insert into meme_jobs (kind, payload, mint, requested_via, requester_chat_id)"
            " values ($1, $2::jsonb, $3, 'studio', $4) returning id",
            args.kind, json.dumps(payload), payload.get("mint"), args.chat)
        print(json.dumps({"job_id": str(row["id"])}))
    finally:
        await conn.close()


async def cmd_outcomes(args) -> None:
    """Settled signals + closed positions — the learn skill's raw material (read-only)."""
    conn = await _connect()
    try:
        signals = await conn.fetch(
            "select symbol, status, confidence, nn_score, suppressed, thesis,"
            "       entry_price, settle_price, created_at, settled_at"
            " from meme_signals where status <> 'open'"
            "   and settled_at > now() - make_interval(days => $1)"
            " order by settled_at desc limit 100", args.days)
        positions = await conn.fetch(
            "select symbol, kind, close_reason, entry_price, exit_price, pnl_pct, pnl_usd,"
            "       size_usd, opened_at, closed_at"
            " from meme_positions where status = 'closed'"
            "   and closed_at > now() - make_interval(days => $1)"
            " order by closed_at desc limit 100", args.days)
        print(json.dumps({"signals": [dict(r) for r in signals],
                          "positions": [dict(r) for r in positions]}, default=str))
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

    q = sub.add_parser("queue")
    q.add_argument("--kind", required=True,
                   choices=["deep_dive", "regime", "scout", "learn"])
    q.add_argument("--payload", default="{}")
    q.add_argument("--chat", type=int, default=None)
    q.set_defaults(fn=cmd_queue)

    o = sub.add_parser("outcomes")
    o.add_argument("--days", type=int, default=14)
    o.set_defaults(fn=cmd_outcomes)

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
