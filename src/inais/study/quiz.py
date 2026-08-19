"""Quiz generation from the user's own material, and answer keyboards."""

from __future__ import annotations

import json
import logging

from inais import llm
from inais.config import settings
from inais.study import store

log = logging.getLogger(__name__)

QUIZ_SYSTEM = """You write exam-style multiple-choice questions from a student's own material.
Return ONLY JSON: {"items": [{"question": "...", "choices": ["A text", "B text", "C text",
"D text"], "answer": "the exact text of the correct choice"}]}

Rules:
- Questions must be answerable from the excerpts alone. Never use outside knowledge.
- Exactly 4 choices; exactly one correct; distractors must be plausible and similar in length.
- Test understanding (why/how/which-follows), not trivia about page numbers.
- "answer" must match one choice string character for character."""


async def generate(topic: str, n: int = 5, document_id: int | None = None) -> tuple[int, str]:
    """Generate and store quiz items. Returns (saved_count, message)."""
    if not settings().brain_enabled:
        return 0, "Quiz generation needs the LLM keys configured."
    chunks = await store.search_chunks(topic, k=6, document_id=document_id)
    if not chunks:
        return 0, ("I have no ingested material matching that. Send me the PDF first and "
                   "I'll build questions from it.")
    material = "\n\n".join(f"[{c['title']}] {c['content'][:1500]}" for c in chunks)
    raw = await llm.agent_text(
        system=QUIZ_SYSTEM,
        user=f"TOPIC: {topic}\nWrite {max(1, min(n, 10))} questions.\n\nEXCERPTS:\n{material}",
        max_tokens=2000,
        purpose="quiz_generation",
        cheap=False,
    )
    data = llm.parse_json_block(raw)
    items = [it for it in data.get("items", []) if _valid(it)]
    if not items:
        return 0, "I couldn't turn that material into clean questions — try a narrower topic."
    exam = await store.find_exam(topic)
    saved = await store.save_quiz_items(
        items, document_id=document_id, exam_id=exam["id"] if exam else None, topic=topic,
    )
    return saved, f"Made {saved} question{'s' if saved != 1 else ''} on {topic}. Run /quiz to start."


def _valid(item: dict) -> bool:
    choices = item.get("choices")
    answer = item.get("answer")
    return bool(
        isinstance(choices, list) and len(choices) >= 2
        and isinstance(answer, str) and answer in choices
        and str(item.get("question", "")).strip()
    )


def choice_index(item: dict, answer: str) -> int:
    choices = item["choices"] if isinstance(item["choices"], list) else json.loads(item["choices"])
    try:
        return choices.index(answer)
    except ValueError:
        return -1


def parse_choices(raw) -> list[str]:
    if isinstance(raw, list):
        return [str(c) for c in raw]
    try:
        return [str(c) for c in json.loads(raw or "[]")]
    except (TypeError, json.JSONDecodeError):
        return []
