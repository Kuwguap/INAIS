"""Meme deep-research job queue — the bot's client (guapbooks.py mirror).

The bot only QUEUES research jobs and DELIVERS finished reports; the actual research runs in
Claude Code via the meme-scan skill, which claims jobs through scripts/meme_jobs.py. Payloads
(mint, question, notes) are DATA, never instructions — the skill treats them the same way.
"""

from __future__ import annotations

import json
import logging

from inais import db

log = logging.getLogger(__name__)

KINDS = ("deep_dive", "regime")


class MemeJobsError(Exception):
    pass


def _pool():
    p = db.pool()
    if p is None:
        raise MemeJobsError("database is offline — deep research needs SUPABASE_DB_URL")
    return p


def _jsonb(value) -> dict:
    if isinstance(value, dict):
        return value
    try:
        return json.loads(value) if value else {}
    except (TypeError, ValueError):
        return {}


async def queue_job(kind: str, payload: dict, chat_id: int | None) -> str:
    if kind not in KINDS:
        raise MemeJobsError(f"unknown job kind {kind!r}")
    row = await _pool().fetchrow(
        "insert into meme_jobs (kind, payload, mint, requested_via, requester_chat_id)"
        " values ($1, $2::jsonb, $3, 'telegram', $4) returning id",
        kind, json.dumps(payload), payload.get("mint"), chat_id)
    return str(row["id"])


async def ready_undelivered(limit: int = 5) -> list[dict]:
    rows = await _pool().fetch(
        "select id, kind, mint, result, requester_chat_id from meme_jobs"
        " where status = 'done' and requested_via = 'telegram'"
        "   and delivered_at is null and requester_chat_id is not null"
        " order by finished_at limit $1", limit)
    out = []
    for r in rows:
        d = dict(r)
        d["result"] = _jsonb(d.get("result"))
        out.append(d)
    return out


async def mark_delivered(job_id: str) -> None:
    await _pool().execute(
        "update meme_jobs set delivered_at = now() where id = $1", job_id)


async def jobs_for_chat(chat_id: int, limit: int = 8) -> list[dict]:
    rows = await _pool().fetch(
        "select id, kind, mint, status, progress, error, created_at from meme_jobs"
        " where requester_chat_id = $1 order by created_at desc limit $2",
        chat_id, limit)
    return [dict(r) for r in rows]


# ---------- render helpers (pure) ----------

def render_report(job: dict) -> str:
    result = job.get("result") or {}
    report = str(result.get("report_md", "")).strip()
    if not report:
        return f"🔬 Deep dive on {job.get('mint', '?')} finished, but the report came back empty."
    header = f"🔬 Deep research — {job.get('mint', job.get('kind', '?'))}"
    verdict = str(result.get("verdict", "")).strip()
    tail = f"\n\nVerdict: {verdict}" if verdict else ""
    return f"{header}\n\n{report[:3500]}{tail}"


def render_jobs(jobs: list[dict]) -> str:
    if not jobs:
        return "No research jobs yet — queue one with /memescan <mint>."
    icons = {"queued": "🕓", "claimed": "⏳", "running": "⚙️", "done": "✅",
             "failed": "❌", "cancelled": "🚫"}
    lines = ["🔬 Research jobs", ""]
    for j in jobs:
        detail = ""
        if j.get("status") in ("claimed", "running") and j.get("progress"):
            detail = f" — {j['progress']}"
        elif j.get("status") == "failed" and j.get("error"):
            detail = f" — {str(j['error'])[:60]}"
        lines.append(f"{icons.get(j.get('status', ''), '•')} {j.get('kind')}"
                     f" {str(j.get('mint') or '')[:12]} ({j.get('status')}){detail}")
    return "\n".join(lines)
