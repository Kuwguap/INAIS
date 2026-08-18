"""Study data access: documents, exams, study plans, quiz items, reviews."""

from __future__ import annotations

import json
import logging
from datetime import date, timedelta

from inais import db, llm
# the scheduling curve is shared with review_items so both decks behave identically
from inais.study.spaced import next_interval

log = logging.getLogger(__name__)


# ---------- documents ----------

async def search_chunks(query: str, k: int = 6, document_id: int | None = None) -> list[dict]:
    """Hybrid RRF search over ingested PDFs. Returns [] if embedding or the query fails.

    When narrowing to one document we over-fetch and filter afterwards: the RRF function
    applies its own limit first, so filtering inside the same LIMIT would drop matches that
    simply ranked below chunks from other documents.
    """
    p = db.pool()
    if p is None or not query.strip():
        return []
    fetch_k = min(k * 5, 30) if document_id else k
    try:
        qvec = llm.vec_literal(await llm.embed(query))
        rows = await p.fetch(
            "select c.content, c.chunk_index, d.title, c.document_id"
            " from hybrid_search_doc_chunks($1, $2::vector, $3) c"
            " join documents d on d.id = c.document_id",
            query, qvec, fetch_k,
        )
    except Exception:
        log.exception("doc chunk search failed")
        return []
    hits = [dict(r) for r in rows]
    if document_id is not None:
        hits = [h for h in hits if h["document_id"] == document_id][:k]
    return hits


async def ordered_chunks(document_id: int | None, topic: str, limit: int = 8) -> list[dict]:
    """Chunks in reading order — used by the narrator."""
    p = db.pool()
    if p is None:
        return []
    if document_id is None:
        hits = await search_chunks(topic, k=3)
        if not hits:
            return []
        # anchor on the best-ranked hit's document, and start at the earliest chunk that
        # matched *within that document* (other documents' hits must not move the start)
        document_id = hits[0]["document_id"]
        start = min(h["chunk_index"] for h in hits if h["document_id"] == document_id)
    else:
        start = 0
    rows = await p.fetch(
        "select c.content, c.chunk_index, d.title from doc_chunks c"
        " join documents d on d.id = c.document_id"
        " where c.document_id = $1 and c.chunk_index >= $2"
        " order by c.chunk_index limit $3",
        document_id, start, limit,
    )
    return [dict(r) for r in rows]


async def list_documents(limit: int = 20) -> list[dict]:
    p = db.pool()
    if p is None:
        return []
    rows = await p.fetch(
        "select id, title, pages, uploaded_at from documents order by uploaded_at desc limit $1",
        limit,
    )
    return [dict(r) for r in rows]


# ---------- exams + study plan ----------

async def create_exam(name: str, exam_date: date, topics: list[str]) -> tuple[int, int]:
    """Create an exam and generate a backward-spaced plan. Returns (exam_id, plan_days)."""
    p = db.pool()
    if p is None:
        raise RuntimeError("No database configured.")
    row = await p.fetchrow(
        "insert into exams (name, date, topics) values ($1, $2, $3) returning id",
        name, exam_date, topics,
    )
    exam_id = row["id"]
    plan = build_plan(exam_date, topics)
    for day, focus in plan:
        await p.execute(
            "insert into study_plan (exam_id, day, focus) values ($1, $2, $3)",
            exam_id, day, focus,
        )
    return exam_id, len(plan)


