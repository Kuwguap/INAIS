"""Self-test: which parts actually work right now, and the real error for the ones that don't.

"Something broke while thinking about that" is useless to the one person who can fix it. This
pokes each dependency in turn and reports the provider's own words — a wrong model id, a
rejected key and an exhausted quota all look identical from the outside otherwise.
"""

from __future__ import annotations

import logging

from inais import db, llm
from inais.config import settings

log = logging.getLogger(__name__)

OK, BAD, OFF = "✅", "❌", "⏸"


def _short(e: Exception) -> str:
    text = str(e).replace("\n", " ")
    return f"{type(e).__name__}: {text[:220]}"


async def check_database() -> str:
    cfg = settings()
    if not cfg.db_enabled:
        return f"{OFF} database — SUPABASE_DB_URL not set"
    p = db.pool()
    if p is None:
        return f"{BAD} database — {db.last_error()}"
    try:
        await p.fetchval("select 1")
        tables = await p.fetchval(
            "select count(*) from information_schema.tables where table_schema = 'public'")
        return f"{OK} database — connected, {tables} tables"
    except Exception as e:
        return f"{BAD} database — {_short(e)}"


async def check_openai() -> str:
    cfg = settings()
    if not cfg.openai_api_key:
        return f"{OFF} OpenAI — no key"
    try:
        reply = await llm.openai_chat(
            model=cfg.triage_model,
            messages=[{"role": "user", "content": "Reply with the single word: ok"}],
            purpose="diagnostic", max_completion_tokens=5)
        return f"{OK} OpenAI {cfg.triage_model} — replied {reply.strip()[:20]!r}"
    except Exception as e:
        return f"{BAD} OpenAI {cfg.triage_model} — {_short(e)}"


async def check_agent_brain() -> str:
    """Tests the provider that actually runs the agent loop, whichever it is."""
    cfg = settings()
    provider = llm.effective_provider()
    model = cfg.openai_agent_model if provider == "openai" else cfg.agent_model
    if llm.provider_override():
        provider = f"{provider}, auto-switched from anthropic"
    if provider == "anthropic" and not cfg.anthropic_api_key:
        return f"{OFF} agent brain — BRAIN_PROVIDER=anthropic but no ANTHROPIC_API_KEY"
    if provider == "openai" and not cfg.openai_api_key:
        return f"{OFF} agent brain — no OPENAI_API_KEY"
    try:
        reply = await llm.agent_text(
            system="Reply with one word.", user="Say ok",
            max_tokens=10, purpose="diagnostic")
        return f"{OK} agent brain ({provider} {model}) — replied {reply.strip()[:20]!r}"
    except Exception as e:
        return (f"{BAD} agent brain ({provider} {model}) — {_short(e)}\n"
                f"     if that model isn't on your account, set BRAIN_PROVIDER=openai "
                f"or change AGENT_MODEL")


async def check_embeddings() -> str:
    cfg = settings()
    if not cfg.openai_api_key:
        return f"{OFF} embeddings — no OpenAI key"
    try:
        vec = await llm.embed("diagnostic")
        return f"{OK} embeddings {cfg.embedding_model} — {len(vec)} dims"
    except Exception as e:
        return f"{BAD} embeddings {cfg.embedding_model} — {_short(e)}"


async def run() -> str:
    """Every check, in the order a turn would hit them."""
    lines = ["🩺 Diagnostics", ""]
    lines.append(await check_database())
    lines.append(await check_openai())      # routing + triage
    lines.append(await check_agent_brain())  # whichever provider runs the loop
    lines.append(await check_embeddings())  # memory writes

    cfg = settings()
    lines.append("")
    lines.append(f"{OK if cfg.binance_enabled else OFF} binance"
                 f" · {OK if cfg.github_enabled else OFF} github"
                 f" · {OK if cfg.learning_enabled else OFF} autonomy")
    if any(line.startswith(BAD) for line in lines):
        lines.append("")
        lines.append("Fix the ❌ lines above — a failing model id or key is the usual cause. "
                     "/why shows the error from the last real turn.")
    return "\n".join(lines)
