"""Pure logic in the planner and study subsystems (no network, no database)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from inais.jobs.reminders import next_cron_fire
from inais.orchestrator.router import rule_route
from inais.study.ingest import chunk_text
from inais.study.quiz import parse_choices
from inais.study.store import build_plan, next_interval
from inais.timeutil import parse_when


# ---------- time resolution ----------

def test_parse_when_relative_minutes():
    before = datetime.now(UTC)
    got = parse_when(in_minutes=20)
    assert got is not None
    delta = (got - before).total_seconds()
    assert 1190 < delta < 1210


def test_parse_when_prefers_relative_over_iso():
    """Models are reliable at 'in 20 minutes' and unreliable at clock arithmetic."""
    got = parse_when(when_iso="2020-01-01T00:00", in_minutes=5)
    assert got is not None and got.year > 2020


def test_parse_when_naive_iso_is_local_and_returned_as_utc():
    got = parse_when(when_iso="2026-09-01T14:30")
    assert got is not None
    assert got.tzinfo is not None
    assert got.utcoffset() == timedelta(0)   # normalised to UTC


def test_parse_when_bare_date_defaults_to_morning():
    got = parse_when(when_iso="2026-09-01")
    assert got is not None and got.astimezone().hour in range(0, 24)


def test_parse_when_rejects_garbage():
    assert parse_when(when_iso="next tuesday-ish") is None
    assert parse_when() is None


def test_parse_when_handles_trailing_z():
    assert parse_when(when_iso="2026-09-01T12:00:00Z") is not None


# ---------- reminders ----------

def test_next_cron_fire_is_in_the_future():
    nxt = next_cron_fire("0 8 * * *")
    assert nxt is not None and nxt > datetime.now(UTC)


def test_next_cron_fire_rejects_invalid_expressions():
    assert next_cron_fire("not a cron") is None
    assert next_cron_fire("99 99 * * *") is None


# ---------- study plan ----------

def test_build_plan_covers_every_topic_and_ends_with_review():
    today = date(2026, 8, 18)
    topics = ["Kinematics", "Newton's laws", "Energy"]
    plan = build_plan(date(2026, 8, 28), topics, today=today)
    assert len(plan) == 10
    assert all(a < b for (a, _), (b, _) in zip(plan, plan[1:], strict=False))  # chronological
    first_pass = " ".join(focus for _, focus in plan if focus.startswith("First pass"))
    for topic in topics:
        assert topic in first_pass
    assert "Full review" in plan[-1][1]


def test_build_plan_is_empty_for_past_or_topicless_exams():
    today = date(2026, 8, 18)
    assert build_plan(date(2026, 8, 1), ["A"], today=today) == []
    assert build_plan(date(2026, 9, 1), [], today=today) == []


def test_build_plan_handles_an_exam_tomorrow():
    today = date(2026, 8, 18)
    plan = build_plan(date(2026, 8, 19), ["Everything"], today=today)
    assert len(plan) == 1


# ---------- spaced repetition ----------

@pytest.mark.parametrize(("current", "correct", "expected"), [
    (1, True, 2), (2, True, 4), (16, True, 30), (30, True, 30),   # doubling, capped at 30
    (8, False, 4), (2, False, 1), (1, False, 1),                  # halving, floored at 1
])
def test_next_interval(current, correct, expected):
    assert next_interval(current, correct) == expected


def test_parse_choices_accepts_json_or_list():
    assert parse_choices(["a", "b"]) == ["a", "b"]
    assert parse_choices('["a", "b"]') == ["a", "b"]
    assert parse_choices(None) == []
    assert parse_choices("not json") == []


# ---------- pdf chunking ----------

def test_chunk_text_splits_long_input_with_overlap():
    para = ("Photosynthesis converts light energy into chemical energy. " * 12).strip()
    text = "\n\n".join([para] * 6)
    chunks = chunk_text(text, size=600, overlap=80)
    assert len(chunks) > 1
    assert all(len(c) <= 700 for c in chunks)
    assert all(len(c) > 40 for c in chunks)


def test_chunk_text_drops_fragments_and_empty_input():
    assert chunk_text("") == []
    assert chunk_text("   \n\n  ") == []
    assert chunk_text("page 3") == []          # too short to be content


def test_chunk_text_keeps_a_single_small_document():
    body = "Ohm's law states that current is proportional to voltage across a conductor."
    assert chunk_text(body) == [body]


# ---------- routing ----------

def test_planner_and_study_keywords_route_correctly():
    assert rule_route("remind me to submit the lab report").agent == "planner"
    assert rule_route("add a task for tomorrow").agent == "planner"
    assert rule_route("quiz me on chapter 4").agent == "study"
    assert rule_route("what's my binance balance?").agent == "finance"
    assert rule_route("draft a reply to that email").agent == "email"


def test_greetings_stay_cheap():
    route = rule_route("hey")
    assert route.agent == "study" and route.complexity == "simple"
