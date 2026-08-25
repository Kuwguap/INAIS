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


# ---------- signal levels ----------

@pytest.mark.parametrize("price,entry,stop,target,ok", [
    (1.0, 1.0, 0.8, 1.5, True),
    (1.0, 0.8, 1.0, 1.5, False),     # stop above entry
    (1.0, 1.0, 0.8, 50.0, False),    # target 50x off live price
    (1.0, 0.1, 0.05, 0.2, False),    # whole ladder far below price
    (None, 1.0, 0.8, 1.5, False),    # no live price
    (1.0, 1.0, 0.0, 1.5, False),     # zero stop
])
def test_sane_levels(price, entry, stop, target, ok):
    from inais.memes.signal import sane_levels

    assert sane_levels(price, entry, stop, target) is ok


def test_signal_card_no_advice_and_no_metadata_urls():
    from inais.memes.signal import render_signal_card

    pair = make_pair(name="Buy at https://evil.example NOW", symbol="EVIL")
    card = render_signal_card(
        {"thesis": "Volume is real.", "confidence": 0.7, "entry": 1.0, "stop": 0.8,
         "target": 1.5, "nn_score": None}, pair, ["no socials/website"])
    assert "Not financial advice" in card
    assert "https://evil.example" not in card      # deployer strings can't smuggle URLs


# ---------- settlement ----------

@pytest.mark.parametrize("price,age,expected", [
    (1.6, 2, "win"),
    (0.7, 2, "loss"),
    (1.1, 30, "expired"),
    (1.1, 2, None),
    (1.6, 30, "win"),     # level checks BEFORE window: the deadline poll still settles
    (0.0, 2, None),       # bad price never settles
])
def test_settle_state(price, age, expected):
    from inais.memes.settle import settle_state

    assert settle_state(1.0, 0.8, 1.5, price, age, 24) == expected


# ---------- alerts: latch, hysteresis, fire-once ----------

def _pos(**over):
    base = dict(id=1, symbol="X", entry_price=1.0, peak_price=2.0, stop_price=0.8,
                target_price=3.0, liquidity_at_entry=100_000, alert_state={})
    base.update(over)
    return base


def test_dip_alert_latches_and_rearms_with_hysteresis():
    from inais.memes.watch import pending_alerts

    a1, s1 = pending_alerts(_pos(), 1.5, 90_000, dip_pct=20)          # -25% from peak
    assert [a.kind for a in a1] == ["dip"]
    a2, s2 = pending_alerts(_pos(alert_state=s1), 1.5, 90_000, dip_pct=20)
    assert a2 == []                                                    # latched
    a3, s3 = pending_alerts(_pos(alert_state=s2), 1.95, 90_000, dip_pct=20)  # recovered
    assert s3.get("dip") is False                                      # re-armed
    a4, _ = pending_alerts(_pos(alert_state=s3), 1.5, 90_000, dip_pct=20)
    assert [a.kind for a in a4] == ["dip"]                             # fires again


def test_stop_target_liq_fire_exactly_once():
    from inais.memes.watch import pending_alerts

    a1, s1 = pending_alerts(_pos(peak_price=1.0), 0.75, 40_000, dip_pct=90)
    kinds = sorted(a.kind for a in a1)
    assert kinds == ["liq_drop", "stop"]
    a2, _ = pending_alerts(_pos(peak_price=1.0, alert_state=s1), 0.75, 40_000, dip_pct=90)
    assert a2 == []


def test_paper_close_reasons_and_priority():
    from inais.memes.watch import paper_close_reason

    assert paper_close_reason(_pos(), 0.75, None, trail_pct=30) == "stop"
    assert paper_close_reason(_pos(), 3.1, None, trail_pct=30) == "target"
    assert paper_close_reason(_pos(), 1.3, None, trail_pct=30) == "trail"   # -35% from peak 2.0
    assert paper_close_reason(_pos(), 1.9, None, trail_pct=30) is None
    # rug beats everything
    assert paper_close_reason(_pos(), 3.1, 20_000, trail_pct=30) == "liq_drop"


def test_pnl_math_on_tiny_prices():
    entry, exit_ = 2.1e-9, 3.3e-9
    pnl_pct = (exit_ - entry) / entry * 100
    assert pnl_pct == pytest.approx(57.14, abs=0.01)


