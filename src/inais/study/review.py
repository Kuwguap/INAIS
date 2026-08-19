"""Brain-dump review: the student recalls a topic out loud, INAIS checks it against the source.

This is deliberately not a grader. It returns what they covered, what was wrong, what they
missed, and one genuine commendation — the shape that actually helps recall.
"""

from __future__ import annotations

import logging

from inais import db, llm
from inais.config import settings
from inais.study import store

log = logging.getLogger(__name__)

AUTO_REVIEW_WINDOW_MINUTES = 20
STUDY_LABEL_HINTS = ("study", "revis", "exam", "lecture", "chapter", "notes", "read")

REVIEW_SYSTEM = """You are INAIS reviewing a student's spoken recap ("brain dump") of what they
just studied. You have their transcript and excerpts from their own source material.

Reply in plain text with exactly these four sections, in this order:

Covered — 2-4 bullets summarising what they actually explained.
Corrections — anything they stated that contradicts the source material. Quote the source
  briefly. If nothing is wrong, say "Nothing factually wrong — good." Do not invent errors.
Gaps — important points in the source material they did not mention (max 4 bullets, most
  important first). If the material is thin, say so instead of padding.
Well done — one specific, genuine sentence about the thing they explained best. Be concrete
  (name the concept), never generic praise.

Rules: judge ONLY against the provided excerpts and topics — never from outside knowledge.
If the excerpts are empty, say you have no material for this topic and give only Covered plus
a short list of questions they should check. Keep the whole reply under 250 words."""


async def recent_study_pomodoro() -> dict | None:
    """A study-ish focus session that finished in the last few minutes, if any."""
    p = db.pool()
    if p is None:
        return None
    row = await p.fetchrow(
        "select id, label, minutes, ended_at from pomodoro_sessions"
        " where completed and ended_at > now() - make_interval(mins => $1)"
        " order by ended_at desc limit 1",
        AUTO_REVIEW_WINDOW_MINUTES,
    )
    if row is None:
        return None
    label = (row["label"] or "").lower()
    if not label:
        return None
    if any(h in label for h in STUDY_LABEL_HINTS):
        return dict(row)
    exam = await store.find_exam(label)      # a label naming an exam counts as studying
    return dict(row) if exam else None


async def run_review(transcript: str, hint: str | None = None) -> tuple[str, int | None]:
    """Compare a spoken recap against the user's material. Returns (feedback, review_id)."""
    transcript = transcript.strip()
    if not transcript:
        return "There was nothing in that recording to review.", None
    if not settings().brain_enabled:
        return "Review needs ANTHROPIC_API_KEY and OPENAI_API_KEY configured.", None

    exam = None
    topic = (hint or "").strip()
    if topic:
        exam = await store.find_exam(topic)
    if exam is None:
        exam = await store.upcoming_exam()
    topics = list(exam["topics"]) if exam and exam.get("topics") else []

    # search the user's own documents with the recap itself plus the exam topics
    query = " ".join(filter(None, [topic, transcript[:600], " ".join(topics[:5])]))
    chunks = await store.search_chunks(query, k=6)
    material = "\n\n".join(
        f"[{c['title']} #{c['chunk_index']}] {c['content'][:1200]}" for c in chunks
    ) or "(no source material found)"

    payload = (
        f"EXAM/TOPIC: {exam['name'] if exam else (topic or 'unspecified')}\n"
        f"TOPIC LIST: {', '.join(topics) if topics else '(none recorded)'}\n\n"
        f"SOURCE EXCERPTS FROM THE STUDENT'S OWN MATERIAL:\n{material}\n\n"
        f"STUDENT'S SPOKEN RECAP:\n{transcript}"
    )
    raw = await llm.agent_text(
        system=REVIEW_SYSTEM,
        user=payload,
        max_tokens=1200,
        purpose="study_review",
        cheap=False,
    )
    feedback = raw.strip()
    if not feedback:
        return "I couldn't produce a review for that recap — try again?", None

    review_id = await store.save_review(
        transcript=transcript,
        feedback=feedback,
        exam_id=exam["id"] if exam else None,
        topic=topic or (exam["name"] if exam else None),
        score_note=f"{len(chunks)} source chunks used",
    )
    return feedback, review_id
