"""Study/dev agent — the default: questions, coding help, studying, general chat.

Owns the material the user has uploaded (PDFs → doc_chunks), their exams and study plans,
quiz generation, and the narrator that reads material aloud.
"""

from __future__ import annotations

import logging
from datetime import date

from inais.orchestrator.registry import AgentDef, Tool, ToolContext, register_agent
from inais.study import quiz, store
from inais.timeutil import parse_when

log = logging.getLogger(__name__)

PROMPT = """## Your current role: study & dev helper
Help with studying, coding, explanations, planning and everyday questions.
- For code: give working, minimal examples; state assumptions briefly.
- For studying: prefer explanations that build understanding over long lectures.
- search_documents searches the user's OWN uploaded material (lecture notes, textbooks).
  Use it whenever a question could plausibly be about their course, and cite the document
  title when you answer from it. If it returns nothing, say so rather than guessing.
- create_exam when the user names an exam and a date: it generates a spaced study plan.
- generate_quiz builds multiple-choice questions from their material; tell them to run /quiz.
- narrate_document reads material aloud as voice messages — use it when they ask you to
  "read X to me" or say they want to listen while commuting.
- Use search_memory for their courses, projects and past conversations."""


async def _search_documents(ctx: ToolContext, args: dict) -> str:
    hits = await store.search_chunks(str(args.get("query", "")), k=int(args.get("k", 6) or 6))
    if not hits:
        docs = await store.list_documents(limit=5)
        if not docs:
            return "No documents ingested yet — the user can send me a PDF in Telegram."
        titles = ", ".join(d["title"] for d in docs)
        return f"Nothing matched. Available documents: {titles}"
    return "\n\n".join(
        f"[{h['title']} — chunk {h['chunk_index']}]\n{h['content'][:1200]}" for h in hits
    )


async def _list_documents(ctx: ToolContext, args: dict) -> str:
    docs = await store.list_documents()
    if not docs:
        return "No documents ingested yet."
    return "\n".join(f"#{d['id']} {d['title']} ({d['pages']} pages)" for d in docs)


async def _create_exam(ctx: ToolContext, args: dict) -> str:
    name = str(args.get("name", "")).strip()
    when = parse_when(args.get("date_iso"))
    topics = [str(t).strip() for t in (args.get("topics") or []) if str(t).strip()]
    if not name or when is None:
        return "An exam needs a name and a date (date_iso)."
    exam_date: date = when.date()
    if not topics:
        return "I need the topic list to build a plan — ask the user which topics it covers."
    try:
        exam_id, days = await store.create_exam(name, exam_date, topics)
    except Exception as e:
        log.exception("create_exam failed")
        return f"Could not create the exam: {e}"
    if days == 0:
        return (f"Exam #{exam_id} saved for {exam_date}, but it's today or in the past — "
                f"no plan generated.")
    return (f"Exam #{exam_id} '{name}' on {exam_date} saved with a {days}-day plan "
            f"over {len(topics)} topics. I'll nudge them each evening.")


async def _get_study_plan(ctx: ToolContext, args: dict) -> str:
    return await store.get_plan(str(args.get("exam", "")).strip() or None)


async def _generate_quiz(ctx: ToolContext, args: dict) -> str:
    topic = str(args.get("topic", "")).strip()
    if not topic:
        return "Which topic should the quiz cover?"
    try:
        n = int(args.get("n", 5))
    except (TypeError, ValueError):
        n = 5
    _, message = await quiz.generate(topic, n)
    return message


async def _narrate_document(ctx: ToolContext, args: dict) -> str:
    """Send the material to the user as sequential Telegram voice messages."""
    from inais.integrations import stt_tts

    topic = str(args.get("topic", "")).strip()
    doc_id = args.get("document_id")
    chunks = await store.ordered_chunks(int(doc_id) if doc_id else None, topic,
                                        limit=int(args.get("max_parts", 4) or 4))
    if not chunks:
        return "I have no ingested material for that — ask them to send the PDF first."

    from aiogram.types import BufferedInputFile

    sent = 0
    for i, c in enumerate(chunks):
        try:
            ogg = await stt_tts.synthesize_voice(c["content"][:1900])
            if not ogg:
                continue
            await ctx.bot.send_voice(
                ctx.chat_id,
                BufferedInputFile(ogg, filename=f"read_{i + 1}.ogg"),
                caption=f"{c['title']} — part {i + 1}/{len(chunks)}" if i == 0 else None,
            )
            sent += 1
        except Exception:
            log.exception("narration part %s failed", i)
            break
    if sent == 0:
        return "Narration failed (TTS or ffmpeg problem) — the text is still searchable."
    return f"Sent {sent} voice part(s) from '{chunks[0]['title']}'. Already delivered to the user."


TOOLS = [
    Tool(
        name="search_documents",
        description="Search the user's own uploaded study material (PDF lecture notes, "
                    "textbooks). Use before answering course-specific questions.",
        input_schema={
            "type": "object",
            "properties": {"query": {"type": "string"}, "k": {"type": "integer"}},
            "required": ["query"],
        },
        handler=_search_documents,
    ),
    Tool(
        name="list_documents",
        description="List the study documents that have been ingested.",
        input_schema={"type": "object", "properties": {}},
        handler=_list_documents,
    ),
    Tool(
        name="create_exam",
        description="Register an exam with its topics and generate a backward-spaced study plan.",
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "date_iso": {"type": "string", "description": "Exam date, e.g. 2026-09-04."},
                "topics": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["name", "date_iso", "topics"],
        },
        handler=_create_exam,
    ),
    Tool(
        name="get_study_plan",
        description="Show the study plan for an exam (or all upcoming exams).",
        input_schema={"type": "object", "properties": {"exam": {"type": "string"}}},
        handler=_get_study_plan,
    ),
    Tool(
        name="generate_quiz",
        description="Create multiple-choice questions from the user's material on a topic.",
        input_schema={
            "type": "object",
            "properties": {"topic": {"type": "string"}, "n": {"type": "integer"}},
            "required": ["topic"],
        },
        handler=_generate_quiz,
    ),
    Tool(
        name="narrate_document",
        description="Read the user's material aloud: sends sequential voice messages. Use when "
                    "they ask you to read/narrate something to them.",
        input_schema={
            "type": "object",
            "properties": {
                "topic": {"type": "string", "description": "What to read (searched in their docs)."},
                "document_id": {"type": "integer"},
                "max_parts": {"type": "integer", "description": "Default 4."},
            },
        },
        handler=_narrate_document,
    ),
]

register_agent(AgentDef(name="study", prompt=PROMPT, tools=TOOLS))


async def _github_status(ctx: ToolContext, args: dict) -> str:
    """Read-only: what is waiting on the user in GitHub right now."""
    from inais.config import settings
    from inais.integrations import github

    if not settings().github_enabled:
        return "GitHub isn't configured (no GITHUB_TOKEN)."
    try:
        items = await github.fetch_all()
    except github.GitHubAuthError as e:
        return f"GitHub rejected the token: {e}"
    except Exception as e:
        return f"Could not reach GitHub: {e}"
    return github.render_digest(items)


TOOLS.append(Tool(
    name="github_status",
    description="Check GitHub for pull requests awaiting the user's review, issues mentioning "
                "them, and failing CI runs. Read-only — you cannot comment, merge or push.",
    input_schema={"type": "object", "properties": {}},
    handler=_github_status,
))
