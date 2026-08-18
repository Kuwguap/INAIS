"""The autonomous learning cycle — what the assistant does while the user is away.

One cycle:
  1. gate: learning on? user idle? today's autonomy budget left?
  2. pick topics from curiosity_queue (scouting new ones first if it's empty)
  3. fan researchers out IN PARALLEL — each searches the web and writes a cited note
  4. a curator sub-agent reads what the researchers posted on the blackboard and decides what
     is worth telling the user, distilling durable bits into long-term facts

Nothing here messages the user unprompted except the curator's optional digest, which is
capped to once per day. Everything else surfaces via /learned and the morning brief.
"""

from __future__ import annotations

import asyncio
import logging

from inais import db
from inais.brain import curiosity, research
from inais.config import settings
from inais.orchestrator import swarm

log = logging.getLogger(__name__)

CURATOR_TASK = """Read the notes your researcher sub-agents left on the blackboard (use
read_notes with consume=true). Then:
1. If anything is genuinely worth the user knowing, state it in at most 3 short lines.
2. Save anything durable about the USER's world (their course, their tools, their market) with
   remember_this — not general trivia.
3. If nothing is worth surfacing, reply exactly: NOTHING
Do not greet the user and do not pad."""


async def _spent_today() -> float:
    p = db.pool()
    if p is None:
        return 0.0
    row = await p.fetchrow(
        "select coalesce(sum(cost_usd), 0) as c from llm_usage"
        " where ts >= current_date and purpose in"
        " ('curiosity_scout','research_synthesis','subagent:study','curator','embedding')",
    )
    return float(row["c"]) if row else 0.0


async def minutes_since_user() -> float | None:
    """How long since the user last said anything. None when unknown (no DB / no messages)."""
    p = db.pool()
    if p is None:
        return None
    row = await p.fetchrow(
        "select extract(epoch from (now() - max(ts))) / 60 as mins from messages"
        " where role = 'user'",
    )
    if row is None or row["mins"] is None:
        return None
    return float(row["mins"])


async def should_run() -> tuple[bool, str]:
    cfg = settings()
    if not cfg.learning_enabled:
        return False, "learning disabled (set LEARNING_ENABLED=true)"
    if not cfg.brain_enabled or db.pool() is None:
        return False, "needs API keys and a database"
    idle = await minutes_since_user()
    if idle is not None and idle < cfg.autonomy_idle_minutes:
        return False, f"user active {idle:.0f} min ago (idle threshold {cfg.autonomy_idle_minutes})"
    spent = await _spent_today()
    if spent >= cfg.autonomy_daily_budget_usd:
        return False, f"daily learning budget spent (${spent:.2f})"
    return True, "ok"


async def run_cycle(bot, force: bool = False) -> str:
    """One learning cycle. Returns a human-readable outcome (used by /learn now)."""
    cfg = settings()
    if not force:
        ok, why = await should_run()
        if not ok:
            log.debug("autonomy skipped: %s", why)
            return f"Skipped: {why}"
    elif not cfg.brain_enabled or db.pool() is None:
        return "Skipped: needs API keys and a database."

    topics = await curiosity.pick(cfg.autonomy_topics_per_cycle)
    if not topics:
        await curiosity.scout()
        topics = await curiosity.pick(cfg.autonomy_topics_per_cycle)
    if not topics:
        return "Nothing to learn right now — no candidate topics."

    # researchers run concurrently — the cycle costs one topic's wall-clock, not the sum
    async def research_one(item: dict) -> str | None:
        try:
            knowledge_id = await research.research_topic(item["topic"], item.get("reason") or "")
        except Exception:
            log.exception("research failed for %r", item["topic"])
            knowledge_id = None
        await curiosity.finish(item["id"], knowledge_id, skipped=knowledge_id is None)
        if knowledge_id is None:
            return None
        # hand the finding to the specialists through the blackboard
        row = await research.get(knowledge_id)
        if row is not None:
            await swarm.post_note(
                from_agent="researcher",
                topic=item["topic"],
                content=research.render_note(row, with_sources=False)[:1500],
                to_agent="all",
            )
        return item["topic"]

    outcomes = await asyncio.gather(*(research_one(i) for i in topics), return_exceptions=True)
    learned = [o for o in outcomes if isinstance(o, str)]
    for o in outcomes:
        if isinstance(o, BaseException):
            log.exception("researcher crashed", exc_info=o)

    if not learned:
        return "Ran a cycle but established nothing useful."

    log.info("autonomy: learned %s", ", ".join(learned))
    await _curate(bot)
    return "Learned: " + ", ".join(learned)


async def _curate(bot) -> None:
    """Second wave: one sub-agent reads the researchers' notes and consolidates them."""
    try:
        run = await swarm.run_parallel(
            bot, settings().owner_telegram_id,
            [swarm.SubTask(agent="study", task=CURATOR_TASK, label="curator")],
        )
    except Exception:
        log.exception("curation wave failed")
        return
    if not run.results:
        return
    output = run.results[0].output.strip()
    if not output or output.upper().startswith("NOTHING"):
        return
    log.info("curator produced a digest (%s chars) — held for /learned", len(output))
    await swarm.post_note("curator", "digest", output, to_agent="all")


async def scheduled_cycle(bot) -> None:
    """APScheduler entry point — never raises."""
    try:
        await run_cycle(bot)
    except Exception:
        log.exception("autonomous learning cycle failed")


async def summary(limit: int = 5) -> str:
    """What /learned shows."""
    cfg = settings()
    rows = await research.recent(limit=limit)
    if not rows:
        queue = await curiosity.queue_summary(limit=5)
        state = "on" if cfg.learning_enabled else "off (LEARNING_ENABLED=false)"
        return f"I haven't learned anything on my own yet. Learning is {state}.\n\nQueue:\n{queue}"
    parts = ["🧠 What I've taught myself lately:"]
    for r in rows:
        when = r["learned_at"].strftime("%d %b %H:%M")
        parts.append(f"\n[{when}] {research.render_note(r)}")
    return "\n".join(parts)
