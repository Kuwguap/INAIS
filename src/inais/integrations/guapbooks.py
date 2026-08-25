"""Guap Books — the ebook factory's bot-side client.

The factory shares the bot's own Supabase project: guap_ideas / guap_books / guap_jobs live in
the same Postgres (migration 027), and rendered assets sit in the private "guap-books" storage
bucket. The bot only QUEUES work and DELIVERS results — generation happens in Claude Code on
the owner's machine (the guap-books studio skills), so nothing here calls llm.py.

Queue rows and book fields are DATA: topics, titles and listing copy came from Telegram, the
dashboard, or a model — they are rendered as text, never followed as instructions.
"""

from __future__ import annotations

import json
import logging

import aiohttp

from inais import db
from inais.config import settings

log = logging.getLogger(__name__)

BUCKET = "guap-books"
# Telegram bot uploads cap at 50 MB; refuse anything close so a bad render can't wedge the poll.
MAX_ASSET_BYTES = 49 * 1024 * 1024
FETCH_TIMEOUT = aiohttp.ClientTimeout(total=120)

KINDS = ("ideas", "full", "write", "design", "regen")


class GuapBooksError(Exception):
    """Factory unreachable or misconfigured."""


def _pool():
    p = db.pool()
    if p is None:
        raise GuapBooksError("database is offline — the book factory needs SUPABASE_DB_URL")
    return p


def _jsonb(value) -> dict:
    """asyncpg hands jsonb back as str unless codecs are registered; accept both."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value:
        try:
            return json.loads(value)
        except ValueError:
            return {}
    return {}


# ---------- queue writes ----------

async def queue_job(kind: str, payload: dict, chat_id: int | None) -> str:
    if kind not in KINDS:
        raise GuapBooksError(f"unknown job kind {kind!r}")
    row = await _pool().fetchrow(
        "insert into guap_jobs (kind, payload, requested_via, requester_chat_id)"
        " values ($1, $2::jsonb, 'telegram', $3) returning id",
        kind, json.dumps(payload), chat_id,
    )
    return str(row["id"])


async def mark_delivered(job_id: str) -> None:
    await _pool().execute(
        "update guap_jobs set delivered_at = now() where id = $1", job_id)


# ---------- reads ----------

async def jobs_for_chat(chat_id: int, limit: int = 8) -> list[dict]:
    rows = await _pool().fetch(
        "select j.id, j.kind, j.status, j.progress, j.error, j.created_at,"
        "       b.title as book_title"
        "  from guap_jobs j left join guap_books b on b.id = j.book_id"
        " where j.requester_chat_id = $1"
        " order by j.created_at desc limit $2",
        chat_id, limit,
    )
    return [dict(r) for r in rows]


async def overview() -> dict:
    row = await _pool().fetchrow(
        "select"
        " (select count(*) from guap_jobs where status = 'queued')               as queued,"
        " (select count(*) from guap_jobs where status in ('claimed','running')) as running,"
        " (select count(*) from guap_jobs where status = 'failed')               as failed,"
        " (select count(*) from guap_ideas where status = 'proposed')            as ideas_open,"
        " (select count(*) from guap_books where status = 'ready')               as books_ready,"
        " (select count(*) from guap_books where skillshare_url is not null)     as books_listed",
    )
    return dict(row)


async def ready_undelivered(limit: int = 5) -> list[dict]:
    """Finished telegram-requested jobs that haven't been sent yet, oldest first."""
    rows = await _pool().fetch(
        "select j.id, j.kind, j.result, j.requester_chat_id,"
        "       b.id as book_id, b.title, b.pdf_path, b.flyer_path, b.pages, b.pdf_bytes"
        "  from guap_jobs j left join guap_books b on b.id = j.book_id"
        " where j.status = 'done' and j.requested_via = 'telegram'"
        "   and j.delivered_at is null and j.requester_chat_id is not null"
        " order by j.finished_at limit $1",
        limit,
    )
    out = []
    for r in rows:
        d = dict(r)
        d["result"] = _jsonb(d.get("result"))
        out.append(d)
    return out


