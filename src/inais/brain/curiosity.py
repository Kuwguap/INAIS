"""What the assistant decides to learn next.

Candidates come from the user's own footprint — questions it answered thinly, upcoming exam
topics, task subjects, recurring conversation themes — and are ranked by the interest network
(when it has learned enough to be trustworthy) before landing in curiosity_queue.
"""

from __future__ import annotations

import logging

from inais import db, llm
from inais.brain import nn
from inais.config import settings

log = logging.getLogger(__name__)

SCOUT_SYSTEM = """You are the curiosity process of INAIS, a personal assistant. From the
material below (recent conversations, the user's exam topics, their open tasks), pick topics
this assistant should go and research on its own so it can help better next time.

Return ONLY JSON: {"topics": [{"topic": "...", "reason": "...", "priority": 0.0-1.0}]}

Rules:
- 1 to 5 topics. Each topic must be a concrete, searchable subject (3-10 words), not a task
  and not a question addressed to the user.
- Prefer gaps: things the assistant clearly did not know, or subjects the user keeps returning
  to. Skip anything already well covered in the KNOWN topics list.
- Never propose researching the user's private data (their inbox, balances, personal life) —
  these topics get looked up on the public web.
- priority reflects how much it would improve future help. Be honest; 0.3 is fine."""


async def scout(limit: int = 5) -> int:
    """Generate new candidate topics. Returns how many were queued."""
    cfg = settings()
    p = db.pool()
    if p is None or not cfg.brain_enabled:
        return 0

    msgs = await p.fetch(
        "select role, content from messages where ts > now() - interval '7 days'"
        "  and role in ('user','assistant') order by id desc limit 120",
    )
    if not msgs:
        return 0
    exams = await p.fetch(
        "select name, topics from exams where date >= current_date order by date limit 3")
    tasks = await p.fetch(
        "select title from tasks where status = 'open' order by due nulls last limit 15")
    known = await p.fetch("select topic from knowledge order by learned_at desc limit 40")
    queued = await p.fetch(
        "select topic from curiosity_queue where status in ('queued','researching') limit 40")

    transcript = "\n".join(f"[{m['role']}] {m['content'][:300]}" for m in reversed(msgs))
    exam_txt = "\n".join(f"{e['name']}: {', '.join(e['topics'] or [])}" for e in exams) or "(none)"
    task_txt = "\n".join(f"- {t['title']}" for t in tasks) or "(none)"
    known_txt = ", ".join(k["topic"] for k in known) or "(nothing yet)"
    queued_txt = ", ".join(q["topic"] for q in queued) or "(nothing queued)"

    resp = await llm.anthropic_message(
        model=cfg.reflection_model,
        system=SCOUT_SYSTEM,
        messages=[{"role": "user", "content":
                   f"RECENT CONVERSATIONS:\n{transcript}\n\nEXAM TOPICS:\n{exam_txt}\n\n"
                   f"OPEN TASKS:\n{task_txt}\n\nALREADY KNOWN topics:\n{known_txt}\n\n"
                   f"ALREADY QUEUED topics:\n{queued_txt}"}],
        max_tokens=900,
        purpose="curiosity_scout",
    )
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    data = llm.parse_json_block(text)
    topics = data.get("topics") or []

    added = 0
    for item in topics[:limit]:
        topic = str(item.get("topic", "")).strip()
        if len(topic) < 4:
            continue
        reason = str(item.get("reason", "")).strip()[:400]
        try:
            priority = float(item.get("priority", 0.5))
        except (TypeError, ValueError):
            priority = 0.5

        # let the learned interest model override the LLM's guess when it is trustworthy
        learned = await nn.score("interest", f"{topic}. {reason}")
        if learned is not None:
            priority = 0.35 * priority + 0.65 * learned
            reason = f"{reason} [interest net: {learned:.2f}]".strip()

        row = await p.fetchrow(
            "insert into curiosity_queue (topic, reason, priority) values ($1, $2, $3)"
            " on conflict (lower(topic)) do nothing returning id",
            topic[:200], reason, max(0.0, min(1.0, priority)),
        )
        if row is not None:
            added += 1
    if added:
        log.info("curiosity: queued %s new topic(s)", added)
    return added


async def pick(n: int = 2) -> list[dict]:
    """Claim the top queued topics for research (atomically, so cycles can't collide)."""
    p = db.pool()
    if p is None:
        return []
    rows = await p.fetch(
        "update curiosity_queue set status = 'researching', picked_at = now()"
        " where id in (select id from curiosity_queue where status = 'queued'"
        "              order by priority desc, id limit $1)"
        " returning id, topic, reason, priority",
        max(1, n),
    )
    return [dict(r) for r in rows]


async def finish(queue_id: int, knowledge_id: int | None, skipped: bool = False) -> None:
    p = db.pool()
    if p is None:
        return
    await p.execute(
        "update curiosity_queue set status = $1, knowledge_id = $2 where id = $3",
        "skipped" if skipped else "done", knowledge_id, queue_id,
    )


async def queue_summary(limit: int = 10) -> str:
    p = db.pool()
    if p is None:
        return "No database configured."
    rows = await p.fetch(
        "select topic, priority, status from curiosity_queue"
        " where status in ('queued','researching') order by priority desc limit $1", limit)
    if not rows:
        return "Nothing queued — I'll scout new topics on the next idle cycle."
    return "\n".join(
        f"• {r['topic']} (priority {float(r['priority']):.2f}, {r['status']})" for r in rows)
