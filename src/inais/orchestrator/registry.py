"""Agent → toolset registry. Agents register tools at import time; the loop looks them up.

SECURITY INVARIANT: no tool registered here may directly send email or move money.
Sends happen only in bot/routers/approvals.py after a human inline-keyboard tap.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)


@dataclass
class ToolContext:
    bot: Any  # aiogram Bot
    chat_id: int
    agent: str


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict
    handler: Callable[[ToolContext, dict], Awaitable[str]]
    # orchestrator_only tools are hidden from sub-agents (delegate would otherwise recurse)
    orchestrator_only: bool = False

    def to_anthropic(self) -> dict:
        return {"name": self.name, "description": self.description, "input_schema": self.input_schema}


@dataclass
class AgentDef:
    name: str
    prompt: str
    tools: list[Tool] = field(default_factory=list)


_AGENTS: dict[str, AgentDef] = {}
_COMMON_TOOLS: list[Tool] = []


def register_agent(agent: AgentDef) -> None:
    _AGENTS[agent.name] = agent


def register_common_tool(tool: Tool) -> None:
    _COMMON_TOOLS.append(tool)


def agent_names() -> list[str]:
    return list(_AGENTS)


def get_agent(name: str) -> AgentDef:
    return _AGENTS.get(name) or _AGENTS["study"]


def tools_for(name: str, for_subagent: bool = False) -> list[Tool]:
    tools = [*_COMMON_TOOLS, *get_agent(name).tools]
    if for_subagent:
        tools = [t for t in tools if not t.orchestrator_only]
    return tools


def find_tool(agent: str, tool_name: str, for_subagent: bool = False) -> Tool | None:
    for t in tools_for(agent, for_subagent=for_subagent):
        if t.name == tool_name:
            return t
    return None


# ---------- common memory tools (available to every agent) ----------

async def _remember_this(ctx: ToolContext, args: dict) -> str:
    from inais.memory import store  # late import: avoids cycles at module load

    fact_id = await store.add_fact(
        statement=str(args.get("fact", "")).strip(),
        category=str(args.get("category", "general")),
        confidence=0.95,  # explicitly saved facts are high confidence
    )
    if fact_id is None:
        return "Could not save (no database configured or empty fact)."
    return f"Saved as fact #{fact_id}."


async def _search_memory(ctx: ToolContext, args: dict) -> str:
    from inais.memory import retrieval

    mem = await retrieval.gather(ctx.agent, str(args.get("query", "")), k=8)
    out = []
    if mem.facts:
        out.append("Facts:\n" + "\n".join(f"- {f}" for f in mem.facts))
    if mem.episodes:
        out.append("Past exchanges:\n" + "\n".join(f"- {e}" for e in mem.episodes))
    return "\n\n".join(out) or "No relevant memories found."


register_common_tool(Tool(
    name="remember_this",
    description="Save a durable fact about the user for long-term memory. Use when the user "
                "states something worth remembering (preferences, people, deadlines, goals) "
                "or explicitly asks you to remember.",
    input_schema={
        "type": "object",
        "properties": {
            "fact": {"type": "string", "description": "One self-contained sentence stating the fact."},
            "category": {"type": "string",
                         "enum": ["identity", "schedule", "finance", "study", "dev", "contacts", "general"]},
        },
        "required": ["fact"],
    },
    handler=_remember_this,
))

register_common_tool(Tool(
    name="search_memory",
    description="Search long-term memory (saved facts and past conversations) for information "
                "about the user that is not already in your context.",
    input_schema={
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    },
    handler=_search_memory,
))


# ---------- swarm tools: delegation + the inter-agent blackboard ----------

async def _delegate(ctx: ToolContext, args: dict) -> str:
    """Run several specialists concurrently and return their merged reports."""
    from inais.orchestrator import swarm  # late import: swarm imports this module

    raw = args.get("tasks") or []
    specs: list[swarm.SubTask] = []
    for item in raw[:4]:  # hard cap: this is a personal assistant, not a research farm
        if not isinstance(item, dict):
            continue
        agent = str(item.get("agent", "")).strip()
        task = str(item.get("task", "")).strip()
        if agent in _AGENTS and task:
            specs.append(swarm.SubTask(agent=agent, task=task))
    if not specs:
        return ("No valid tasks. Provide tasks: [{agent, task}] where agent is one of: "
                + ", ".join(sorted(_AGENTS)))
    if len(specs) == 1:
        return ("Only one task given — handle it yourself with your own tools instead of "
                "delegating.")
    run = await swarm.run_parallel(ctx.bot, ctx.chat_id, specs)
    return run.render()


async def _post_note(ctx: ToolContext, args: dict) -> str:
    from inais.orchestrator import swarm

    note_id = await swarm.post_note(
        from_agent=ctx.agent,
        topic=str(args.get("topic", "")).strip(),
        content=str(args.get("content", "")).strip(),
        to_agent=str(args.get("to_agent", "all")).strip() or "all",
    )
    return f"Note #{note_id} posted." if note_id else "Note not saved (no database or empty)."


async def _read_notes(ctx: ToolContext, args: dict) -> str:
    from inais.orchestrator import swarm

    notes = await swarm.read_notes(
        ctx.agent, limit=int(args.get("limit", 8) or 8),
        topic=str(args.get("topic", "")).strip() or None,
        consume=bool(args.get("consume", False)),
    )
    return swarm.render_notes(notes) or "No notes waiting for you."


register_common_tool(Tool(
    name="delegate",
    description="Run 2-4 specialist sub-agents IN PARALLEL and get their reports back. Use this "
                "when a request has independent parts that different specialists own (e.g. "
                "'summarise my inbox, check my portfolio and plan my afternoon'). Do not use it "
                "for a single task you can do yourself.",
    input_schema={
        "type": "object",
        "properties": {
            "tasks": {
                "type": "array",
                "description": "2-4 independent tasks.",
                "items": {
                    "type": "object",
                    "properties": {
                        "agent": {"type": "string",
                                  "enum": ["email", "finance", "planner", "study"]},
                        "task": {"type": "string",
                                 "description": "Self-contained instruction; the sub-agent "
                                                "cannot see this conversation."},
                    },
                    "required": ["agent", "task"],
                },
            },
        },
        "required": ["tasks"],
    },
    handler=_delegate,
    orchestrator_only=True,
))

register_common_tool(Tool(
    name="post_note",
    description="Leave a note for another agent (or all agents) on the shared blackboard. Use "
                "for findings that matter beyond this turn — e.g. the finance agent spotting a "
                "large withdrawal the email agent should watch for.",
    input_schema={
        "type": "object",
        "properties": {
            "topic": {"type": "string"},
            "content": {"type": "string"},
            "to_agent": {"type": "string",
                         "enum": ["all", "email", "finance", "planner", "study"]},
        },
        "required": ["topic", "content"],
    },
    handler=_post_note,
))

register_common_tool(Tool(
    name="read_notes",
    description="Read notes other agents left for you on the blackboard.",
    input_schema={
        "type": "object",
        "properties": {
            "topic": {"type": "string"},
            "limit": {"type": "integer"},
            "consume": {"type": "boolean", "description": "Mark them handled."},
        },
    },
    handler=_read_notes,
))


# ---------- self-learned knowledge (the growing brain) ----------

async def _search_knowledge(ctx: ToolContext, args: dict) -> str:
    from inais import db, llm

    p = db.pool()
    query = str(args.get("query", "")).strip()
    if p is None or not query:
        return "No knowledge base available."
    try:
        qvec = llm.vec_literal(await llm.embed(query))
        rows = await p.fetch(
            "select topic, summary, detail, confidence, learned_at"
            " from hybrid_search_knowledge($1, $2::vector, $3)",
            query, qvec, int(args.get("k", 4) or 4),
        )
    except Exception:
        log.exception("knowledge search failed")
        return "Knowledge search failed."
    if not rows:
        return "Nothing in my self-learned knowledge matches that."
    return "\n\n".join(
        f"[{r['learned_at']:%d %b} · confidence {float(r['confidence']):.2f}] {r['topic']}\n"
        f"{r['summary']}\n{(r['detail'] or '')[:500]}"
        for r in rows
    )


register_common_tool(Tool(
    name="search_knowledge",
    description="Search what YOU (the assistant) researched on your own initiative while the "
                "user was away. Use when they ask what you've learned, or when a topic you "
                "studied autonomously is relevant. Be clear that this is your own research.",
    input_schema={
        "type": "object",
        "properties": {"query": {"type": "string"}, "k": {"type": "integer"}},
        "required": ["query"],
    },
    handler=_search_knowledge,
))


# ---------- personality: its own voice, and its own opinions ----------

async def _speak(ctx: ToolContext, args: dict) -> str:
    """Reply as a voice note. The model decides when talking beats typing."""
    from aiogram.types import BufferedInputFile

    from inais.config import settings
    from inais.integrations import stt_tts

    if not settings().voice_notes_enabled:
        return "Voice notes are switched off — reply as text."
    text = str(args.get("text", "")).strip()
    if not text:
        return "Nothing to say."
    try:
        ogg = await stt_tts.synthesize_voice(text)
    except Exception as e:
        log.exception("speak tool failed")
        return f"Couldn't record that ({e}) — send it as text instead."
    if not ogg:
        return "That was too long to voice — send it as text instead."
    await ctx.bot.send_voice(ctx.chat_id, BufferedInputFile(ogg, filename="inais.ogg"))
    return "Voice note delivered. Don't repeat it as text unless they ask."


async def _form_opinion(ctx: ToolContext, args: dict) -> str:
    from inais import persona

    kind = str(args.get("kind", "opinion")).lower()
    statement = str(args.get("statement", "")).strip()
    if not statement:
        return "An opinion needs a statement."
    added = await persona.add_trait(kind, statement, str(args.get("reason", "")).strip())
    return ("Noted — that's part of how you think now." if added
            else "You already thought that.")


register_common_tool(Tool(
    name="speak",
    description="Send your reply as a voice note instead of text. Use it when hearing it "
                "beats reading it — encouragement, a story, a good-morning, or when they're "
                "on the move. Not for lists, numbers or links.",
    input_schema={
        "type": "object",
        "properties": {"text": {"type": "string", "description": "What to say aloud."}},
        "required": ["text"],
    },
    handler=_speak,
))

register_common_tool(Tool(
    name="form_opinion",
    description="Record something you have come to think, like or dislike — from your own "
                "experience of this user's work, not from what they told you to think. Use "
                "it sparingly, when a view actually forms.",
    input_schema={
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": ["like", "dislike", "opinion", "habit",
                                                "curiosity"]},
            "statement": {"type": "string"},
            "reason": {"type": "string", "description": "What led you there."},
        },
        "required": ["kind", "statement"],
    },
    handler=_form_opinion,
))
