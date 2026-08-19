"""Persistence for episodic (messages), semantic (facts) and procedural (preferences) memory.

Every function is a no-op / empty result when the DB is not configured, so the bot
still runs (statelessly) with only a Telegram token.
"""

from __future__ import annotations

import logging

from inais import db, llm

log = logging.getLogger(__name__)


# ---------- episodic ----------

async def save_message(chat_id: int, role: str, content: str, source: str = "telegram") -> int | None:
    p = db.pool()
    if p is None or not content.strip():
        return None
    row = await p.fetchrow(
        "insert into messages (chat_id, role, content, source) values ($1, $2, $3, $4) returning id",
        chat_id, role, content, source,
    )
    return row["id"]


async def embed_message(message_id: int, content: str,
                        vec: list[float] | None = None) -> None:
    """Attach an embedding after the reply has already gone out (off the hot path).

    `vec` lets the caller reuse an embedding it already computed — the user's message is
    otherwise embedded once for retrieval, once here and once for the interest network.
    """
    p = db.pool()
    if p is None:
        return
    try:
        if vec is None:
            vec = await llm.embed(content)
        await p.execute(
            "update messages set embedding = $1::vector where id = $2",
            llm.vec_literal(vec), message_id,
        )
    except Exception:
        log.exception("embedding message %s failed", message_id)


async def recent_history(chat_id: int, limit: int = 24) -> list[dict]:
    """Last N turns for the conversation window, oldest first, as Anthropic-style messages."""
    p = db.pool()
    if p is None:
        return []
    rows = await p.fetch(
        "select role, content from messages where chat_id = $1 and role in ('user','assistant')"
        " and ts > now() - interval '6 hours'"
        " and id > coalesce((select max(id) from messages where chat_id = $1 and role = 'reset'), 0)"
        " order by id desc limit $2",
        chat_id, limit,
    )
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


async def mark_reset(chat_id: int) -> None:
    """A /reset marker — recent_history only returns messages after the newest marker."""
    p = db.pool()
    if p is None:
        return
    await p.execute(
        "insert into messages (chat_id, role, content, source) values ($1, 'reset', '---', 'telegram')",
        chat_id,
    )


# ---------- semantic ----------

async def add_fact(statement: str, category: str = "general", confidence: float = 0.8,
                   source_message_id: int | None = None) -> int | None:
    p = db.pool()
    if p is None:
        return None
    vec = await llm.embed(statement)
    row = await p.fetchrow(
        "insert into facts (category, statement, embedding, confidence, source_message_id)"
        " values ($1, $2, $3::vector, $4, $5) returning id",
        category, statement, llm.vec_literal(vec), confidence, source_message_id,
    )
    return row["id"]


async def supersede_fact(old_id: int, replacement: str, category: str = "general",
                         confidence: float = 0.8) -> int | None:
    p = db.pool()
    if p is None:
        return None
    new_id = await add_fact(replacement, category, confidence)
    if new_id is not None:
        await p.execute("update facts set superseded_by = $1 where id = $2", new_id, old_id)
    return new_id


async def active_facts(limit: int = 300) -> list[dict]:
    p = db.pool()
    if p is None:
        return []
    rows = await p.fetch(
        "select id, category, statement, confidence from facts"
        " where superseded_by is null and deleted_at is null order by id desc limit $1", limit,
    )
    return [dict(r) for r in rows]


async def count_facts() -> int:
    p = db.pool()
    if p is None:
        return 0
    row = await p.fetchrow(
        "select count(*) as n from facts where superseded_by is null and deleted_at is null")
    return int(row["n"])


async def page_facts(offset: int = 0, limit: int = 5) -> list[dict]:
    """One page of live facts, newest first — what /facts browses."""
    p = db.pool()
    if p is None:
        return []
    rows = await p.fetch(
        "select id, category, statement, confidence, created_at from facts"
        " where superseded_by is null and deleted_at is null"
        " order by id desc offset $1 limit $2", max(0, offset), limit,
    )
    return [dict(r) for r in rows]


async def get_fact(fact_id: int) -> dict | None:
    p = db.pool()
    if p is None:
        return None
    row = await p.fetchrow(
        "select id, category, statement, confidence, deleted_at, superseded_by"
        " from facts where id = $1", fact_id)
    return dict(row) if row else None


async def forget_fact(fact_id: int) -> str | None:
    """Soft-delete: the row stays for audit but leaves retrieval. Returns the statement."""
    p = db.pool()
    if p is None:
        return None
    row = await p.fetchrow(
        "update facts set deleted_at = now() where id = $1 and deleted_at is null"
        " returning statement", fact_id)
    return row["statement"] if row else None


# ---------- procedural ----------

async def add_preference(agent_name: str, rule: str, source: str = "inferred",
                         strength: float = 0.7) -> None:
    p = db.pool()
    if p is None:
        return
    await p.execute(
        "insert into preferences (agent_name, rule, source, strength) values ($1, $2, $3, $4)",
        agent_name, rule, source, strength,
    )


async def preferences_for(agent_name: str) -> list[str]:
    """Rules for this agent plus global ('all') rules — small enough to inject wholesale."""
    p = db.pool()
    if p is None:
        return []
    rows = await p.fetch(
        "select rule from preferences where agent_name in ($1, 'all')"
        " order by strength desc, updated_at desc limit 40", agent_name,
    )
    return [r["rule"] for r in rows]


async def get_profile() -> str:
    p = db.pool()
    if p is None:
        return ""
    row = await p.fetchrow("select doc from user_profile where id = 1")
    return row["doc"] if row else ""


async def set_profile(doc: str) -> None:
    p = db.pool()
    if p is None:
        return
    await p.execute(
        "insert into user_profile (id, doc, rendered_at) values (1, $1, now())"
        " on conflict (id) do update set doc = excluded.doc, rendered_at = now()", doc,
    )
