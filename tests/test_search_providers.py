"""The search provider chain and the pure response parsers."""

from __future__ import annotations

from inais.config import Settings
from inais.integrations.search import _PROVIDERS, parse_google_cse, parse_serper


def _settings(**over):
    base = {"telegram_bot_token": "t", "owner_telegram_id": 1}
    base.update(over)
    return Settings(**base)


# ---------- provider chain ----------

def test_serper_leads_the_chain_when_configured():
    chain = _settings(serper_api_key="k", tavily_api_key="k", brave_api_key="k",
                      google_cse_api_key="k", google_cse_id="cx").search_providers
    assert chain[0] == "serper"
    assert chain[-1] == "duckduckgo"


def test_duckduckgo_is_always_the_last_resort():
    assert _settings().search_providers == ["duckduckgo"]


def test_google_cse_needs_both_key_and_cx():
    """Half-configured CSE would 400 on every call — leave it out of the chain instead."""
    assert "google_cse" not in _settings(google_cse_api_key="k").search_providers
    assert "google_cse" not in _settings(google_cse_id="cx").search_providers
    assert "google_cse" in _settings(google_cse_api_key="k",
                                     google_cse_id="cx").search_providers


def test_every_chain_entry_has_an_implementation():
    chain = _settings(serper_api_key="k", tavily_api_key="k", brave_api_key="k",
                      google_cse_api_key="k", google_cse_id="cx").search_providers
    for provider in chain:
        assert provider in _PROVIDERS


def test_primary_provider_property_matches_the_chain_head():
    cfg = _settings(serper_api_key="k")
    assert cfg.search_provider == "serper"


# ---------- parsers ----------

SERPER_SAMPLE = {
    "organic": [
        {"title": "pgvector HNSW tuning", "link": "https://example.com/a",
         "snippet": "How ef_search affects recall."},
        {"title": "No link row", "snippet": "must be dropped"},
        {"title": "Second", "link": "https://example.com/b", "snippet": "s"},
    ],
    "answerBox": {"answer": "ignored — not organic"},
}


def test_parse_serper_keeps_organic_results_with_links():
    hits = parse_serper(SERPER_SAMPLE)
    assert [h.url for h in hits] == ["https://example.com/a", "https://example.com/b"]
    assert hits[0].title == "pgvector HNSW tuning"
    assert "ef_search" in hits[0].snippet


def test_parse_serper_survives_junk():
    assert parse_serper({}) == []
    assert parse_serper({"organic": None}) == []
    assert parse_serper({"organic": [{}]}) == []


def test_parse_google_cse_maps_items():
    hits = parse_google_cse({"items": [
        {"title": "T", "link": "https://example.com", "snippet": "S"},
        {"title": "no link"},
    ]})
    assert len(hits) == 1
    assert hits[0].url == "https://example.com"


def test_parse_google_cse_survives_empty_responses():
    """CSE omits `items` entirely when there are no results."""
    assert parse_google_cse({}) == []
    assert parse_google_cse({"searchInformation": {"totalResults": "0"}}) == []


# ---------- DNS pinning (SSRF wave 2) ----------

def test_pinned_resolver_only_answers_for_its_host():
    import asyncio
    import socket

    from inais.integrations.fetch import _PinnedResolver

    resolver = _PinnedResolver("example.com", [(socket.AF_INET, "93.184.216.34")])
    results = asyncio.run(resolver.resolve("example.com", 443, socket.AF_UNSPEC))
    assert results[0]["host"] == "93.184.216.34"
    assert results[0]["hostname"] == "example.com"   # SNI/cert checks keep the real name
    try:
        asyncio.run(resolver.resolve("evil.example.net", 443))
        raise AssertionError("should have refused a different host")
    except OSError:
        pass


def test_fetch_validates_every_redirect_hop():
    import inspect

    from inais.integrations import fetch

    source = inspect.getsource(fetch.fetch_page)
    assert "_resolve_public" in source
    assert "_PinnedResolver" in source
    assert "allow_redirects=False" in source