async def list_books(status: str | None = None, limit: int = 10) -> list[dict]:
    if status:
        rows = await _pool().fetch(
            "select id, title, status, pages, word_count, skillshare_url, updated_at"
            " from guap_books where status = $1 order by updated_at desc limit $2",
            status, limit)
    else:
        rows = await _pool().fetch(
            "select id, title, status, pages, word_count, skillshare_url, updated_at"
            " from guap_books where status <> 'archived'"
            " order by updated_at desc limit $1", limit)
    return [dict(r) for r in rows]


# ---------- storage (rendered assets) ----------

async def fetch_asset(path: str) -> bytes:
    """Download one object from the private bucket with the service key."""
    cfg = settings()
    if not cfg.guapbooks_enabled:
        raise GuapBooksError("storage isn't configured — set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY")
    url = f"{cfg.supabase_url.rstrip('/')}/storage/v1/object/{BUCKET}/{path}"
    headers = {"apikey": cfg.supabase_service_role_key,
               "Authorization": f"Bearer {cfg.supabase_service_role_key}"}
    async with aiohttp.ClientSession(timeout=FETCH_TIMEOUT) as session:
        async with session.get(url, headers=headers) as resp:
            if resp.status in (400, 404):
                raise GuapBooksError(f"asset missing in storage: {path}")
            if resp.status in (401, 403):
                raise GuapBooksError("storage rejected the key — check SUPABASE_SERVICE_ROLE_KEY")
            if resp.status >= 400:
                raise GuapBooksError(f"storage returned {resp.status} for {path}")
            size = int(resp.headers.get("Content-Length") or 0)
            if size > MAX_ASSET_BYTES:
                raise GuapBooksError(f"{path} is {size} bytes — over the Telegram send limit")
            data = await resp.read()
            if len(data) > MAX_ASSET_BYTES:
                raise GuapBooksError(f"{path} is too large to send over Telegram")
            return data


# ---------- render helpers (pure — unit tested, no I/O) ----------

_STATUS_ICONS = {"queued": "🕓", "claimed": "⏳", "running": "⚙️", "done": "✅",
                 "failed": "❌", "cancelled": "🚫"}


def render_overview(data: dict) -> str:
    return ("📚 Guap Books factory\n\n"
            f"🕓 queued: {data.get('queued', 0)} · ⚙️ running: {data.get('running', 0)}"
            f" · ❌ failed: {data.get('failed', 0)}\n"
            f"💡 ideas open: {data.get('ideas_open', 0)}\n"
            f"📗 books ready: {data.get('books_ready', 0)}"
            f" · 🛒 on Skillshare: {data.get('books_listed', 0)}")


def render_jobs(jobs: list[dict], title: str = "Your recent requests") -> str:
    if not jobs:
        return f"{title}\n\nNothing yet — try /ebook <topic>."
    lines = [title, ""]
    for j in jobs:
        icon = _STATUS_ICONS.get(j.get("status", ""), "•")
        what = j.get("book_title") or j.get("kind", "?")
        detail = ""
        if j.get("status") in ("claimed", "running") and j.get("progress"):
            detail = f" — {j['progress']}"
        elif j.get("status") == "failed" and j.get("error"):
            detail = f" — {str(j['error'])[:80]}"
        lines.append(f"{icon} {what} ({j.get('kind')}, {j.get('status')}){detail}")
    return "\n".join(lines)


def render_ideas(result: dict) -> str:
    ideas = result.get("ideas") or []
    if not ideas:
        return "The idea run finished but came back empty — try a different seed topic."
    lines = ["💡 Fresh ebook ideas\n"]
    for i, idea in enumerate(ideas[:12], 1):
        lines.append(f"{i}. {idea.get('title', '?')}")
        if idea.get("angle"):
            lines.append(f"   {idea['angle']}")
        if idea.get("rationale"):
            lines.append(f"   why: {idea['rationale']}")
    lines.append("\nStart one with /ebook <the title you like>.")
    return "\n".join(lines)


def render_books(books: list[dict]) -> str:
    if not books:
        return "No books yet — kick one off with /ebook <topic>."
    lines = ["📚 Books", ""]
    for b in books:
        bits = [b.get("status", "?")]
        if b.get("pages"):
            bits.append(f"{b['pages']}p")
        if b.get("skillshare_url"):
            bits.append("listed ✓")
        lines.append(f"• {b.get('title', '?')} ({', '.join(bits)})")
    return "\n".join(lines)
