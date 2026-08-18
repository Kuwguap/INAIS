"""Sub-agent swarm: specialists running concurrently, coordinating through a blackboard.

Why this exists:
- Speed. A request spanning email + finance + study used to be sequential tool calls in one
  loop. Now the orchestrator fans out and the wall-clock is the slowest specialist, not the sum.
- Autonomy. The learning loop (brain/autonomy.py) uses the same primitive to run researchers
  in parallel with no user in the loop, then a curator that reads what they wrote.

Sub-agents are deliberately weaker than the orchestrator: cheap model (SUBAGENT_MODEL),
few iterations, no delegate tool (no recursion), and they return text, not control flow.
Agent-to-agent messaging is asynchronous by design: notes land in `agent_notes` and are read
by whoever runs next (the curator wave, a later cycle, or another specialist on request).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from inais import db, llm, trace
from inais.config import settings
from inais.orchestrator import registry
from inais.timeutil import prompt_now

log = logging.getLogger(__name__)

SUBAGENT_MAX_ITERATIONS = 5
SUBAGENT_MAX_TOKENS = 1500

SUBAGENT_FRAME = """You are a sub-agent of INAIS working autonomously on ONE task, with no
user watching. Rules:
- Use your tools to get real data. Never invent facts, numbers, emails or sources.
- You cannot ask the user anything. If information is missing, say what is missing.
- Finish with a compact report (<= 200 words): findings, then anything the orchestrator must
  know. No greetings, no filler.
- If your findings matter to another specialist, call post_note before finishing."""


@dataclass
class SubTask:
    agent: str
    task: str
    label: str = ""
    extra_context: str = ""


@dataclass
class SubResult:
    agent: str
    task: str
    output: str
    ok: bool = True
    label: str = ""

    def render(self) -> str:
        head = self.label or self.agent
        return f"[{head}] {self.output.strip()}"


@dataclass
class SwarmRun:
    results: list[SubResult] = field(default_factory=list)

    def render(self) -> str:
        return "\n\n".join(r.render() for r in self.results) or "(no sub-agent output)"


def _semaphore() -> asyncio.Semaphore:
    return asyncio.Semaphore(max(1, settings().max_parallel_subagents))


async def run_subagent(bot, chat_id: int, spec: SubTask, model: str | None = None) -> SubResult:
    """One bounded tool loop for a single specialist."""
    cfg = settings()
    agent = registry.get_agent(spec.agent)
    tools = registry.tools_for(agent.name, for_subagent=True)
    system = "\n\n".join(filter(None, [
        SUBAGENT_FRAME, prompt_now(), agent.prompt, spec.extra_context,
    ]))
    messages: list[dict] = [{"role": "user", "content": spec.task}]
    ctx = registry.ToolContext(bot=bot, chat_id=chat_id, agent=agent.name)

    final = ""
    for _ in range(SUBAGENT_MAX_ITERATIONS):
        try:
            resp = await llm.anthropic_message(
                model=model or cfg.subagent_model,
                system=system,
                messages=messages,
                tools=[t.to_anthropic() for t in tools],
                max_tokens=SUBAGENT_MAX_TOKENS,
                purpose=f"subagent:{agent.name}",
            )
        except Exception as e:
            log.exception("sub-agent %s failed", spec.agent)
            return SubResult(spec.agent, spec.task, f"failed: {e}", ok=False, label=spec.label)

        text_parts = [b.text for b in resp.content if getattr(b, "type", "") == "text"]
        if text_parts:
            final = "\n".join(text_parts)
        if resp.stop_reason != "tool_use":
            return SubResult(spec.agent, spec.task, final, label=spec.label)

        messages.append({"role": "assistant", "content": resp.content})
        results = []
        for block in resp.content:
            if getattr(block, "type", "") != "tool_use":
                continue
            tool = registry.find_tool(agent.name, block.name, for_subagent=True)
            if tool is None:
                output = f"Unknown or restricted tool: {block.name}"
                trace.record_tool(f"{agent.name}/{block.name}", ok=False, detail="restricted")
            else:
                try:
                    output = await tool.handler(ctx, dict(block.input or {}))
                    trace.record_tool(f"{agent.name}/{block.name}", ok=True)
                except Exception as e:
                    log.exception("sub-agent tool %s failed", block.name)
                    output = f"Tool error: {e}"
                    trace.record_tool(f"{agent.name}/{block.name}", ok=False, detail=str(e)[:80])
            results.append({"type": "tool_result", "tool_use_id": block.id,
                            "content": str(output)[:6000]})
        messages.append({"role": "user", "content": results})

    return SubResult(spec.agent, spec.task,
                     final or "hit the sub-agent iteration limit", ok=bool(final),
                     label=spec.label)


async def run_parallel(bot, chat_id: int, specs: list[SubTask],
                       model: str | None = None) -> SwarmRun:
    """Run specialists concurrently under the configured cap."""
    if not specs:
        return SwarmRun([])
    sem = _semaphore()

    async def guarded(spec: SubTask) -> SubResult:
        async with sem:
            return await run_subagent(bot, chat_id, spec, model=model)

    log.info("swarm: launching %s sub-agents (%s)", len(specs), ", ".join(s.agent for s in specs))
    gathered = await asyncio.gather(*(guarded(s) for s in specs), return_exceptions=True)
    results: list[SubResult] = []
    for spec, res in zip(specs, gathered, strict=True):
        if isinstance(res, BaseException):
            log.exception("sub-agent %s crashed", spec.agent, exc_info=res)
            results.append(SubResult(spec.agent, spec.task, f"crashed: {res}", ok=False,
                                     label=spec.label))
        else:
            results.append(res)
    return SwarmRun(results)


# ---------- blackboard ----------

async def post_note(from_agent: str, topic: str, content: str, to_agent: str = "all") -> int | None:
    p = db.pool()
    if p is None or not content.strip():
        return None
    row = await p.fetchrow(
        "insert into agent_notes (from_agent, to_agent, topic, content)"
        " values ($1, $2, $3, $4) returning id",
        from_agent, to_agent or "all", topic[:200] or "note", content,
    )
    return row["id"]


async def read_notes(for_agent: str, limit: int = 10, consume: bool = False,
                     topic: str | None = None) -> list[dict]:
    p = db.pool()
    if p is None:
        return []
    params: list = [for_agent, limit]
    topic_clause = ""
    if topic:
        params.insert(1, f"%{topic}%")
        topic_clause = " and topic ilike $2"
    rows = await p.fetch(
        f"select id, from_agent, topic, content, ts from agent_notes"
        f" where to_agent in ($1, 'all') and not consumed{topic_clause}"
        f" order by id desc limit ${len(params)}",
        *params,
    )
    if consume and rows:
        await p.execute(
            "update agent_notes set consumed = true where id = any($1::bigint[])",
            [r["id"] for r in rows],
        )
    return [dict(r) for r in rows]


def render_notes(notes: list[dict]) -> str:
    if not notes:
        return ""
    return "## Notes from your other agents\n" + "\n".join(
        f"- ({n['from_agent']} on {n['topic']}) {n['content'][:400]}" for n in notes
    )
