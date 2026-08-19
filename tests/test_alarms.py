"""Alarm-grade reminders: nag scheduling and the stop-phrase matcher (pure logic)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from inais.jobs.reminders import is_stop_text, next_nag_delay


# ---------- nag schedule ----------

def test_nag_delay_doubles_each_time():
    assert next_nag_delay(0, 3) == timedelta(minutes=3)
    assert next_nag_delay(1, 3) == timedelta(minutes=6)
    assert next_nag_delay(2, 3) == timedelta(minutes=12)


def test_nag_delay_never_goes_below_a_minute():
    """A zero-minute base would turn the 30s tick into a machine gun."""
    assert next_nag_delay(0, 0) >= timedelta(minutes=1)
    assert next_nag_delay(3, 0) >= timedelta(minutes=1)


def test_nag_delay_handles_negative_count():
    assert next_nag_delay(-1, 3) == timedelta(minutes=3)


# ---------- the stop phrase ----------

@pytest.mark.parametrize("text", [
    "stop", "Stop", "STOP", "stop!", "stop.", " stop ",
    "stop it", "stop reminder", "stop reminding me", "ok stop",
])
def test_stop_phrases_are_recognised(text):
    assert is_stop_text(text)


@pytest.mark.parametrize("text", [
    # ordinary sentences containing the word must still reach the brain
    "how do I stop a docker container",
    "stop by the shop on your way",
    "should I stop taking notes by hand?",
    "nonstop flights to accra",
    "", "   ",
])
def test_ordinary_sentences_are_not_stop_commands(text):
    assert not is_stop_text(text)
