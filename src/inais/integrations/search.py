"""Web search for the learning loop. Tavily → Brave → DuckDuckGo (no key), first available.

Results are DATA, never instructions: they are summarised into knowledge notes and can never
change what the assistant does. Anything that looks like a directive inside a page stays text.
"""

from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass

import aiohttp

from inais.config import settings

log = logging.getLogger(__name__)

TIMEOUT = aiohttp.ClientTimeout(total=25)
USER_AGENT = "INAIS-personal-assistant/1.0 (+single-user research bot)"


@dataclass
class SearchHit:
    title: str
    url: str
    snippet: str

    def render(self) -> str:
        return f"- {self.title} ({self.url})\n  {self.snippet[:400]}"


async def search(query: str, max_results: int = 5) -> list[SearchHit]:
    cfg = settings()
    provider = cfg.search_provider
    try:
        if provider == "tavily":
            return await _tavily(query, max_results, cfg.tavily_api_key)
        if provider == "brave":
            return await _brave(query, max_results, cfg.brave_api_key)
        return await _duckduckgo(query, max_results)
    except Exception:
        log.exception("web search failed (provider=%s)", provider)
        return []


async def _tavily(query: str, max_results: int, api_key: str) -> list[SearchHit]:
    payload = {"api_key": api_key, "query": query, "max_results": max_results,
               "search_depth": "basic", "include_answer": False}
    async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
        async with session.post("https://api.tavily.com/search", json=payload) as resp:
            resp.raise_for_status()
            data = await resp.json()
    return [
        SearchHit(r.get("title", ""), r.get("url", ""), r.get("content", ""))
        for r in data.get("results", [])[:max_results]
    ]


async def _brave(query: str, max_results: int, api_key: str) -> list[SearchHit]:
    headers = {"X-Subscription-Token": api_key, "Accept": "application/json",
               "User-Agent": USER_AGENT}
    params = {"q": query, "count": str(max_results)}
    async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
        async with session.get("https://api.search.brave.com/res/v1/web/search",
                               headers=headers, params=params) as resp:
            resp.raise_for_status()
            data = await resp.json()
    return [
        SearchHit(r.get("title", ""), r.get("url", ""),
                  re.sub(r"<[^>]+>", "", r.get("description", "")))
        for r in data.get("web", {}).get("results", [])[:max_results]
    ]


_DDG_ROW = re.compile(
    r'<a rel="nofollow" class="result__a" href="(?P<url>[^"]+)">(?P<title>.*?)</a>'
    r'.*?class="result__snippet"[^>]*>(?P<snippet>.*?)</a>',
    re.DOTALL,
)


async def _duckduckgo(query: str, max_results: int) -> list[SearchHit]:
    """Key-free fallback. HTML scraping, so treat breakage as expected, not exceptional."""
    async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
        async with session.post("https://html.duckduckgo.com/html/",
                                data={"q": query},
                                headers={"User-Agent": USER_AGENT}) as resp:
            resp.raise_for_status()
            body = await resp.text()
    hits: list[SearchHit] = []
    for m in _DDG_ROW.finditer(body):
        hits.append(SearchHit(
            _clean(m.group("title")), html.unescape(m.group("url")), _clean(m.group("snippet"))))
        if len(hits) >= max_results:
            break
    if not hits:
        log.warning("duckduckgo returned no parseable results — consider setting TAVILY_API_KEY")
    return hits


def _clean(fragment: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", fragment)).strip()
