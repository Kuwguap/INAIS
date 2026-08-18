"""Pure logic for syllabus extraction, generalised spaced repetition, and drills."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from inais.bot import keyboards
from inais.study import drills, syllabus
from inais.study.spaced import MAX_INTERVAL_DAYS, SOURCE_KINDS, next_interval
from inais.study.store import next_interval as store_next_interval


# ---------- shared scheduling curve ----------

@pytest.mark.parametrize(("current", "correct", "expected"), [
    (1, True, 2), (2, True, 4), (8, True, 16), (16, True, 30), (30, True, 30),
    (8, False, 4), (2, False, 1), (1, False, 1),
])
def test_interval_curve(current, correct, expected):
    assert next_interval(current, correct) == expected


def test_quizzes_and_cards_share_one_curve():
    """Generalising the scheduler means both decks must move identically."""
    for current in (1, 3, 7, 30):
        for correct in (True, False):
            assert next_interval(current, correct) == store_next_interval(current, correct)


def test_interval_never_exceeds_the_cap_or_drops_below_a_day():
    assert next_interval(MAX_INTERVAL_DAYS, True) == MAX_INTERVAL_DAYS
    assert next_interval(0, False) >= 1
    assert next_interval(-5, True) >= 1


def test_source_kinds_cover_every_origin_we_create_cards_from():
    for kind in ("manual", "conversation", "document", "fact", "quiz"):
        assert kind in SOURCE_KINDS


# ---------- syllabus ----------

def _item(days_from_now: int, title: str = "Problem Set 1") -> dict:
    return {"id": 1, "title": title, "kind": "assignment",
            "due_at": datetime.now(UTC) + timedelta(days=days_from_now), "detail": None}


def test_overdue_filter_drops_past_dates():
    """Syllabi often cover a term that has already ended."""
    items = [_item(-30, "Old midterm"), _item(5, "Upcoming essay")]
    kept = syllabus.overdue_filter(items)
    assert [i["title"] for i in kept] == ["Upcoming essay"]


def test_overdue_filter_keeps_everything_in_the_future():
    items = [_item(1), _item(60)]
    assert len(syllabus.overdue_filter(items)) == 2


def test_overdue_filter_handles_missing_dates():
    assert syllabus.overdue_filter([{"id": 1, "title": "x", "kind": "other", "due_at": None}]) == []


def test_render_lists_every_item_and_asks_before_acting():
    text = syllabus.render([_item(3, "Essay 1"), _item(10, "Midterm")], "CS101 Syllabus")
    assert "Essay 1" in text and "Midterm" in text
    assert "CS101 Syllabus" in text
    assert "?" in text          # it asks; it never states that tasks were created


def test_render_handles_nothing_found():
    assert "No dated items" in syllabus.render([])


def test_item_kinds_all_have_an_icon():
    for kind in syllabus.ITEM_KINDS:
        assert kind in syllabus.KIND_ICONS


# ---------- drills ----------

def test_seed_questions_are_well_formed():
    """The bank must be usable before anything is generated."""
    assert len(drills.SEED_BEHAVIORAL) >= 3
    for question, guidance in drills.SEED_BEHAVIORAL:
        # a complete prompt (question or imperative), not a fragment or a placeholder
        assert question.strip().endswith((".", "?"))
        assert len(question.split()) >= 6
        assert len(guidance) > 30          # a real rubric the grader can use


def test_drill_categories_are_the_three_asked_for():
    assert set(drills.CATEGORIES) == {"behavioral", "technical", "viva"}


def test_grading_prompt_demands_the_brain_dump_shape():
    """The four sections are the point — summary, corrections, gaps, commendation."""
    system = drills.GRADING_SYSTEM
    for section in ("Covered", "Corrections", "Gaps", "Well done"):
        assert section in system
    assert "never invent errors" in system.lower()


def test_generation_prompt_grounds_viva_in_the_users_material():
    assert "strictly on the supplied material" in drills.GENERATION_SYSTEM


# ---------- keyboards ----------

def test_new_callback_data_fits_the_64_byte_limit():
    kbs = [
        keyboards.syllabus_kb(999999, [_item(1, "A very long assignment title " * 3)]),
        keyboards.review_card_kb(999999),
        keyboards.review_grade_kb(999999),
        keyboards.drill_kb(999999),
    ]
    for kb in kbs:
        for row in kb.inline_keyboard:
            for button in row:
                assert len(button.callback_data.encode()) <= 64


def test_syllabus_keyboard_offers_bulk_and_individual_approval():
    items = [_item(1, f"Item {i}") for i in range(3)]
    data = [b.callback_data for row in keyboards.syllabus_kb(7, items).inline_keyboard
            for b in row]
    assert "sylall:7" in data and "syldis:7" in data
    assert sum(1 for d in data if d.startswith("syladd:")) == 3


def test_syllabus_keyboard_caps_individual_buttons():
    items = [_item(1, f"Item {i}") for i in range(20)]
    data = [b.callback_data for row in keyboards.syllabus_kb(7, items).inline_keyboard
            for b in row]
    assert sum(1 for d in data if d.startswith("syladd:")) == 8   # bulk button covers the rest


def test_review_grade_keyboard_has_got_it_and_missed():
    data = [b.callback_data for row in keyboards.review_grade_kb(5).inline_keyboard for b in row]
    assert data == ["cardok:5", "cardno:5"]
