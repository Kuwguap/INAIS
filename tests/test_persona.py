"""Personality and the guardrails on speaking unprompted."""

from __future__ import annotations

import pytest

from inais import persona


def test_character_sets_a_voice_not_a_disclaimer_wall():
    text = persona.CHARACTER
    assert "opinions" in text
    assert "Short by default" in text
    # it must inhabit the character without deflating disclaimers...
    assert "never break register" in text
    assert '"as an AI"' in text
    # ...while staying honest under a direct question, and never inventing user facts
    assert "directly asked" in text
    assert "Never invent facts about the user" in text
    assert "never claim to have sent an email" in text.lower()


def test_render_groups_traits_by_kind():
    rows = [
        {"kind": "like", "statement": "clean migrations", "reason": "they never surprise you"},
        {"kind": "dislike", "statement": "meetings before 10am", "reason": ""},
        {"kind": "opinion", "statement": "the finance agent should stay read-only",
         "reason": ""},
    ]
    out = persona.render(rows)
    assert "You like: clean migrations" in out
    assert "You dislike: meetings before 10am" in out
    assert "You think: the finance agent should stay read-only" in out
    assert "they never surprise you" in out


def test_render_is_empty_without_traits():
    assert persona.render([]) == ""


def test_every_trait_kind_renders():
    for kind in persona.TRAIT_KINDS:
        out = persona.render([{"kind": kind, "statement": "x", "reason": ""}])
        assert "x" in out, kind


# ---------- quiet hours ----------

@pytest.mark.parametrize(("hour", "quiet"), [
    (23, True), (2, True), (7, True),      # inside 22:00-08:00
    (8, False), (12, False), (21, False),  # outside
])
def test_quiet_hours_wrap_around_midnight(hour, quiet):
    """The default window crosses midnight, which naive comparisons get wrong."""
    assert persona.in_quiet_hours(hour) is quiet


def test_daytime_quiet_window_does_not_wrap(monkeypatch):
    from inais.config import settings

    cfg = settings()
    monkeypatch.setattr(cfg, "quiet_hours_start", 9, raising=False)
    monkeypatch.setattr(cfg, "quiet_hours_end", 17, raising=False)
    assert persona.in_quiet_hours(12) is True
    assert persona.in_quiet_hours(20) is False


def test_equal_bounds_means_never_quiet(monkeypatch):
    from inais.config import settings

    cfg = settings()
    monkeypatch.setattr(cfg, "quiet_hours_start", 0, raising=False)
    monkeypatch.setattr(cfg, "quiet_hours_end", 0, raising=False)
    assert persona.in_quiet_hours(3) is False


def test_proactive_is_off_by_default():
    """Speaking unprompted is opt-in; a chatty bot gets muted."""
    from inais.config import settings

    assert settings().proactive_enabled is False
    assert settings().proactive_max_per_day <= 5


# ---------- voice request detection ----------

@pytest.mark.parametrize("text", [
    "Send your response as a voice note",
    "reply with voice", "voice note it please", "can you vn this",
    "say it as audio", "record a voice message for me",
])
def test_explicit_voice_requests_are_detected(text):
    from inais.textutil import wants_voice

    assert wants_voice(text)


@pytest.mark.parametrize("text", [
    "what time is my exam", "I need to find my voice in writing",
    "stop reminding me", "summarise this chapter", "",
])
def test_ordinary_messages_do_not_force_voice(text):
    from inais.textutil import wants_voice

    assert not wants_voice(text)


def test_persona_advertises_web_tools():
    """The bot was telling the user to run curl — the persona must forbid that."""
    text = persona.CHARACTER
    assert "web_search" in text and "read_url" in text
    assert "curl" in text  # explicitly told never to suggest it
