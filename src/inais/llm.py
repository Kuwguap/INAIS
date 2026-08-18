"""Single gateway for every LLM call. Records usage/cost into llm_usage when the DB is up."""

from __future__ import annotations

import json
import logging
import re
from functools import lru_cache

from anthropic import AsyncAnthropic
from openai import AsyncOpenAI

from inais import db
from inais.config import settings

log = logging.getLogger(__name__)

# USD per million tokens (input, output). Prefix-matched so dated ids like
# claude-haiku-4-5-20251001 resolve. Unknown models record cost 0 with a warning.
PRICES_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-sonnet-5": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-opus-5": (5.0, 25.0),
    "gpt-5-mini": (0.25, 2.0),
    "gpt-5-nano": (0.20, 1.25),
    "gpt-4o-mini-transcribe": (1.25, 5.0),  # audio-input tokens
    "text-embedding-3-small": (0.02, 0.0),
}


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    for prefix, (pin, pout) in PRICES_PER_MTOK.items():
        if model.startswith(prefix):
            return (input_tokens * pin + output_tokens * pout) / 1_000_000
    log.warning("no price entry for model %s — recording cost 0", model)
    return 0.0


@lru_cache
def anthropic_client() -> AsyncAnthropic:
    return AsyncAnthropic(api_key=settings().anthropic_api_key)


@lru_cache
def openai_client() -> AsyncOpenAI:
    return AsyncOpenAI(api_key=settings().openai_api_key)


async def record_usage(
    provider: str, model: str, purpose: str, input_tokens: int, output_tokens: int,
    cost_usd: float | None = None,
) -> None:
    p = db.pool()
    if p is None:
        return
    if cost_usd is None:
        cost_usd = estimate_cost(model, input_tokens, output_tokens)
    try:
        await p.execute(
            "insert into llm_usage (provider, model, purpose, input_tokens, output_tokens, cost_usd)"
            " values ($1, $2, $3, $4, $5, $6)",
            provider, model, purpose, input_tokens, output_tokens, cost_usd,
        )
    except Exception:  # usage tracking must never break the main flow
        log.exception("failed to record llm usage")


async def anthropic_message(
    *, model: str, system: list[dict] | str, messages: list[dict], tools: list[dict] | None = None,
    max_tokens: int = 2048, purpose: str = "agent",
):
    """Raw Anthropic call + usage recording. Returns the SDK response object."""
    kwargs: dict = {"model": model, "max_tokens": max_tokens, "messages": messages, "system": system}
    if tools:
        kwargs["tools"] = tools
    resp = await anthropic_client().messages.create(**kwargs)
    await record_usage(
        "anthropic", model, purpose,
        resp.usage.input_tokens + getattr(resp.usage, "cache_creation_input_tokens", 0) or resp.usage.input_tokens,
        resp.usage.output_tokens,
    )
    return resp


async def openai_json(
    *, model: str, system: str, user: str, purpose: str, max_completion_tokens: int = 600,
) -> dict:
    """Cheap-model structured classification. Returns parsed JSON (empty dict on failure)."""
    resp = await openai_client().chat.completions.create(
        model=model,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        response_format={"type": "json_object"},
        max_completion_tokens=max_completion_tokens,
    )
    if resp.usage:
        await record_usage("openai", model, purpose, resp.usage.prompt_tokens, resp.usage.completion_tokens)
    try:
        return json.loads(resp.choices[0].message.content or "{}")
    except json.JSONDecodeError:
        log.warning("openai_json returned non-JSON for purpose=%s", purpose)
        return {}


async def openai_chat(
    *, model: str, messages: list[dict], purpose: str, max_completion_tokens: int = 1500,
) -> str:
    """History-aware chat completion on the cheap tier."""
    resp = await openai_client().chat.completions.create(
        model=model, messages=messages, max_completion_tokens=max_completion_tokens,
    )
    if resp.usage:
        await record_usage("openai", model, purpose, resp.usage.prompt_tokens, resp.usage.completion_tokens)
    return resp.choices[0].message.content or ""


async def openai_text(
    *, model: str, system: str, user: str, purpose: str, max_completion_tokens: int = 1500,
) -> str:
    resp = await openai_client().chat.completions.create(
        model=model,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        max_completion_tokens=max_completion_tokens,
    )
    if resp.usage:
        await record_usage("openai", model, purpose, resp.usage.prompt_tokens, resp.usage.completion_tokens)
    return resp.choices[0].message.content or ""


async def embed(text: str) -> list[float]:
    cfg = settings()
    text = text[:8000]  # embedding model context guard
    resp = await openai_client().embeddings.create(model=cfg.embedding_model, input=text)
    if resp.usage:
        await record_usage("openai", cfg.embedding_model, "embedding", resp.usage.prompt_tokens, 0)
    return resp.data[0].embedding


def vec_literal(v: list[float]) -> str:
    """Encode an embedding as a pgvector text literal (cast with ::vector in SQL)."""
    return "[" + ",".join(f"{x:.7g}" for x in v) + "]"


def parse_json_block(text: str) -> dict:
    """Parse JSON from an LLM reply that may wrap it in ```json fences or prose."""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    else:
        brace = re.search(r"\{.*\}", text, re.DOTALL)
        if brace:
            text = brace.group(0)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        log.warning("could not parse JSON from LLM reply: %.200s", text)
        return {}