def build_plan(exam_date: date, topics: list[str], today: date | None = None) -> list[tuple[date, str]]:
    """First pass over topics early, spaced review as the exam approaches.

    The last two days are always review/practice; earlier days walk the topic list.
    """
    today = today or date.today()
    days = (exam_date - today).days
    if days <= 0 or not topics:
        return []
    plan: list[tuple[date, str]] = []
    review_days = min(2, max(1, days // 4)) if days >= 3 else 0
    learning_days = days - review_days

    for i in range(learning_days):
        day = today + timedelta(days=i)
        topic = topics[i % len(topics)]
        if i < len(topics):
            plan.append((day, f"First pass: {topic}"))
        else:
            plan.append((day, f"Review + practice questions: {topic}"))
    for j in range(review_days):
        day = today + timedelta(days=learning_days + j)
        plan.append((day, "Full review: weakest topics first, then a timed practice run"))
    return plan


async def get_plan(exam_name: str | None = None) -> str:
    p = db.pool()
    if p is None:
        return "No database configured."
    if exam_name:
        rows = await p.fetch(
            "select e.name, e.date, sp.day, sp.focus, sp.done from study_plan sp"
            " join exams e on e.id = sp.exam_id where e.name ilike $1"
            " order by sp.day limit 40", f"%{exam_name}%",
        )
    else:
        rows = await p.fetch(
            "select e.name, e.date, sp.day, sp.focus, sp.done from study_plan sp"
            " join exams e on e.id = sp.exam_id where e.date >= current_date"
            " order by sp.day limit 40",
        )
    if not rows:
        return "No study plan yet — create an exam first."
    out = [f"Plan for {rows[0]['name']} (exam {rows[0]['date']}):"]
    for r in rows:
        mark = "✅" if r["done"] else "•"
        out.append(f"{mark} {r['day']}: {r['focus']}")
    return "\n".join(out)


async def mark_plan_done(plan_id: int) -> bool:
    p = db.pool()
    if p is None:
        return False
    row = await p.fetchrow(
        "update study_plan set done = true where id = $1 and not done returning id", plan_id,
    )
    return row is not None


async def find_exam(name_fragment: str) -> dict | None:
    p = db.pool()
    if p is None:
        return None
    row = await p.fetchrow(
        "select id, name, date, topics from exams where name ilike $1"
        " order by date desc limit 1", f"%{name_fragment}%",
    )
    return dict(row) if row else None


async def upcoming_exam() -> dict | None:
    p = db.pool()
    if p is None:
        return None
    row = await p.fetchrow(
        "select id, name, date, topics from exams where date >= current_date"
        " order by date limit 1",
    )
    return dict(row) if row else None


# ---------- quizzes ----------

async def save_quiz_items(items: list[dict], document_id: int | None = None,
                          exam_id: int | None = None, topic: str | None = None) -> int:
    p = db.pool()
    if p is None:
        return 0
    saved = 0
    for it in items:
        question = str(it.get("question", "")).strip()
        answer = str(it.get("answer", "")).strip()
        if not question or not answer:
            continue
        choices = it.get("choices") or []
        await p.execute(
            "insert into quiz_items (document_id, exam_id, topic, question, answer, choices)"
            " values ($1, $2, $3, $4, $5, $6::jsonb)",
            document_id, exam_id, topic, question, answer, json.dumps(choices),
        )
        saved += 1
    return saved


async def due_quiz_item(topic: str | None = None) -> dict | None:
    p = db.pool()
    if p is None:
        return None
    if topic:
        row = await p.fetchrow(
            "select * from quiz_items where next_review <= current_date"
            "  and (topic ilike $1 or question ilike $1)"
            " order by next_review, times_asked limit 1", f"%{topic}%",
        )
        if row is None:  # fall back to any item on that topic
            row = await p.fetchrow(
                "select * from quiz_items where topic ilike $1 or question ilike $1"
                " order by times_asked limit 1", f"%{topic}%",
            )
    else:
        row = await p.fetchrow(
            "select * from quiz_items where next_review <= current_date"
            " order by next_review, times_asked limit 1",
        )
    return dict(row) if row else None


async def get_quiz_item(item_id: int) -> dict | None:
    p = db.pool()
    if p is None:
        return None
    row = await p.fetchrow("select * from quiz_items where id = $1", item_id)
    return dict(row) if row else None


# the scheduling curve is shared with review_items so both decks behave identically


async def record_quiz_answer(item_id: int, correct: bool) -> int:
    """Updates spaced-repetition state. Returns the new interval in days."""
    p = db.pool()
    if p is None:
        return 1
    row = await p.fetchrow("select interval_days from quiz_items where id = $1", item_id)
    interval = next_interval(int(row["interval_days"]) if row else 1, correct)
    await p.execute(
        "update quiz_items set times_asked = times_asked + 1,"
        " times_correct = times_correct + $1, interval_days = $2,"
        " next_review = current_date + $2 where id = $3",
        1 if correct else 0, interval, item_id,
    )
    return interval


# ---------- brain-dump reviews ----------

async def save_review(transcript: str, feedback: str, exam_id: int | None = None,
                      topic: str | None = None, score_note: str | None = None) -> int | None:
    p = db.pool()
    if p is None:
        return None
    row = await p.fetchrow(
        "insert into study_reviews (exam_id, topic, transcript, feedback, score_note)"
        " values ($1, $2, $3, $4, $5) returning id",
        exam_id, topic, transcript, feedback, score_note,
    )
    return row["id"]
