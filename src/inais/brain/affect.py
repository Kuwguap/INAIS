"""A running read of how the user seems to be doing, and how to meet them there.

This is pattern-matching on their own recent words — never mind-reading and never diagnosis.
The output is one steering sentence for the persona ("they've sounded stretched thin; keep it
short and warm"), cached so it costs one cheap call per half hour of active chat, not one per
message. The prompt forbids clinical language outright: the assistant adapts its register,
it does not label the user.
"""

from __future__ import annotations

import logging
import time

from inais import db, llm
from inais.config import settings

log = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 30 * 60
_cache: tuple[float, str] | None = None   # (computed_at, line)

READ_SYSTEM = """You read a user's recent messages and journal trend, and produce ONE short
steering sentence for their assistant — how to pitch the next replies.

Examples of the register: "They're in a focused grind — be brisk and skip the warmth." /
"They've sounded stretched thin the last few days — keep it short, warm, no new demands." /
"Upbeat and curious today — it's a good moment to go deeper."

Hard rules:
- Describe tone and energy ONLY as evidenced by their own words. Never guess at causes.
- NEVER use clinical or diagnostic language (no "depressed", "anxiety", "burnout", etc.).
- If the messages are neutral or there's too little to go on, reply exactly: NEUTRAL
- One sentence, no preamble."""


def invalidate() -> None:
    global _cache
    _cache = None


async def _compute() -> str:
    p = db.pool()
    if p is None or not settings().brain_enabled:
        return ""
    msgs = await p.fetch(
        "select content from messages where role = 'user'"
        " and ts > now() - interval '48 hours' order by id desc limit 15")
    if len(msgs) < 3:
        return ""   # too little signal to say anything responsibly
    mood = await p.fetchrow(
        "select avg(mood_score) as score, count(*) as n from journal_entries"
        " where created_at > now() - interval '5 days'")
    mood_line = (f"Journal trend over {mood['n']} entries: {float(mood['score']):+.1f}"
                 if mood and mood["n"] else "No recent journal entries.")
    sample = "\n".join(f"- {m['content'][:200]}" for m in msgs)
    try:
        line = await llm.agent_text(
            system=READ_SYSTEM,
            user=f"{mood_line}\n\nTHEIR RECENT MESSAGES (newest first):\n{sample}",
            max_tokens=100, purpose="affect", cheap=True)
    except Exception:
        log.exception("affect read failed")
        return ""
    line = (line or "").strip()
    if not line or line.upper().startswith("NEUTRAL"):
        return ""
    return line[:300]


async def current_line() -> str:
    """The steering sentence, or empty. Cached for CACHE_TTL_SECONDS."""
    global _cache
    now = time.time()
    if _cache is not None and now - _cache[0] < CACHE_TTL_SECONDS:
        return _cache[1]
    line = await _compute()
    _cache = (now, line)
    return line
