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


def tools_for(name: str) -> list[Tool]:
    return [*_COMMON_TOOLS, *get_agent(name).tools]


def find_tool(agent: str, tool_name: str) -> Tool | None:
    for t in tools_for(agent):
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
