"""Nightly reflection: distill the day into facts, style rules and a fresh user profile.

This is the 'learning' loop: it never touches model weights — it curates what gets
retrieved and injected into every future prompt (RAG personalization).
"""

from __future__ import annotations

import json
import logging

from inais import db, llm
from inais.config import settings
from inais.memory import store

log = logging.getLogger(__name__)

REFLECTION_SYSTEM = """You are the memory-consolidation process of INAIS, a personal assistant.
You will receive: the user's current profile document, currently known facts (with ids),
recent conversation transcripts, and email drafts the user manually edited before sending.

Return ONLY a JSON object:
{
  "new_facts": [{"statement": "...", "category": "identity|schedule|finance|study|dev|contacts|general", "confidence": 0.0-1.0}],
  "superseded": [{"fact_id": 123, "replacement": "corrected statement"}],
  "style_rules": [{"agent_name": "email|finance|study|all", "rule": "..."}],
  "profile": "a compact rewritten profile document (<= 350 words) describing the user"
}

Guidelines:
- Facts must be durable and about the USER (not about the assistant or one-off trivia).
- Only supersede a fact when the transcripts clearly contradict it.
- Derive style_rules from how the user EDITED drafts (tone, length, sign-off, phrasing) and
  from explicit instructions in the transcripts. Rules must be short imperatives.
- The profile is a rewrite, not an append: merge old + new, drop stale details.
- If there is nothing to record, return empty lists and the unchanged profile."""


async def run_reflection() -> str:
    """Returns a short human-readable summary of what was learned (for /reflect)."""
    p = db.pool()
    if p is None:
        return "Reflection skipped: no database configured."

    msgs = await p.fetch(
        "select id, role, content from messages where ts > now() - interval '36 hours'"
        " and role in ('user','assistant') order by id asc limit 400",
    )
    if not msgs:
        return "Nothing new to reflect on."

    facts = await store.active_facts(limit=200)
    profile = await store.get_profile()
    edited = await p.fetch(
        "select id, body, user_edit from drafts"
        " where user_edit is not null and edit_processed_at is null limit 20",
    )

    transcript = "\n".join(f"[{m['role']}] {m['content'][:500]}" for m in msgs)
    facts_txt = "\n".join(f"(id={f['id']}) {f['statement']}" for f in facts) or "(none)"
    edits_txt = "\n\n".join(
        f"DRAFT #{d['id']}\n--- assistant wrote ---\n{d['body'][:800]}\n--- user changed it to ---\n{d['user_edit'][:800]}"
        for d in edited
    ) or "(none)"

    user_payload = (
        f"CURRENT PROFILE:\n{profile or '(empty)'}\n\n"
        f"KNOWN FACTS:\n{facts_txt}\n\n"
        f"USER-EDITED DRAFTS:\n{edits_txt}\n\n"
        f"TRANSCRIPTS (last 36h):\n{transcript}"
    )

    resp = await llm.anthropic_message(
        model=settings().reflection_model,
        system=REFLECTION_SYSTEM,
        messages=[{"role": "user", "content": user_payload}],
        max_tokens=3000,
        purpose="reflection",
    )
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    data = llm.parse_json_block(text)
    if not data:
        return "Reflection ran but produced no parseable output."

    added = 0
    for f in data.get("new_facts", []):
        try:
            await store.add_fact(f["statement"], f.get("category", "general"),
                                 float(f.get("confidence", 0.8)))
            added += 1
        except Exception:
            log.exception("failed to add fact %s", json.dumps(f)[:200])

    superseded = 0
    for s in data.get("superseded", []):
        try:
            await store.supersede_fact(int(s["fact_id"]), s["replacement"])
            superseded += 1
        except Exception:
            log.exception("failed to supersede fact %s", s)

    rules = 0
    for r in data.get("style_rules", []):
        try:
            await store.add_preference(r.get("agent_name", "all"), r["rule"], source="edit_correction"
                                       if edited else "inferred")
            rules += 1
        except Exception:
            log.exception("failed to add style rule %s", r)

    if data.get("profile"):
        await store.set_profile(data["profile"])

    if edited:
        await p.execute(
            "update drafts set edit_processed_at = now() where id = any($1::bigint[])",
            [d["id"] for d in edited],
        )

    summary = f"Reflection done: +{added} facts, {superseded} superseded, +{rules} style rules, profile refreshed."
    log.info(summary)
    return summary
