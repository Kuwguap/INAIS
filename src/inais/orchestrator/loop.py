"""The brain. Simple turns → cheap model; tool-heavy turns → the agent tool loop.

Which provider runs the agent loop is BRAIN_PROVIDER's business, not this module's: it calls
llm.agent_tools with provider-neutral messages. Each conversation is pinned to one model while
active (prompt caches are provider+model scoped) — a session escalated to the agent model
stays there until 30 min of idle or /reset.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from inais import llm, persona, trace
from inais.brain import signals
from inais.config import settings
from inais.memory import retrieval, store
from inais.orchestrator import registry, router, swarm
from inais.timeutil import prompt_now

log = logging.getLogger(__name__)

MAX_TOOL_ITERATIONS = 8
SESSION_TTL_SECONDS = 30 * 60


@dataclass
class TurnResult:
    """What a turn produced. `voice_sent` is True when the model already delivered a voice
    note via the speak tool, so the deterministic backstop in the routers doesn't double-send."""
    text: str
    voice_sent: bool = False

# chat_id -> (pinned_to_agent_model, last_activity_ts)
_sessions: dict[int, tuple[bool, float]] = {}

ASSISTANT_RULES = """You have long-term memory: use remember_this when the user shares
something durable, and search_memory when you might already know something relevant.
When a request has independent parts owned by different specialists, use delegate to run
them in parallel rather than doing everything sequentially yourself."""


def _session_pinned(chat_id: int) -> bool:
    pinned, ts = _sessions.get(chat_id, (False, 0.0))
    return pinned and (time.time() - ts) < SESSION_TTL_SECONDS


def _touch_session(chat_id: int, pinned: bool) -> None:
    _sessions[chat_id] = (pinned, time.time())


def reset_session(chat_id: int) -> None:
    _sessions.pop(chat_id, None)


def _build_system(agent: registry.AgentDef, memory_text: str, notes_text: str = "",
                  persona_text: str = "") -> str:
    parts = [persona_text or persona.CHARACTER, ASSISTANT_RULES, prompt_now(), agent.prompt]
    if memory_text:
        parts.append(memory_text)
    if notes_text:
        parts.append(notes_text)
    return "\n\n".join(p for p in parts if p)


async def handle_text(bot, chat_id: int, text: str, source: str = "text",
                      images: list[dict] | None = None) -> TurnResult:
    """Main entry: route, run, persist. Returns the reply text (and whether voice was sent).

    `images` are Anthropic image blocks. They force the agent path: the cheap tier is
    text-only here, and an image is exactly the kind of turn that benefits from tools.
    """
    cfg = settings()
    turn = trace.begin(chat_id, text, source=source)
    if not cfg.brain_enabled:
        trace.finish(error="brain not configured")
        return TurnResult(
            "I'm alive, but my brain isn't wired up yet — set ANTHROPIC_API_KEY and "
            "OPENAI_API_KEY in .env to enable it. (M0 echo mode)\n\nYou said: " + text)

    try:
        # One embedding of the user's text, reused four ways: the LOCAL router, memory
        # retrieval, the stored message, and the interest network's training example.
        # It comes first so routing can happen on-device when the distilled router is good.
        user_vec: list[float] | None = None
        if cfg.db_enabled and cfg.brain_enabled:
            try:
                user_vec = await llm.embed(text)
            except Exception:
                log.exception("embedding the user turn failed — continuing without memory")

        r = await router.route(text, vec=user_vec)
        if _session_pinned(chat_id):
            r = router.Route(r.agent, "complex", "pinned")
        if images:
            r = router.Route(r.agent, "complex", f"{r.source}+image")

        agent = registry.get_agent(r.agent)
        turn.agent, turn.complexity, turn.route_source = agent.name, r.complexity, r.source

        mem = await retrieval.gather(agent.name, text, query_vec=user_vec)
        persona_text = await persona.block()
        note_rows = await swarm.read_notes(agent.name, limit=5)
        notes = swarm.render_notes(note_rows)
        turn.notes_count = len(note_rows)
        turn.memory_counts = {
            "facts": len(mem.facts), "episodes": len(mem.episodes),
            "knowledge": len(mem.knowledge), "rules": len(mem.preferences),
        }
        turn.memory_items = [*(f"fact: {f}" for f in mem.facts),
                             *(f"learned: {k}" for k in mem.knowledge)]

        # fetch history BEFORE saving the current message, or it would appear twice in the prompt
        history = await store.recent_history(chat_id)
        # history is text-only, so mark the attachment or later turns lose the context
        stored_text = f"{text}\n[user sent {len(images)} image(s)]" if images else text
        user_msg_id = await store.save_message(chat_id, "user", stored_text)
        if user_msg_id is not None:
            asyncio.create_task(store.embed_message(user_msg_id, text, vec=user_vec))
        # whatever the user raises unprompted is training signal for the interest network
        asyncio.create_task(signals.user_message_signal(text, embedding=user_vec))
        # and if we spoke first recently, this reply is the engagement label
        asyncio.create_task(signals.mark_proactive_replied())

        voice_sent = False
        if r.complexity == "simple":
            reply = await _simple_turn(agent, mem, history, text, notes, persona_text)
            # Self-healing: a tool-less reply that hands the work back to the user ("run curl
            # yourself") gets re-run through the agent path, which HAS web_search/read_url.
            if router.looks_like_fetch_refusal(reply):
                turn.route_source = f"{r.source}+escalated"
                reply, voice_sent = await _agent_turn(
                    bot, chat_id, agent, mem, history, text, notes, images, persona_text)
                _touch_session(chat_id, pinned=True)
            else:
                _touch_session(chat_id, pinned=False)
        else:
            reply, voice_sent = await _agent_turn(bot, chat_id, agent, mem, history, text,
                                                  notes, images, persona_text)
            _touch_session(chat_id, pinned=True)
    except Exception as e:
        trace.finish(error=f"{type(e).__name__}: {e}")
        raise

    reply = reply.strip() or (
        "I came back with nothing on that one — usually the model spent its whole budget "
        "thinking. Try rephrasing, or lower OPENAI_REASONING_EFFORT. /why shows the calls.")
    asst_msg_id = await store.save_message(chat_id, "assistant", reply)
    if asst_msg_id is not None:
        asyncio.create_task(store.embed_message(asst_msg_id, reply))
    trace.finish(reply=reply)
    return TurnResult(reply, voice_sent)


