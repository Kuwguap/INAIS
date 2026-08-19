"""Single gateway for every LLM call. Records usage/cost into llm_usage when the DB is up."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from functools import lru_cache

from anthropic import AsyncAnthropic
from openai import AsyncOpenAI

from inais import db, trace
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
    "gpt-5": (1.25, 10.0),        # generic gpt-5*; keep AFTER the mini/nano entries
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1": (2.0, 8.0),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.5, 10.0),
    "gpt-4o-mini-transcribe": (1.25, 5.0),  # audio-input tokens
    "text-embedding-3-small": (0.02, 0.0),
}


# Reasoning models spend hidden tokens from the SAME budget as the reply, so a small
# max_completion_tokens gets consumed before any output exists and the API 400s. Floor it.
REASONING_PREFIXES = ("gpt-5", "o1", "o3", "o4")
REASONING_MIN_TOKENS = 2000
_TOKEN_LIMIT_HINTS = ("max_tokens", "output limit")


def completion_budget(model: str, requested: int) -> int:
    """Token budget to actually send, allowing for invisible reasoning tokens."""
    if model.startswith(REASONING_PREFIXES):
        return max(requested, REASONING_MIN_TOKENS)
    return requested


def _is_token_limit_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(hint in text for hint in _TOKEN_LIMIT_HINTS)


async def _chat_completion(*, model: str, messages: list[dict], purpose: str,
                           max_completion_tokens: int, low_effort: bool = False, **extra):
    """Every OpenAI chat call goes through here: budget floor, retries, usage recorded.

    Two failure modes are handled rather than raised: a reasoning model burning its whole
    budget before producing output, and an older model rejecting reasoning_effort.
    """
    kwargs: dict = {
        "model": model,
        "messages": messages,
        "max_completion_tokens": completion_budget(model, max_completion_tokens),
        **extra,
    }
    # Reasoning time dominates latency: an unconstrained GPT-5 turn takes tens of seconds,
    # and a tool loop multiplies that by the number of round trips. Classification never
    # benefits from deliberation; the agent gets whatever the user configured.
    if model.startswith(REASONING_PREFIXES):
        kwargs["reasoning_effort"] = (
            "minimal" if low_effort else settings().openai_reasoning_effort)

    bumped = False
    resp = None
    for _ in range(3):
        try:
            resp = await openai_client().chat.completions.create(**kwargs)
            break
        except Exception as e:
            message = str(e).lower()
            if "reasoning_effort" in kwargs and (
                    "reasoning_effort" in message or "invalid value" in message):
                kwargs.pop("reasoning_effort")   # model predates or rejects the parameter
                continue
            if _is_token_limit_error(e) and not bumped:
                bumped = True
                kwargs["max_completion_tokens"] = max(
                    kwargs["max_completion_tokens"] * 3, REASONING_MIN_TOKENS * 2)
                log.warning("%s hit its output limit on %s — retrying with %s tokens",
                            model, purpose, kwargs["max_completion_tokens"])
                continue
            raise
    if resp is None:
        raise RuntimeError(f"{model} could not complete {purpose}")
    if resp.usage:
        await record_usage("openai", model, purpose,
                           resp.usage.prompt_tokens, resp.usage.completion_tokens)
    return resp


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
    if cost_usd is None:
        cost_usd = estimate_cost(model, input_tokens, output_tokens)
    trace.record_llm(provider, model, purpose, input_tokens, output_tokens, cost_usd)
    p = db.pool()
    if p is None:
        return
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
    input_tokens = (
        (resp.usage.input_tokens or 0)
        + (getattr(resp.usage, "cache_creation_input_tokens", 0) or 0)
        + (getattr(resp.usage, "cache_read_input_tokens", 0) or 0)
    )
    await record_usage("anthropic", model, purpose, input_tokens, resp.usage.output_tokens)
    return resp


async def openai_json(
    *, model: str, system: str, user: str, purpose: str, max_completion_tokens: int = 600,
) -> dict:
    """Cheap-model structured classification. Returns parsed JSON (empty dict on failure)."""
    resp = await _chat_completion(
        model=model,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        purpose=purpose,
        max_completion_tokens=max_completion_tokens,
        low_effort=True,          # every openai_json caller is a classifier
        response_format={"type": "json_object"},
    )
    try:
        return json.loads(resp.choices[0].message.content or "{}")
    except json.JSONDecodeError:
        log.warning("openai_json returned non-JSON for purpose=%s", purpose)
        return {}


async def openai_chat(
    *, model: str, messages: list[dict], purpose: str, max_completion_tokens: int = 1500,
) -> str:
    """History-aware chat completion on the cheap tier."""
    resp = await _chat_completion(
        model=model, messages=messages, purpose=purpose,
        max_completion_tokens=max_completion_tokens)
    return resp.choices[0].message.content or ""


async def openai_text(
    *, model: str, system: str, user: str, purpose: str, max_completion_tokens: int = 1500,
) -> str:
    resp = await _chat_completion(
        model=model,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        purpose=purpose, max_completion_tokens=max_completion_tokens)
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


# ---------------------------------------------------------------------------
# Provider-neutral agent API.
#
# The orchestrator and sub-agents speak one message format and llm.py translates it, so
# BRAIN_PROVIDER can move the whole brain between Anthropic and OpenAI without any caller
# knowing. Neutral messages look like:
#     {"role": "user", "content": str | [blocks]}
#     {"role": "assistant", "text": str, "tool_calls": [{"id", "name", "input"}]}
#     {"role": "tool_results", "results": [{"id", "content"}]}
# Image blocks use the Anthropic shape and are converted for OpenAI.
# ---------------------------------------------------------------------------

@dataclass
class AgentReply:
    text: str
    tool_calls: list[dict]
    stop_reason: str          # "tool_use" when the model wants a tool, else "end"


def _img_to_openai(block: dict) -> dict:
    src = block.get("source", {})
    url = "data:" + str(src.get("media_type", "image/jpeg")) + ";base64," + str(src.get("data", ""))
    return {"type": "image_url", "image_url": {"url": url}}


def to_anthropic_messages(messages: list[dict]) -> list[dict]:
    out: list[dict] = []
    for m in messages:
        role = m.get("role")
        if role == "user":
            out.append({"role": "user", "content": m.get("content") or ""})
        elif role == "assistant":
            blocks: list[dict] = []
            if m.get("text"):
                blocks.append({"type": "text", "text": m["text"]})
            for call in m.get("tool_calls", []):
                blocks.append({"type": "tool_use", "id": call["id"],
                               "name": call["name"], "input": call.get("input", {})})
            out.append({"role": "assistant", "content": blocks or m.get("content") or ""})
        elif role == "tool_results":
            out.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": r["id"], "content": str(r["content"])}
                for r in m["results"]
            ]})
    return out


def to_openai_messages(messages: list[dict], system: str) -> list[dict]:
    out: list[dict] = [{"role": "system", "content": system}]
    for m in messages:
        role = m.get("role")
        if role == "user":
            content = m.get("content") or ""
            if isinstance(content, list):
                converted = [
                    _img_to_openai(b) if b.get("type") == "image" else b for b in content
                ]
                out.append({"role": "user", "content": converted})
            else:
                out.append({"role": "user", "content": content})
        elif role == "assistant":
            entry: dict = {"role": "assistant"}
            text = m.get("text") or m.get("content") or ""
            calls = m.get("tool_calls") or []
            if calls:
                entry["tool_calls"] = [
                    {"id": c["id"], "type": "function",
                     "function": {"name": c["name"],
                                  "arguments": json.dumps(c.get("input", {}))}}
                    for c in calls
                ]
                # A tool-calling turn usually has no prose. Omit content entirely rather
                # than sending null — the API rejects a null content field outright.
                if text:
                    entry["content"] = text
            else:
                entry["content"] = text
            out.append(entry)
        elif role == "tool_results":
            for r in m["results"]:
                out.append({"role": "tool", "tool_call_id": r["id"],
                            "content": str(r["content"])})
    return out


def tools_to_openai(tools: list[dict]) -> list[dict]:
    return [
        {"type": "function",
         "function": {"name": t["name"], "description": t.get("description", ""),
                      "parameters": t.get("input_schema", {"type": "object", "properties": {}})}}
        for t in tools
    ]


async def agent_tools(*, system: str, messages: list[dict], tools: list[dict],
                      max_tokens: int = 2048, purpose: str = "agent",
                      model: str | None = None) -> AgentReply:
    """One turn of a tool-using agent loop, on whichever provider is configured."""
    cfg = settings()
    provider = cfg.agent_provider
    chosen = model or cfg.resolved_agent_model

    if provider == "anthropic":
        resp = await anthropic_message(
            model=chosen,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=to_anthropic_messages(messages),
            tools=tools, max_tokens=max_tokens, purpose=purpose,
        )
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        calls = [{"id": b.id, "name": b.name, "input": dict(b.input or {})}
                 for b in resp.content if getattr(b, "type", "") == "tool_use"]
        return AgentReply(text=text, tool_calls=calls,
                          stop_reason="tool_use" if calls else "end")

    extra: dict = {"tools": tools_to_openai(tools)} if tools else {}
    resp = await _chat_completion(
        model=chosen, messages=to_openai_messages(messages, system),
        purpose=purpose, max_completion_tokens=max_tokens, **extra)
    choice = resp.choices[0].message
    calls = []
    for call in (choice.tool_calls or []):
        try:
            arguments = json.loads(call.function.arguments or "{}")
        except json.JSONDecodeError:
            arguments = {}
        calls.append({"id": call.id, "name": call.function.name, "input": arguments})
    return AgentReply(text=choice.content or "", tool_calls=calls,
                      stop_reason="tool_use" if calls else "end")


async def agent_text(*, system: str, user: str, max_tokens: int = 1500,
                     purpose: str = "agent", model: str | None = None,
                     cheap: bool = False) -> str:
    """A single system+user call returning text, on whichever provider is configured.

    `cheap=True` asks for the small tier — used by background passes (reflection, research
    synthesis) where the strong model is not worth its price.
    """
    cfg = settings()
    if model is None:
        model = cfg.resolved_subagent_model if cheap else cfg.resolved_agent_model
    if cfg.agent_provider == "anthropic":
        resp = await anthropic_message(
            model=model, system=system,
            messages=[{"role": "user", "content": user}],
            max_tokens=max_tokens, purpose=purpose,
        )
        return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
    return (await openai_chat(
        model=model,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        purpose=purpose, max_completion_tokens=max_tokens,
    )).strip()
