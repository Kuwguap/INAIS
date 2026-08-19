"""Routing: free rules first, then a cheap-model classifier. Escalation picks the model."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from inais import llm
from inais.config import settings

log = logging.getLogger(__name__)

AGENTS = ("finance", "email", "study", "planner")

_FINANCE_HINTS = ("binance", "portfolio", "crypto", "btc", "eth", "usdt", "trade", "deposit",
                  "withdraw", "balance", "pnl", "invest")
_EMAIL_HINTS = ("email", "mail", "inbox", "gmail", "reply to", "draft", "unread")
_PLANNER_HINTS = ("task", "todo", "to-do", "remind", "reminder", "deadline", "schedule",
                  "calendar", "agenda", "plan my day", "plan my week", "due ")
_STUDY_HINTS = ("exam", "quiz", "revise", "revision", "chapter", "lecture", "notes",
                "study", "read to me", "flashcard", "syllabus")


@dataclass
class Route:
    agent: str
    complexity: str  # "simple" | "complex"
    source: str = "classifier"  # rule | classifier | pinned — surfaced by /why


def rule_route(text: str) -> Route | None:
    lower = text.lower()
    if any(h in lower for h in _FINANCE_HINTS):
        return Route("finance", "complex", "rule")  # finance always needs tools
    if any(h in lower for h in _EMAIL_HINTS):
        return Route("email", "complex", "rule")
    if any(h in lower for h in _PLANNER_HINTS):
        return Route("planner", "complex", "rule")
    if any(h in lower for h in _STUDY_HINTS):
        return Route("study", "complex", "rule")
    if len(text) <= 60 and text.rstrip("!.? ").lower() in (
        "hi", "hello", "hey", "thanks", "thank you", "ok", "okay", "good morning",
        "good night", "yo", "sup",
    ):
        return Route("study", "simple", "rule")
    return None


async def route(text: str, vec: list[float] | None = None) -> Route:
    r = rule_route(text)
    if r is not None:
        return r

    # Local first: once the distilled router reproduces the LLM's decisions reliably AND
    # the complexity head beats chance, this message never leaves the machine to be
    # understood. Either head unsure → fall through to the LLM (and keep harvesting).
    if vec is not None:
        from inais.brain import nn

        local = await nn.classify_route(vec)
        if local is not None:
            agent, confidence = local
            hardness = await nn.score("complexity", text, embedding=vec)
            if hardness is not None:
                complexity = "complex" if hardness >= 0.5 else "simple"
                return Route(agent, complexity, f"local-nn({confidence:.2f})")

    if not settings().openai_api_key:
        return Route("study", "complex", "no-classifier")
    data = await llm.openai_json(
        model=settings().triage_model,
        system=(
            "Classify the user's message for a personal assistant. Return JSON "
            '{"agent": "finance|email|study|planner", "complexity": "simple|complex"}.\n'
            "- finance: crypto portfolio, Binance, money, budgeting\n"
            "- email: reading/searching/replying to the user's Gmail\n"
            "- study: everything else (questions, coding, studying, chat)\n"
            '- complexity "simple": answerable directly in one short reply without tools or '
            'multi-step reasoning; "complex": needs tools, code, long reasoning, or memory lookups.'
        ),
        user=text[:2000],
        purpose="routing",
        max_completion_tokens=60,
    )
    agent = data.get("agent") if data.get("agent") in AGENTS else "study"
    complexity = data.get("complexity") if data.get("complexity") in ("simple", "complex") else "complex"

    # distillation harvest: the LLM's decision becomes a training example, so the local
    # router and complexity head learn to make this exact call without the API
    if vec is not None:
        import asyncio

        from inais.brain import nn

        asyncio.create_task(nn.add_router_example(text, agent, AGENTS, embedding=vec))
        asyncio.create_task(nn.add_example(
            "complexity", text, 1.0 if complexity == "complex" else 0.0,
            note="distilled from LLM router", embedding=vec))
    return Route(agent, complexity, "classifier")
