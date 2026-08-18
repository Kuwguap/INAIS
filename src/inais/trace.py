"""Per-turn tracing — what /why reads.

An assistant that routes, retrieves and calls tools on your behalf is opaque by default:
when it answers oddly you cannot tell whether the router sent it to the wrong specialist,
memory fed it something stale, or a tool failed silently. This keeps the last ~20 turns in
memory (never written to the database — it is debug data, not history) with the route, the
memories retrieved, the tools called, and the exact token/cost breakdown.

Kept in a ContextVar so anything running inside a turn — including background tasks spawned
from it and concurrent sub-agents — attributes its LLM spend to the right turn without
threading a trace object through every call.
"""

from __future__ import annotations

import time
from collections import deque
from contextvars import ContextVar
from dataclasses import dataclass, field

RING_SIZE = 20
MAX_MEMORY_ITEMS = 6

_turns: deque[Turn] = deque(maxlen=RING_SIZE)
_current: ContextVar[Turn | None] = ContextVar("inais_current_turn", default=None)


@dataclass
class LlmCall:
    provider: str
    model: str
    purpose: str
    input_tokens: int
    output_tokens: int
    cost_usd: float


@dataclass
class ToolCall:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class Turn:
    chat_id: int
    user_text: str
    started_at: float = field(default_factory=time.time)
    source: str = "text"               # text | voice
    agent: str = ""
    complexity: str = ""
    route_source: str = ""             # rule | classifier | pinned
    memory_counts: dict[str, int] = field(default_factory=dict)
    memory_items: list[str] = field(default_factory=list)
    notes_count: int = 0
    tools: list[ToolCall] = field(default_factory=list)
    llm_calls: list[LlmCall] = field(default_factory=list)
    reply_preview: str = ""
    duration_ms: int = 0
    error: str = ""

    @property
    def cost_usd(self) -> float:
        return sum(c.cost_usd for c in self.llm_calls)

    @property
    def input_tokens(self) -> int:
        return sum(c.input_tokens for c in self.llm_calls)

    @property
    def output_tokens(self) -> int:
        return sum(c.output_tokens for c in self.llm_calls)

    def render(self, index: int = 1) -> str:
        from inais.timeutil import fmt_epoch

        lines = [f"🔍 Turn -{index} · {fmt_epoch(self.started_at)}"]
        lines.append(f"You ({self.source}): {self.user_text[:200]}")

        route = f"→ agent: {self.agent or '?'} · {self.complexity or '?'}"
        if self.route_source:
            route += f" (routed by {self.route_source})"
        lines.append(route)

        if self.memory_counts:
            counts = ", ".join(f"{k} {v}" for k, v in self.memory_counts.items() if v)
            lines.append(f"→ memory: {counts or 'nothing retrieved'}")
            for item in self.memory_items[:MAX_MEMORY_ITEMS]:
                lines.append(f"   • {item[:140]}")
        if self.notes_count:
            lines.append(f"→ blackboard notes read: {self.notes_count}")

        if self.tools:
            lines.append("→ tools:")
            for t in self.tools:
                mark = "ok" if t.ok else "FAILED"
                detail = f" — {t.detail[:80]}" if t.detail else ""
                lines.append(f"   • {t.name} [{mark}]{detail}")
        else:
            lines.append("→ tools: none")

        if self.llm_calls:
            lines.append("→ model calls:")
            for c in self.llm_calls:
                lines.append(
                    f"   • {c.model} ({c.purpose}): {c.input_tokens} in / "
                    f"{c.output_tokens} out · ${c.cost_usd:.4f}")
        lines.append(
            f"→ total: {self.input_tokens} in / {self.output_tokens} out · "
            f"${self.cost_usd:.4f} · {self.duration_ms} ms")
        if self.error:
            lines.append(f"⚠️ error: {self.error[:200]}")
        elif self.reply_preview:
            lines.append(f"→ reply: {self.reply_preview[:200]}")
        return "\n".join(lines)


def begin(chat_id: int, user_text: str, source: str = "text") -> Turn:
    """Start a turn. It enters the ring immediately so a crashed turn is still inspectable."""
    turn = Turn(chat_id=chat_id, user_text=user_text, source=source)
    _turns.append(turn)
    _current.set(turn)
    return turn


def current() -> Turn | None:
    return _current.get()


def record_llm(provider: str, model: str, purpose: str, input_tokens: int,
               output_tokens: int, cost_usd: float) -> None:
    turn = _current.get()
    if turn is not None:
        turn.llm_calls.append(
            LlmCall(provider, model, purpose, input_tokens, output_tokens, cost_usd))


def record_tool(name: str, ok: bool = True, detail: str = "") -> None:
    turn = _current.get()
    if turn is not None:
        turn.tools.append(ToolCall(name, ok, detail))


def finish(reply: str = "", error: str = "") -> None:
    turn = _current.get()
    if turn is None:
        return
    turn.duration_ms = int((time.time() - turn.started_at) * 1000)
    turn.reply_preview = reply
    turn.error = error


def recent(n: int = 1) -> list[Turn]:
    """Most recent first."""
    return list(_turns)[-n:][::-1]


def count() -> int:
    return len(_turns)


def clear() -> None:
    _turns.clear()
