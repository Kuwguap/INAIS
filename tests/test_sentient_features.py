"""Snooze, cloze cards, reading digest, commitments, and the sentient-proactive additions.

Pure-logic and source-inspection only — no live DB (matching the project's test idiom).
"""

from __future__ import annotations

import inspect

import pytest

from inais.agents import applications, commitments
from inais.bot import keyboards
from inais.jobs import proactive, reminders
from inais.study import cloze


# ---------- reminder snooze ----------

def test_reminder_kb_has_stop_and_snooze():
    cds = [b.callback_data for row in keyboards.reminder_stop_kb(7).inline_keyboard for b in row]
    assert "rstop:7" in cds
    assert "rsnz:7:10" in cds and "rsnz:7:60" in cds
    assert all(len(c.encode()) <= 64 for c in cds)


def test_snooze_treats_recurring_and_oneshot_differently():
    """One-shot re-arms in place; recurring must NOT overwrite fire_at (that's the next slot),
    it inserts a decoupled one-shot copy instead."""
    src = inspect.getsource(reminders.snooze)
    assert "recurring_cron" in src
    assert "fired = false" in src                 # one-shot path re-enters deliver_due
    assert "insert into reminders" in src         # recurring path adds a one-shot copy


# ---------- cloze flashcards ----------

@pytest.mark.parametrize("text,answer,expected", [
    ("The mitochondria is the powerhouse of the cell", "powerhouse",
     "The mitochondria is the _____ of the cell"),
    ("Python uses duck typing", "Duck Typing", "Python uses _____"),  # case-insensitive
])
def test_mask_cloze_blanks_the_answer(text, answer, expected):
    assert cloze.mask_cloze(text, answer) == expected


def test_mask_cloze_appends_blank_when_answer_absent():
    out = cloze.mask_cloze("A sentence with no match", "xyz")
    assert out.endswith("(_____)")


def test_mask_cloze_masks_only_the_first_occurrence():
    assert cloze.mask_cloze("go go go", "go") == "_____ go go"


@pytest.mark.parametrize("item,ok", [
    ({"sentence": "The cell wall is rigid", "answer": "rigid"}, True),
    ({"sentence": "", "answer": "x"}, False),
    ({"sentence": "short", "answer": "short"}, False),   # answer not shorter than sentence
])
def test_cloze_validator(item, ok):
    assert cloze._valid(item) is ok


# ---------- commitments ----------

def test_commitment_due_date_offsets_from_days():
    from datetime import date

    d = commitments._due_date({"due_in_days": 3})
    assert isinstance(d, date)
    assert commitments._due_date({}) is None
    assert commitments._due_date({"due_in_days": "nope"}) is None


def test_commitments_render_empty_and_populated():
    assert "No open commitments" in commitments.render([])
    text = commitments.render([{"id": 1, "text": "email advisor", "due_at": None}])
    assert "#1" in text and "email advisor" in text


def test_commitments_kb_done_buttons_and_empty():
    assert keyboards.commitments_kb([]) is None
    kb = keyboards.commitments_kb([{"id": 5, "text": "x", "due_at": None}])
    cd = kb.inline_keyboard[0][0].callback_data
    assert cd == "cmtdone:5" and len(cd.encode()) <= 64


# ---------- observant / while-you-were-away proactive additions ----------

def test_stale_applications_queries_by_update_age():
    src = inspect.getsource(applications.stale_applications)
    assert "updated_at" in src and "make_interval" in src
    assert "rejected" in src and "withdrawn" in src   # terminal states excluded


@pytest.mark.parametrize("message,topic,expected", [
    ("Here's a note on transformer attention I found", "transformer attention", True),
    ("Good morning, hope today goes well", "binance trading fees", False),
    ("", "anything", False),
])
def test_mentions_gates_the_surfaced_flag(message, topic, expected):
    assert proactive._mentions(message, topic) is expected


def test_context_returns_a_candidate_tuple():
    """_context now returns (text, candidate) so consider() can de-dupe shared knowledge."""
    src = inspect.getsource(proactive._context)
    assert "surfaced_at is null" in src
    assert "tuple[str, dict | None]" in src


def test_consider_marks_knowledge_surfaced_after_sharing():
    src = inspect.getsource(proactive.consider)
    assert "surfaced_at = now()" in src
    assert "_mentions" in src


# ---------- mood-aware behaviour ----------

def test_direction_needs_a_real_trend():
    from inais import journal

    assert journal._direction([1.0, 1.0, 2.0, 2.0]) == "improving"
    assert journal._direction([2.0, 2.0, 0.5, 0.5]) == "declining"
    assert journal._direction([1.0, 1.1, 1.0, 1.05]) == ""   # noise, not a trend
    assert journal._direction([1.0, 2.0]) == ""              # too few points


def test_new_journal_entry_invalidates_the_affect_cache():
    from inais import journal

    assert "invalidate" in inspect.getsource(journal.add_entry)


# ---------- reading digest ----------

def test_reading_digest_is_none_when_nothing_unread():
    from inais.jobs import reading

    assert reading.build([]) is None
    assert reading.build([{"id": 1, "title": "t", "source_url": "u", "summary": "s"}])


def test_reading_digest_marks_read_only_after_send():
    """Guard the mark-read-after-send ordering so a failed send doesn't lose the queue."""
    from inais.jobs import schedules

    src = inspect.getsource(schedules.setup)
    idx_send = src.find("reading_digest")
    block = src[idx_send:idx_send + 800]
    assert "mark_read" in block
    assert block.index("send_message") < block.index("mark_read")
