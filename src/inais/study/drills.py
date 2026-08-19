"""Interview and viva practice: ask out loud, answer out loud, get graded.

Grading reuses the brain-dump pattern — Covered / Corrections / Gaps / Well done — because
it is the shape that actually helps someone rehearse: it tells you what you said, what was
wrong, what you left out, and one true thing you did well. Answers are graded against the
question's own guidance and the user's material, never against outside knowledge.
"""

from __future__ import annotations

import logging

from inais import db, llm
from inais.config import settings
from inais.study import store

log = logging.getLogger(__name__)

CATEGORIES = ("behavioral", "technical", "viva")

GRADING_SYSTEM = """You are coaching a student through interview or viva practice. You have
the question, notes on what a strong answer covers, optionally excerpts from their own study
material, and a transcript of what they actually said out loud.

Reply in plain text with exactly these four sections:

Covered — 2-4 bullets on what they actually said.
Corrections — anything factually wrong or misleading, quoting the guidance or material. If
  nothing is wrong, say "Nothing wrong — accurate throughout." Never invent errors.
Gaps — what a strong answer includes that they did not say (max 4 bullets, most important
  first). For behavioral questions, judge structure too: situation, action, result.
Well done — one specific, genuine sentence about the strongest part of their answer. Name
  the actual thing they said. Never generic praise.

Keep the whole reply under 250 words. Speak to them directly ("you said"), not about them."""

GENERATION_SYSTEM = """Write interview or viva questions for a student. Return ONLY JSON:
{"questions": [{"question": "...", "guidance": "what a strong answer covers, 1-2 sentences"}]}

Rules:
- Questions must be answerable out loud in 60-90 seconds.
- For "viva", base every question strictly on the supplied material and name concepts from it.
- For "technical", favour understanding and trade-offs over trivia.
- For "behavioral", ask for a real situation ("Tell me about a time when...").
- guidance is a grading rubric, not a model answer to read aloud."""

# A small starting deck so /drill works before anything is generated.
SEED_BEHAVIORAL = [
    ("Tell me about a time you had to learn something difficult quickly.",
     "Should name a concrete situation, the approach taken, and what the result was; "
     "strong answers mention what they would do differently."),
    ("Describe a project you're proud of and your specific contribution.",
     "Should separate their own work from the team's, and state the impact concretely."),
    ("Tell me about a time you disagreed with someone on a team.",
     "Should show they listened to the other position and describe how it was resolved, "
     "not just that they were right."),
    ("What's a mistake you made recently, and what did you change afterwards?",
     "Should own a real mistake without deflecting, and name a specific change in behaviour."),
    ("Why this role, and why now?",
     "Should connect their actual experience and interests to the specific role; "
     "generic enthusiasm is a weak answer."),
]


async def seed_if_empty() -> int:
    """Make /drill useful on first run without requiring generation."""
    p = db.pool()
    if p is None:
        return 0
    row = await p.fetchrow("select count(*) as n from drill_questions")
    if row and row["n"]:
        return 0
    added = 0
    for question, guidance in SEED_BEHAVIORAL:
        result = await p.fetchrow(
            "insert into drill_questions (category, question, guidance)"
            " values ('behavioral', $1, $2) on conflict (lower(question)) do nothing"
            " returning id", question, guidance)
        if result:
            added += 1
    return added


async def pick(category: str | None = None, exam: str | None = None) -> dict | None:
    """Least-asked question first, so a session works through the bank rather than repeating."""
    p = db.pool()
    if p is None:
        return None
    await seed_if_empty()
    if exam:
        row = await p.fetchrow(
            "select q.* from drill_questions q join exams e on e.id = q.exam_id"
            " where e.name ilike $1 order by q.times_asked, random() limit 1", f"%{exam}%")
        if row:
            return dict(row)
    if category in CATEGORIES:
        row = await p.fetchrow(
            "select * from drill_questions where category = $1"
            " order by times_asked, random() limit 1", category)
    else:
        row = await p.fetchrow(
            "select * from drill_questions order by times_asked, random() limit 1")
    return dict(row) if row else None


async def get_question(question_id: int) -> dict | None:
    p = db.pool()
    if p is None:
        return None
    row = await p.fetchrow("select * from drill_questions where id = $1", question_id)
    return dict(row) if row else None


