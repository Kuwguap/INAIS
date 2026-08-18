"""The brain. Simple turns → cheap OpenAI model; tool-heavy turns → Anthropic tool loop.

Each conversation is pinned to one model while active (prompt caches are provider+model
scoped) — a session escalated to the agent model stays there until 30 min of idle or /reset.
"""

from __future__ import annotations

import asyncio
import logging
import time

from inais import llm
from inais.config import settings
from inais.memory import retrieval, store
from inais.orchestrator import registry, router

log = logging.getLogger(__name__)

MAX_TOOL_ITERATIONS = 8
SESSION_TTL_SECONDS = 30 * 60

# chat_id -> (pinned_to_agent_model, last_activity_ts)
_sessions: dict[int, tuple[bool, float]] = {}

BASE_PERSONA = """You are INAIS, {owner}'s personal assistant, living in Telegram.
You are direct, warm and concise — this is a chat app, so prefer short answers unless the
task genuinely needs depth. Plain text only (no markdown tables, minimal formatting).
You have long-term memory: use remember_this when the user shares something durable,
and search_memory when you might already know something relevant.
Never claim to have sent an email — you can only create drafts that the user approves."""


def _session_pinned(chat_id: int) -> bool:
    pinned, ts = _sessions.get(chat_id, (False, 0.0))
    return pinned and (time.time() - ts) < SESSION_TTL_SECONDS


def _touch_session(chat_id: int, pinned: bool) -> None:
    _sessions[chat_id] = (pinned, time.time())


def reset_session(chat_id: int) -> None:
    _sessions.pop(chat_id, None)


def _build_system(agent: registry.AgentDef, memory_text: str) -> str:
    parts = [BASE_PERSONA.format(owner="the user"), agent.prompt]
    if memory_text:
        parts.append(memory_text)
    return "\n\n".join(p for p in parts if p)


async def handle_text(bot, chat_id: int, text: str) -> str:
    """Main entry: route, run, persist. Returns the reply text."""
    cfg = settings()
    if not cfg.brain_enabled:
        return ("I'm alive, but my brain isn't wired up yet — set ANTHROPIC_API_KEY and "
                "OPENAI_API_KEY in .env to enable it. (M0 echo mode)\n\nYou said: " + text)

    r = await router.route(text)
    if _session_pinned(chat_id):
        r = router.Route(r.agent, "complex")

    agent = registry.get_agent(r.agent)
    mem = await retrieval.gather(agent.name, text)
    # fetch history BEFORE saving the current message, or it would appear twice in the prompt
    history = await store.recent_history(chat_id)
    user_msg_id = await store.save_message(chat_id, "user", text)
    if user_msg_id is not None:
        asyncio.create_task(store.embed_message(user_msg_id, text))

    if r.complexity == "simple":
        reply = await _simple_turn(agent, mem, history, text)
        _touch_session(chat_id, pinned=False)
    else:
        reply = await _agent_turn(bot, chat_id, agent, mem, history, text)
        _touch_session(chat_id, pinned=True)

    reply = reply.strip() or "(no reply)"
    asst_msg_id = await store.save_message(chat_id, "assistant", reply)
    if asst_msg_id is not None:
        asyncio.create_task(store.embed_message(asst_msg_id, reply))
    return reply


async def _simple_turn(agent, mem, history: list[dict], text: str) -> str:
    messages = [{"role": "system", "content": _build_system(agent, mem.render())}]
    messages += history[-8:]
    messages.append({"role": "user", "content": text})
    return await llm.openai_chat(
        model=settings().triage_model, messages=messages, purpose=f"simple:{agent.name}",
    )


async def _agent_turn(bot, chat_id: int, agent, mem, history: list[dict], text: str) -> str:
    cfg = settings()
    tools = registry.tools_for(agent.name)
    system = [{
        "type": "text",
        "text": _build_system(agent, mem.render()),
        "cache_control": {"type": "ephemeral"},
    }]
    messages: list[dict] = [*history, {"role": "user", "content": text}]
    ctx = registry.ToolContext(bot=bot, chat_id=chat_id, agent=agent.name)

    final_text = ""
    for _ in range(MAX_TOOL_ITERATIONS):
        resp = await llm.anthropic_message(
            model=cfg.agent_model,
            system=system,
            messages=messages,
            tools=[t.to_anthropic() for t in tools],
            max_tokens=2048,
            purpose=f"agent:{agent.name}",
        )
        text_parts = [b.text for b in resp.content if getattr(b, "type", "") == "text"]
        if text_parts:
            final_text = "\n".join(text_parts)

        if resp.stop_reason != "tool_use":
            return final_text

        messages.append({"role": "assistant", "content": resp.content})
        results = []
        for block in resp.content:
            if getattr(block, "type", "") != "tool_use":
                continue
            tool = registry.find_tool(agent.name, block.name)
            if tool is None:
                output = f"Unknown tool: {block.name}"
            else:
                try:
                    output = await tool.handler(ctx, dict(block.input or {}))
                except Exception as e:  # tool errors go back to the model, not the user
                    log.exception("tool %s failed", block.name)
                    output = f"Tool error: {e}"
            results.append({"type": "tool_result", "tool_use_id": block.id, "content": str(output)[:8000]})
        messages.append({"role": "user", "content": results})

    return final_text or "I hit my tool-use limit for this turn — try narrowing the request."
