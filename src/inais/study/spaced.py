"""Spaced repetition over arbitrary material.

The scheduling rule lives here and is shared with the generated quizzes in `quiz.py`, so
both move on the same curve: a correct answer doubles the interval (capped at 30 days), a
miss halves it (floor 1 day). Cards can come from anywhere — a fact from conversation, a
snippet from a PDF, something the user asked to be drilled on.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from inais import db

log = logging.getLogger(__name__)

MAX_INTERVAL_DAYS = 30
SOURCE_KINDS = ("manual", "conversation", "document", "fact", "quiz")


def next_interval(current: int, correct: bool) -> int:
    """Right answers double the interval (cap 30 days); wrong answers halve it (floor 1)."""
    if correct:
        return min(max(1, current) * 2, MAX_INTERVAL_DAYS)
    return max(1, max(1, current) // 2)


async def add_card(front: str, back: str, source_kind: str = "manual",
                   source_id: int | None = None, topic: str | None = None) -> int | None:
    """Create a card. Returns None if it duplicates an existing front."""
    p = db.pool()
    front, back = front.strip(), back.strip()
    if p is None or not front or not back:
        return None
    if source_kind not in SOURCE_KINDS:
        source_kind = "manual"
    row = await p.fetchrow(
        "insert into review_items (front, back, source_kind, source_id, topic)"
        " values ($1, $2, $3, $4, $5)"
        " on conflict (lower(front)) do nothing returning id",
        front[:1000], back[:2000], source_kind, source_id, topic,
    )
    return row["id"] if row else None


async def due_card(topic: str | None = None) -> dict | None:
    """The card most overdue, optionally within a topic."""
    p = db.pool()
    if p is None:
        return None
    if topic:
        row = await p.fetchrow(
            "select * from review_items where next_review <= current_date"
            "   and (topic ilike $1 or front ilike $1)"
            " order by next_review, times_asked limit 1", f"%{topic}%")
    else:
        row = await p.fetchrow(
            "select * from review_items where next_review <= current_date"
            " order by next_review, times_asked limit 1")
    return dict(row) if row else None


async def get_card(card_id: int) -> dict | None:
    p = db.pool()
    if p is None:
        return None
    row = await p.fetchrow("select * from review_items where id = $1", card_id)
    return dict(row) if row else None


async def record_answer(card_id: int, correct: bool) -> int:
    """Update the schedule. Returns the new interval in days."""
    p = db.pool()
    if p is None:
        return 1
    row = await p.fetchrow("select interval_days from review_items where id = $1", card_id)
    interval = next_interval(int(row["interval_days"]) if row else 1, correct)
    await p.execute(
        "update review_items set times_asked = times_asked + 1,"
        " times_correct = times_correct + $1, interval_days = $2,"
        " next_review = current_date + $2, last_reviewed = $3 where id = $4",
        1 if correct else 0, interval, datetime.now(UTC), card_id,
    )
    return interval


async def delete_card(card_id: int) -> str | None:
    p = db.pool()
    if p is None:
        return None
    row = await p.fetchrow("delete from review_items where id = $1 returning front", card_id)
    return row["front"] if row else None


async def stats() -> str:
    p = db.pool()
    if p is None:
        return "No database configured."
    row = await p.fetchrow(
        "select count(*) as total,"
        " count(*) filter (where next_review <= current_date) as due,"
        " coalesce(sum(times_asked), 0) as asked,"
        " coalesce(sum(times_correct), 0) as correct from review_items")
    if not row or not row["total"]:
        return ("🗂 No review cards yet.\n"
                "Say \"remember this for review\" or add them from your documents.")
    accuracy = (row["correct"] / row["asked"] * 100) if row["asked"] else 0
    return (f"🗂 Review deck\n"
            f"Cards: {row['total']} · due now: {row['due']}\n"
            f"Answered: {row['asked']} · accuracy {accuracy:.0f}%\n"
            f"/card to review one now")
