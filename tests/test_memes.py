"""Meme-coin intelligence — pure logic. No DB, no network (project test idiom)."""

from __future__ import annotations

import math

import pytest

from inais.integrations.dexscreener import Pair, age_minutes, parse_pair
from inais.integrations.rugcheck import RugReport, parse_report
from inais.memes import features, links, screener

MINT = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"      # BONK mint (valid base58)
PAIR_ADDR = "HVNwzt7Pxfu76KHCMQPTLuTCLTm6WnQ1esLv4eizseSv"  # valid base58

NOW_MS = 1_700_000_000_000


def make_pair(**over) -> Pair:
    base = dict(
        mint=MINT, pair_address=PAIR_ADDR, symbol="BONK", name="Bonk",
        price_usd=0.000021, liquidity_usd=150_000.0, fdv_usd=1_400_000.0,
        volume_h24=800_000.0, volume_h1=40_000.0,
        change_m5=1.2, change_h1=8.0, change_h24=42.0,
        buys_h1=120, sells_h1=80, created_at_ms=NOW_MS - 6 * 3600 * 1000,
        has_socials=True, has_website=True,
    )
    base.update(over)
    return Pair(**base)


def make_report(**over) -> RugReport:
    base = dict(score=10.0, mint_authority_active=False, freeze_authority_active=False,
                lp_locked_pct=95.0, top10_holder_pct=22.0, holder_count=5000, risks=[])
    base.update(over)
    return RugReport(**base)


# ---------- dexscreener parsing ----------

def test_parse_pair_tolerates_missing_keys():
    raw = {"baseToken": {"address": MINT, "symbol": "X"}, "pairAddress": PAIR_ADDR}
    p = parse_pair(raw)
    assert p is not None and p.mint == MINT and p.price_usd is None and p.buys_h1 == 0


def test_parse_pair_rejects_unusable():
    assert parse_pair({}) is None
    assert parse_pair({"baseToken": {}, "pairAddress": ""}) is None
    assert parse_pair("not a dict") is None


def test_age_minutes():
    p = make_pair(created_at_ms=NOW_MS - 90 * 60 * 1000)
    assert age_minutes(p, NOW_MS) == pytest.approx(90.0)
    assert age_minutes(make_pair(created_at_ms=None), NOW_MS) is None


# ---------- rugcheck parsing (conservative defaults) ----------

def test_parse_report_unknowns_read_as_risky():
    r = parse_report({})
    assert r.mint_authority_active is True      # unknown = assumed active = risky
    assert r.freeze_authority_active is True
    assert r.lp_locked_pct is None


def test_parse_report_reads_lp_and_holders():
    r = parse_report({
        "mintAuthority": None, "freezeAuthority": None,
        "markets": [{"lp": {"lpLockedPct": 98.5}}],
        "topHolders": [{"pct": 5.0}] * 12,
        "totalHolders": 3000,
        "risks": [{"name": "Low liquidity"}],
    })
    assert r.mint_authority_active is False
    assert r.lp_locked_pct == pytest.approx(98.5)
    assert r.top10_holder_pct == pytest.approx(50.0)   # only first 10 summed
    assert r.risks == ["Low liquidity"]


# ---------- feature vector: THE versioning contract ----------

def test_feature_vector_length_and_version_locked():
    # Changing FEATURE_LEN or the layout without bumping MEME_FEATURES_VERSION corrupts the
    # meme_signal head: ragged context arrays break training, and a mismatched serving vector
    # silently returns None from nn.score. See features.py docstring for the bump procedure.
    assert features.FEATURE_LEN == 12
    assert features.MEME_FEATURES_VERSION == 1
    vec = features.meme_features(make_pair(), make_report(), age_min=360.0)
    assert len(vec) == features.FEATURE_LEN
    assert all(isinstance(v, float) and math.isfinite(v) and -1.0 <= v <= 1.0 for v in vec)


def test_missing_data_yields_neutral_defaults():
    bare = Pair(mint=MINT, pair_address=PAIR_ADDR, symbol="?", name="?", price_usd=None,
                liquidity_usd=None, fdv_usd=None, volume_h24=None, volume_h1=None,
                change_m5=None, change_h1=None, change_h24=None, buys_h1=0, sells_h1=0,
                created_at_ms=None, has_socials=False, has_website=False)
    vec = features.meme_features(bare, None)
    assert len(vec) == features.FEATURE_LEN
    assert vec[7] == 0.5        # buy pressure neutral when no txns
    assert vec[8] == 0.5        # holder concentration neutral when unknown
    assert vec[9] == 0.0 and vec[10] == 0.0   # no report → not locked, not renounced


# ---------- hard screen (fails closed) ----------

KW = dict(min_liquidity=20000, min_age_minutes=30, max_top10_pct=40.0, now_ms=NOW_MS)


def test_clean_pair_passes():
    assert screener.hard_reject(make_pair(), make_report(), **KW) is None


@pytest.mark.parametrize("pair_over,report_over,reason", [
    ({"liquidity_usd": 500}, {}, "low_liquidity"),
    ({"created_at_ms": NOW_MS - 5 * 60 * 1000}, {}, "too_young"),
    ({"created_at_ms": None}, {}, "too_young"),
    ({}, {"mint_authority_active": True}, "mint_authority"),
    ({}, {"freeze_authority_active": True}, "freeze_authority"),
    ({}, {"lp_locked_pct": 10.0}, "lp_unlocked"),
    ({}, {"top10_holder_pct": 80.0}, "holder_concentration"),
    ({}, {"score": 90.0}, "rug_score"),
])
def test_hard_reject_matrix(pair_over, report_over, reason):
    assert screener.hard_reject(make_pair(**pair_over), make_report(**report_over), **KW) == reason


def test_missing_rug_report_fails_closed():
    assert screener.hard_reject(make_pair(), None, **KW) == "no_rug_report"


def test_soft_flags():
    flags = screener.soft_flags(make_pair(has_socials=False, has_website=False,
                                          buys_h1=5, sells_h1=40),
                                make_report(holder_count=50, risks=["Copycat token"]))
    assert "no socials/website" in flags
    assert "only 50 holders" in flags
    assert "rugcheck: Copycat token" in flags
    assert "heavy selling" in flags


# ---------- deep links (the scraped-string → button boundary) ----------

def test_deep_links_exact_urls():
    assert links.chart_url(PAIR_ADDR) == f"https://dexscreener.com/solana/{PAIR_ADDR}"
    assert links.jupiter_url(MINT) == f"https://jup.ag/swap/SOL-{MINT}"
    assert links.photon_url(PAIR_ADDR).endswith(f"/lp/{PAIR_ADDR}")


@pytest.mark.parametrize("bad", [
    "", "short", "0" * 40, "O" * 40, "I" * 40, "l" * 40,          # non-base58 chars
    "javascript:alert(1)", "https://evil.example/x", MINT + "/extra",
    MINT + "?q=1", "x" * 60,
])
def test_links_reject_non_base58(bad):
    assert links.valid_address(bad) is False
    assert links.chart_url(bad) is None
    assert links.jupiter_url(bad) is None
    assert links.photon_url(bad) is None