async def _simple_turn(agent, mem, history: list[dict], text: str, notes: str = "",
                       persona_text: str = "") -> str:
    messages = [{"role": "system",
                 "content": _build_system(agent, mem.render(), notes, persona_text)}]
    messages += history[-8:]
    messages.append({"role": "user", "content": text})
    return await llm.openai_chat(
        model=settings().triage_model, messages=messages, purpose=f"simple:{agent.name}",
    )


async def _run_tool(ctx, agent_name: str, call: dict) -> dict:
    """Execute one tool call. Errors go back to the model, never to the user."""
    tool = registry.find_tool(agent_name, call["name"])
    if tool is None:
        trace.record_tool(call["name"], ok=False, detail="unknown tool")
        return {"id": call["id"], "content": f"Unknown tool: {call['name']}"}
    try:
        output = await tool.handler(ctx, call["input"])
        trace.record_tool(call["name"], ok=True, detail=str(output)[:80])
    except Exception as e:
        log.exception("tool %s failed", call["name"])
        trace.record_tool(call["name"], ok=False, detail=str(e)[:80])
        output = f"Tool error: {e}"
    return {"id": call["id"], "content": str(output)[:8000]}


async def _agent_turn(bot, chat_id: int, agent, mem, history: list[dict], text: str,
                      notes: str = "", images: list[dict] | None = None,
                      persona_text: str = "") -> tuple[str, bool]:
    tools = registry.tools_for(agent.name)
    system = _build_system(agent, mem.render(), notes, persona_text)
    # images ride along in the user turn; text alone stays a plain string so cached
    # prefixes from earlier text-only turns still match
    user_content: str | list[dict] = text
    if images:
        user_content = [*images, {"type": "text", "text": text}]
    messages: list[dict] = [*history, {"role": "user", "content": user_content}]
    ctx = registry.ToolContext(bot=bot, chat_id=chat_id, agent=agent.name)

    final_text = ""
    for _ in range(MAX_TOOL_ITERATIONS):
        reply = await llm.agent_tools(
            system=system,
            messages=messages,
            tools=[t.to_anthropic() for t in tools],
            max_tokens=2048,
            purpose=f"agent:{agent.name}",
        )
        if reply.text:
            final_text = reply.text
        if reply.stop_reason != "tool_use":
            return final_text, ctx.voice_sent

        messages.append({"role": "assistant", "text": reply.text,
                         "tool_calls": reply.tool_calls})
        # the model asked for these together, so run them together — sequential tool calls
        # were adding seconds per round trip for no reason
        results = await asyncio.gather(
            *(_run_tool(ctx, agent.name, call) for call in reply.tool_calls))
        messages.append({"role": "tool_results", "results": list(results)})

    fallback = final_text or "I hit my tool-use limit for this turn — try narrowing the request."
    return fallback, ctx.voice_sent
