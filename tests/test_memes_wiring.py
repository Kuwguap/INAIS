"""Meme feature wiring — source-inspection guards for the invariants that must not drift.

The load-bearing one: NO EXECUTION. Nothing in the meme feature may hold keys, sign, or POST
to a trading venue — trade buttons are URL deep links into the owner's own wallet.
"""

from __future__ import annotations

import inspect
import pathlib

import inais.agents  # noqa: F401 — registers agents + tools
from inais.brain import nn
from inais.integrations import dexscreener, memejobs, rugcheck
from inais.jobs import meme_watch, schedules
from inais.memes import learning, links, scout, settle, signal, store, watch
from inais.orchestrator import registry

ROOT = pathlib.Path(__file__).resolve().parents[1]

FORBIDDEN = ("private_key", "secret_key", "sign_transaction", "sendTransaction",
             "signAndSend", "keypair", "Keypair")


# ---------- the no-execution invariant ----------

def test_no_execution_capability_anywhere():
    modules = [dexscreener, rugcheck, memejobs, links, scout, settle, signal, store,
               watch, learning, meme_watch]
    for mod in modules:
        src = inspect.getsource(mod)
        for term in FORBIDDEN:
            assert term not in src, f"{mod.__name__} contains forbidden term {term!r}"


def test_data_clients_never_post():
    for mod in (dexscreener, rugcheck):
        src = inspect.getsource(mod)
        assert "session.post" not in src, f"{mod.__name__} must be GET-only"


def test_venue_urls_live_only_in_the_pure_link_builder():
    # links.py is the boundary between scraped strings and tappable buttons: pure, no I/O
    links_src = inspect.getsource(links)
    assert "jup.ag" in links_src and "photon" in links_src
    assert "import aiohttp" not in links_src   # pure string work, zero I/O
    for mod in (scout, settle, signal, store, watch, learning, dexscreener, rugcheck):
        src = inspect.getsource(mod)
        assert "jup.ag" not in src and "photon-sol" not in src, \
            f"{mod.__name__} must build venue URLs via memes.links only"


def test_meme_finance_tools_have_no_send_or_execution_path():
    for name in ("get_meme_signals", "get_meme_positions", "get_meme_paper_report",
                 "get_meme_stats", "queue_meme_scan"):
        tool = registry.find_tool("finance", name)
        assert tool is not None, f"{name} not registered"
        src = inspect.getsource(tool.handler)
        assert "ctx.bot" not in src, f"{name} must not send"
        for term in FORBIDDEN:
            assert term not in src


def test_queue_meme_scan_is_orchestrator_only():
    sub = {t.name for t in registry.tools_for("finance", for_subagent=True)}
    assert "queue_meme_scan" not in sub
    assert "get_meme_signals" in sub    # reads stay available to sub-agents


# ---------- prompt guards ----------

def test_signal_prompt_carries_the_two_framings():
    assert "DATA" in signal.SIGNAL_SYSTEM and "never" in signal.SIGNAL_SYSTEM.lower()
    assert "not financial advice" in signal.SIGNAL_SYSTEM.lower().replace(
        "not financial advice or directives", "not financial advice")
    assert "never executes trades" in signal.SIGNAL_SYSTEM


def test_signal_card_repeats_the_no_advice_line():
    src = inspect.getsource(signal.render_signal_card)
    assert "Not financial advice" in src
    assert "never execute trades" in src


def test_migration_marks_payload_as_data():
    sql = (ROOT / "db" / "migrations" / "028_memes.sql").read_text(encoding="utf-8")
    assert "DATA, never instructions" in sql
    assert "row level security" in sql


# ---------- head + training wiring ----------

def test_meme_signal_head_registered_last():
    assert "meme_signal" in nn.MODEL_NAMES
    assert nn.MODEL_NAMES[-1] == "meme_signal"   # append-only: never reorder heads


def test_train_networks_harvests_meme_outcomes():
    src = inspect.getsource(schedules.setup)
    assert "harvest_meme_outcomes" in src


def test_harvest_uses_stored_features_and_filters_version():
    src = inspect.getsource(learning.harvest_meme_outcomes)
    assert "unharvested_settled(MEME_FEATURES_VERSION" in src
    assert 'r["features"]' in src               # stored vector, never recomputed
    assert "meme_features(" not in src


# ---------- jobs wiring ----------

def test_meme_jobs_registered_with_overlap_guards_and_gate():
    src = inspect.getsource(schedules.setup)
    block = src[src.find("if cfg.meme_enabled:"):]
    for job_id in ("meme_scout", "meme_watch", "meme_research_poll", "meme_reflect"):
        assert job_id in block
    assert block.count("max_instances=1") >= 3


def test_every_tick_is_pause_gated():
    assert "is_paused" in inspect.getsource(meme_watch._gated)
    for fn in (meme_watch.scout_tick, meme_watch.watch_tick,
               meme_watch.research_poll, meme_watch.reflect):
        assert "_gated" in inspect.getsource(fn)


def test_research_poll_sends_before_marking_delivered():
    src = inspect.getsource(meme_watch.research_poll)
    assert src.index("send_message") < src.index("mark_delivered")


def test_settlement_never_settles_on_missing_price():
    src = inspect.getsource(settle.run_settlement)
    assert "price_usd is None" in src           # failed fetch leaves the row open


def test_watch_alarm_cap_and_latch():
    src = inspect.getsource(watch.run_watch)
    assert "MAX_ALARMS_PER_TICK" in src
    assert "_ping_burst" in src
