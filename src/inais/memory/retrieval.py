"""Hybrid (RRF: full-text + vector) retrieval over facts and past messages."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from inais import db, llm
from inais.memory import store

log = logging.getLogger(__name__)


@dataclass
class MemoryContext:
    profile: str = ""
    preferences: list[str] = field(default_factory=list)
    facts: list[str] = field(default_factory=list)
    episodes: list[str] = field(default_factory=list)

    def render(self) -> str:
        parts: list[str] = []
        if self.profile:
            parts.append(f"## What you know about the user\n{self.profile}")
        if self.preferences:
            parts.append("## Standing rules from the user\n" + "\n".join(f"- {r}" for r in self.preferences))
        if self.facts:
            parts.append("## Relevant remembered facts\n" + "\n".join(f"- {f}" for f in self.facts))
        if self.episodes:
            parts.append("## Possibly relevant past exchanges\n" + "\n".join(f"- {e}" for e in self.episodes))
        return "\n\n".join(parts)


async def gather(agent_name: str, query: str, k: int = 6) -> MemoryContext:
    ctx = MemoryContext()
    p = db.pool()
    if p is None:
        return ctx

    ctx.profile = await store.get_profile()
    ctx.preferences = await store.preferences_for(agent_name)

    try:
        qvec = llm.vec_literal(await llm.embed(query))
        fact_rows = await p.fetch(
            "select statement from hybrid_search_facts($1, $2::vector, $3)", query, qvec, k,
        )
        ctx.facts = [r["statement"] for r in fact_rows]
        msg_rows = await p.fetch(
            "select content from hybrid_search_messages($1, $2::vector, $3)", query, qvec, k,
        )
        ctx.episodes = [r["content"][:400] for r in msg_rows]
    except Exception:
        log.exception("memory retrieval failed — continuing without it")
    return ctx