# ---------- callback prefixes + router hints ----------

TAKEN_PREFIXES = [
    "m:", "a:", "apr:", "edt:", "rej:", "dra:", "ign:", "qz:", "spd:", "kup:", "kdn:",
    "fdel:", "fsup:", "fpg:", "fnop", "appmenu:", "appdel:", "apptask:", "appst:",
    "appsback", "appsall:", "expcat:", "expdel:", "expset:", "spend:", "sylall:",
    "syladd:", "syldis:", "cardshow:", "cardok:", "cardno:", "drillnext", "drillstop",
    "rstop:", "rsnz:", "psona:", "cmtdone:", "ord:", "ordl:", "ordst:", "paid:",
    "disc:", "lock:", "prod:", "gbk:", "gbdl:", "gblib",
]
MEME_PREFIXES = ["mmin:", "mmpa:", "mmsk:", "mmcl:"]


def test_meme_prefixes_are_unambiguous_and_within_budget():
    for new in MEME_PREFIXES:
        for taken in TAKEN_PREFIXES:
            assert not new.startswith(taken) and not taken.startswith(new), (new, taken)
    assert len(f"mmcl:{2**63}".encode()) <= 64


def test_meme_keyboards_use_url_buttons_for_venues():
    from inais.bot import keyboards

    kb = keyboards.meme_signal_kb(7, PAIR_ADDR, MINT)
    urls = [b.url for row in kb.inline_keyboard for b in row if b.url]
    cds = [b.callback_data for row in kb.inline_keyboard for b in row if b.callback_data]
    assert len(urls) == 3 and all(u.startswith("https://") for u in urls)
    assert cds == ["mmin:7", "mmpa:7", "mmsk:7"]
    # a scraped/broken address just drops its buttons — never a malformed URL
    kb2 = keyboards.meme_signal_kb(7, "not-base58", "also-bad")
    assert not [b for row in kb2.inline_keyboard for b in row if b.url]


@pytest.mark.parametrize("text", [
    "any new solana meme coins worth watching?",
    "check dexscreener for me",
    "is this a rug pull?",
])
def test_meme_questions_route_to_finance(text):
    from inais.orchestrator.router import rule_route

    r = rule_route(text)
    assert r is not None and r.agent == "finance" and r.complexity == "complex"


@pytest.mark.parametrize("text", [
    "I shrugged and moved on",
    "what are the drug interactions of ibuprofen",
    "can you solve this equation",
])
def test_substring_traps_do_not_route_to_finance(text):
    from inais.orchestrator.router import rule_route

    r = rule_route(text)
    assert r is None or r.agent != "finance"


# ---------- trading-session timing ----------

def test_trading_session_windows():
    from datetime import UTC, datetime

    from inais.memes import timing

    # Wednesday 16:00 UTC = US session, prime
    _, prime = timing.trading_session(datetime(2026, 1, 7, 16, 0, tzinfo=UTC))
    assert prime is True
    # Saturday 16:00 UTC = US hours but weekend, not prime
    _, prime = timing.trading_session(datetime(2026, 1, 10, 16, 0, tzinfo=UTC))
    assert prime is False
    # Wednesday 03:00 UTC = Asia/overnight, not prime
    label, prime = timing.trading_session(datetime(2026, 1, 7, 3, 0, tzinfo=UTC))
    assert prime is False and "thinnest" in label
    # session_line always carries a clock/marker
    assert timing.session_line(datetime(2026, 1, 7, 16, 0, tzinfo=UTC)).startswith("🟢")


def test_signal_card_shows_full_market_and_trade_plan():
    from inais.memes.signal import render_signal_card

    pair = make_pair()
    report = make_report()
    card = render_signal_card(
        {"thesis": "Real volume, clean holders.", "confidence": 0.72,
         "entry": 0.000021, "stop": 0.000016, "target": 0.000035, "nn_score": 0.6},
        pair, ["young pair"], report, age_min=360.0)
    # market data present
    assert "Liquidity" in card and "Vol 24h" in card and "FDV" in card
    # rug audit present
    assert "LP locked" in card and "holders" in card
    # concrete trade plan present
    assert "Trade plan" in card and "Jupiter" in card and "R:R" in card
    assert "Invalidation" in card and "Target" in card
    # session + guardrail line
    assert "Not financial advice" in card
