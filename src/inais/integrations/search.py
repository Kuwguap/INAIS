"""Web search for the learning loop.

Providers cascade in the order settings().search_providers builds — Serper (Google results,
the big free pool) → Tavily → Brave → Google CSE (100/day backup) → DuckDuckGo (keyless
last resort). Each is tried until one returns results, so a spent quota degrades instead of
blinding the researcher.

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
    """Try each configured provider in order until one produces results."""
    _, hits = await search_traced(query, max_results)
    return hits


async def search_traced(query: str, max_results: int = 5) -> tuple[str, list[SearchHit]]:
    """Like search(), but also returns which provider produced the results."""
    cfg = settings()
    for provider in cfg.search_providers:
        try:
            hits = await _PROVIDERS[provider](query, max_results, cfg)
        except Exception:
            log.exception("search provider %s failed — trying the next", provider)
            continue
        if hits:
            return provider, hits
        log.info("search provider %s returned nothing for %r", provider, query[:60])
    return "none", []


async def probe() -> str:
    """Test every provider and report the raw outcome — for /diag.

    search() hides which backend answered and swallows errors to fall through, so a wrong
    Serper key looks identical to 'no results'. This says exactly what each one did.
    """
    import aiohttp as _aiohttp

    cfg = settings()
    q = "test query"
    lines = [f"🔎 Search providers (order: {' → '.join(cfg.search_providers)})"]
    checks = [
        ("serper", bool(cfg.serper_api_key)),
        ("tavily", bool(cfg.tavily_api_key)),
        ("brave", bool(cfg.brave_api_key)),
        ("google_cse", bool(cfg.google_cse_api_key and cfg.google_cse_id)),
        ("duckduckgo", True),
    ]
    for name, configured in checks:
        if not configured:
            lines.append(f"⏸ {name} — no key")
            continue
        try:
            hits = await _PROVIDERS[name](q, 3, cfg)
            mark = "✅" if hits else "⚠️"
            note = "" if hits else " (reachable but returned nothing — often blocked on"
            note += " datacenter IPs)" if not hits and name == "duckduckgo" else ""
            lines.append(f"{mark} {name} — {len(hits)} result(s){note}")
        except _aiohttp.ClientResponseError as e:
            hint = " (key invalid or out of quota)" if e.status in (401, 403, 429) else ""
            lines.append(f"❌ {name} — HTTP {e.status}{hint}")
        except Exception as e:
            lines.append(f"❌ {name} — {type(e).__name__}: {str(e)[:120]}")
    return "\n".join(lines)


def parse_serper(data: dict) -> list[SearchHit]:
    """serper.dev: organic results carry title/link/snippet."""
    return [
        SearchHit(str(r.get("title", "")), str(r.get("link", "")),
                  str(r.get("snippet", "")))
        for r in (data.get("organic") or [])
        if r.get("link")
    ]


async def _serper(query: str, max_results: int, cfg) -> list[SearchHit]:
    async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
        async with session.post(
            "https://google.serper.dev/search",
            headers={"X-API-KEY": cfg.serper_api_key, "Content-Type": "application/json"},
            json={"q": query, "num": max_results},
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
    return parse_serper(data)[:max_results]


def parse_google_cse(data: dict) -> list[SearchHit]:
    return [
        SearchHit(str(r.get("title", "")), str(r.get("link", "")),
                  str(r.get("snippet", "")))
        for r in (data.get("items") or [])
        if r.get("link")
    ]


async def _google_cse(query: str, max_results: int, cfg) -> list[SearchHit]:
    params = {"key": cfg.google_cse_api_key, "cx": cfg.google_cse_id,
              "q": query, "num": str(min(max_results, 10))}
    async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
        async with session.get("https://www.googleapis.com/customsearch/v1",
                               params=params) as resp:
            resp.raise_for_status()
            data = await resp.json()
    return parse_google_cse(data)[:max_results]


async def _tavily(query: str, max_results: int, cfg) -> list[SearchHit]:
    payload = {"api_key": cfg.tavily_api_key, "query": query, "max_results": max_results,
               "search_depth": "basic", "include_answer": False}
    async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
        async with session.post("https://api.tavily.com/search", json=payload) as resp:
            resp.raise_for_status()
            data = await resp.json()
    return [
        SearchHit(r.get("title", ""), r.get("url", ""), r.get("content", ""))
        for r in data.get("results", [])[:max_results]
    ]


async def _brave(query: str, max_results: int, cfg) -> list[SearchHit]:
    headers = {"X-Subscription-Token": cfg.brave_api_key, "Accept": "application/json",
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


async def _duckduckgo(query: str, max_results: int, cfg=None) -> list[SearchHit]:
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


_PROVIDERS = {
    "serper": _serper,
    "tavily": _tavily,
    "brave": _brave,
    "google_cse": _google_cse,
    "duckduckgo": _duckduckgo,
}
