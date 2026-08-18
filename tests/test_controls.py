"""Safety controls: turn tracing, the pause flag cache, and fact-browser callback data."""

from __future__ import annotations

import asyncio

import pytest

from inais import controls, trace
from inais.bot import keyboards


@pytest.fixture(autouse=True)
def _clean_state():
    trace.clear()
    controls._cache.clear()
    yield
    trace.clear()
    controls._cache.clear()


# ---------- trace ring buffer ----------

def test_turn_enters_the_ring_immediately():
    """A turn that crashes mid-flight must still be inspectable with /why."""
    trace.begin(1, "hello")
    assert trace.count() == 1
    assert trace.recent(1)[0].user_text == "hello"


def test_ring_buffer_drops_the_oldest_turns():
    for i in range(trace.RING_SIZE + 7):
        trace.begin(1, f"turn {i}")
    assert trace.count() == trace.RING_SIZE
    newest = trace.recent(1)[0]
    assert newest.user_text == f"turn {trace.RING_SIZE + 6}"


def test_recent_is_newest_first_and_nth_back_is_last():
    for i in range(5):
        trace.begin(1, f"turn {i}")
    turns = trace.recent(3)
    assert [t.user_text for t in turns] == ["turn 4", "turn 3", "turn 2"]
    assert turns[-1].user_text == "turn 2"   # this is what /why 3 shows


def test_costs_and_tokens_accumulate_across_calls():
    trace.begin(1, "q")
    trace.record_llm("openai", "gpt-5-mini", "routing", 100, 10, 0.0001)
    trace.record_llm("anthropic", "claude-sonnet-5", "agent:study", 2000, 400, 0.012)
    turn = trace.recent(1)[0]
    assert turn.input_tokens == 2100
    assert turn.output_tokens == 410
    assert turn.cost_usd == pytest.approx(0.0121)


def test_tool_failures_are_visible_in_the_render():
    trace.begin(1, "check my mail")
    trace.record_tool("list_recent_emails", ok=False, detail="token expired")
    rendered = trace.recent(1)[0].render()
    assert "FAILED" in rendered
    assert "token expired" in rendered


def test_render_includes_route_memory_and_totals():
    turn = trace.begin(1, "what is my portfolio worth?")
    turn.agent, turn.complexity, turn.route_source = "finance", "complex", "rule"
    turn.memory_counts = {"facts": 2, "episodes": 0, "knowledge": 1, "rules": 0}
    turn.memory_items = ["fact: trades BTC"]
    trace.record_llm("anthropic", "claude-sonnet-5", "agent:finance", 500, 50, 0.0025)
    trace.finish(reply="About $4,000.")
    rendered = turn.render()
    assert "finance" in rendered and "routed by rule" in rendered
    assert "facts 2" in rendered and "fact: trades BTC" in rendered
    assert "$0.0025" in rendered
    assert "About $4,000." in rendered


def test_errors_are_recorded_instead_of_the_reply():
    trace.begin(1, "boom")
    trace.finish(error="RuntimeError: upstream 500")
    rendered = trace.recent(1)[0].render()
    assert "error" in rendered and "upstream 500" in rendered


def test_recording_outside_a_turn_is_a_no_op():
    """Scheduled jobs call the same LLM gateway with no turn in context."""
    trace.record_llm("openai", "gpt-5-mini", "email-triage", 10, 1, 0.0)
    trace.record_tool("poll", ok=True)
    trace.finish(reply="x")
    assert trace.count() == 0


def test_background_task_attributes_spend_to_its_turn():
    """create_task copies the context, so late embeddings still land on the right turn."""
    async def scenario():
        trace.begin(1, "remember this")

        async def late_embedding():
            await asyncio.sleep(0)
            trace.record_llm("openai", "text-embedding-3-small", "embedding", 50, 0, 0.000001)

        task = asyncio.create_task(late_embedding())
        await task

    asyncio.run(scenario())
    assert trace.recent(1)[0].llm_calls[0].purpose == "embedding"


# ---------- pause flag ----------

def test_pause_defaults_to_running():
    assert controls.is_paused() is False


def test_is_paused_reads_the_cache():
    controls._cache[controls.PAUSED] = True
    assert controls.is_paused() is True
    controls._cache[controls.PAUSED] = False
    assert controls.is_paused() is False


def test_set_paused_without_a_database_reports_failure():
    """Refusing beats silently pretending a pause survived a restart."""
    assert asyncio.run(controls.set_paused(True)) is False
    assert controls.is_paused() is False


# ---------- facts keyboard ----------

def _facts(n: int, start: int = 1) -> list[dict]:
    return [{"id": i, "category": "general", "statement": f"fact {i}", "confidence": 0.9}
            for i in range(start, start + n)]


def test_facts_keyboard_has_delete_and_fix_per_fact():
    kb = keyboards.facts_kb(_facts(3), offset=0, total=3, page_size=5)
    data = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "fdel:1:0" in data and "fsup:1" in data
    assert all(len(d.encode()) <= 64 for d in data)   # Telegram's hard limit


def test_first_page_has_next_but_no_prev():
    kb = keyboards.facts_kb(_facts(5), offset=0, total=12, page_size=5)
    data = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "fpg:5" in data
    assert not any(d.startswith("fpg:") and d != "fpg:5" for d in data)


def test_middle_page_has_both_directions():
    kb = keyboards.facts_kb(_facts(5, start=6), offset=5, total=12, page_size=5)
    data = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "fpg:0" in data and "fpg:10" in data


def test_last_page_has_no_next():
    kb = keyboards.facts_kb(_facts(2, start=11), offset=10, total=12, page_size=5)
    data = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "fpg:5" in data
    assert not any(d == "fpg:15" for d in data)


def test_page_counter_label_is_correct():
    kb = keyboards.facts_kb(_facts(5, start=6), offset=5, total=12, page_size=5)
    labels = [b.text for row in kb.inline_keyboard for b in row]
    assert "2/3" in labels