async def mark_asked(question_id: int) -> None:
    p = db.pool()
    if p is not None:
        await p.execute(
            "update drill_questions set times_asked = times_asked + 1, last_asked = now()"
            " where id = $1", question_id)


async def generate(category: str, topic: str = "", n: int = 5,
                   exam_id: int | None = None) -> tuple[int, str]:
    """Build questions, grounded in the user's own material for viva/technical."""
    if not settings().brain_enabled:
        return 0, "Question generation needs the LLM keys configured."
    if category not in CATEGORIES:
        category = "behavioral"

    material = ""
    if category in ("viva", "technical") and topic:
        chunks = await store.search_chunks(topic, k=5)
        material = "\n\n".join(f"[{c['title']}] {c['content'][:1200]}" for c in chunks)
        if category == "viva" and not material:
            return 0, ("I have no material on that to build viva questions from — "
                       "send me the PDF first.")

    raw = await llm.agent_text(
        system=GENERATION_SYSTEM,
        user=f"CATEGORY: {category}\nTOPIC: {topic or 'general'}\n"
                   f"Write {max(1, min(n, 10))} questions.\n\n"
                   f"MATERIAL:\n{material or '(none — use general knowledge of the category)'}",
        max_tokens=2000,
        purpose="drill_generation",
        cheap=False,
    )
    data = llm.parse_json_block(raw)

    p = db.pool()
    if p is None:
        return 0, "No database configured."
    saved = 0
    for item in (data.get("questions") or [])[:10]:
        question = str(item.get("question", "")).strip()
        if not question:
            continue
        row = await p.fetchrow(
            "insert into drill_questions (category, exam_id, question, guidance)"
            " values ($1, $2, $3, $4) on conflict (lower(question)) do nothing returning id",
            category, exam_id, question[:1000],
            str(item.get("guidance", "")).strip()[:1000] or None)
        if row:
            saved += 1
    if not saved:
        return 0, "I couldn't produce new questions there — try a narrower topic."
    return saved, f"Added {saved} {category} question(s). Run /drill to practise."


async def grade(question: dict, transcript: str) -> tuple[str, int | None]:
    """Grade a spoken answer. Returns (feedback, answer_id)."""
    transcript = transcript.strip()
    if not transcript:
        return "There was nothing in that recording to grade.", None
    if not settings().brain_enabled:
        return "Grading needs ANTHROPIC_API_KEY configured.", None

    material = ""
    if question.get("category") in ("viva", "technical"):
        chunks = await store.search_chunks(question["question"], k=4)
        material = "\n\n".join(
            f"[{c['title']}] {c['content'][:1000]}" for c in chunks)

    payload = (
        f"QUESTION ({question.get('category', 'behavioral')}): {question['question']}\n\n"
        f"WHAT A STRONG ANSWER COVERS:\n{question.get('guidance') or '(not specified)'}\n\n"
        f"THEIR OWN MATERIAL:\n{material or '(none)'}\n\n"
        f"WHAT THEY SAID:\n{transcript}"
    )
    raw = await llm.agent_text(
        system=GRADING_SYSTEM,
        user=payload,
        max_tokens=1200,
        purpose="drill_grading",
        cheap=False,
    )
    feedback = raw.strip()
    if not feedback:
        return "I couldn't grade that answer — try again?", None

    p = db.pool()
    answer_id = None
    if p is not None:
        row = await p.fetchrow(
            "insert into drill_answers (question_id, transcript, feedback)"
            " values ($1, $2, $3) returning id", question["id"], transcript, feedback)
        answer_id = row["id"]
    return feedback, answer_id


async def stats() -> str:
    p = db.pool()
    if p is None:
        return "No database configured."
    row = await p.fetchrow(
        "select (select count(*) from drill_questions) as questions,"
        " (select count(*) from drill_answers) as answered,"
        " (select count(*) from drill_answers where created_at > now() - interval '7 days')"
        "   as this_week")
    return (f"🎤 Drill bank\n"
            f"Questions: {row['questions']}\n"
            f"Answered: {row['answered']} (this week: {row['this_week']})\n"
            f"/drill to practise one now")
