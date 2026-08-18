"""Routing: free rules first, then a cheap-model classifier. Escalation picks the model."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from inais import llm
from inais.config import settings

log = logging.getLogger(__name__)

AGENTS = ("finance", "email", "study")

_FINANCE_HINTS = ("binance", "portfolio", "crypto", "btc", "eth", "usdt", "trade", "deposit",
                  "withdraw", "balance", "pnl", "invest")
_EMAIL_HINTS = ("email", "mail", "inbox", "gmail", "reply to", "draft", "unread")


@dataclass
class Route:
    agent: str
    complexity: str  # "simple" | "complex"


def rule_route(text: str) -> Route | None:
    lower = text.lower()
    if any(h in lower for h in _FINANCE_HINTS):
        return Route("finance", "complex")  # finance always needs tools
    if any(h in lower for h in _EMAIL_HINTS):
        return Route("email", "complex")
    if len(text) <= 60 and text.rstrip("!.? ").lower() in (
        "hi", "hello", "hey", "thanks", "thank you", "ok", "okay", "good morning",
        "good night", "yo", "sup",
    ):
        return Route("study", "simple")
    return None


async def route(text: str) -> Route:
    r = rule_route(text)
    if r is not None:
        return r
    if not settings().openai_api_key:
        return Route("study", "complex")
    data = await llm.openai_json(
        model=settings().triage_model,
        system=(
            "Classify the user's message for a personal assistant. Return JSON "
            '{"agent": "finance|email|study", "complexity": "simple|complex"}.\n'
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
    return Route(agent, complexity)
