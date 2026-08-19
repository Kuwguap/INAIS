"""The button menu: every button must lead somewhere, and fit Telegram's limits."""

from __future__ import annotations

from inais.bot.routers import menu

DIRECT = {"status", "pause", "resume"}   # handled inline in on_action


def test_every_button_has_a_handler():
    """A dead button is worse than a missing one — it looks like the bot is broken."""
    menu._register_actions()
    orphans = [
        f"{section}/{action}"
        for section, (_, _, actions) in menu.SECTIONS.items()
        for _, action in actions
        if action not in menu.ACTIONS and action not in menu.CONVERSATIONAL
        and action not in DIRECT
    ]
    assert orphans == []


def test_callback_data_fits_telegrams_limit():
    payloads = [b.callback_data
                for key in menu.SECTIONS
                for row in menu.section_kb(key).inline_keyboard
                for b in row]
    payloads += [b.callback_data for row in menu.home_kb().inline_keyboard for b in row]
    assert payloads
    assert all(len(p.encode()) <= 64 for p in payloads)


def test_home_keyboard_offers_every_section():
    labels = [b.text for row in menu.home_kb().inline_keyboard for b in row]
    assert len(labels) == len(menu.SECTIONS)
    for label, _, _ in menu.SECTIONS.values():
        assert label in labels


def test_every_section_can_get_back_home():
    for key in menu.SECTIONS:
        payloads = [b.callback_data for row in menu.section_kb(key).inline_keyboard for b in row]
        assert "m:home" in payloads


def test_rows_stay_narrow_enough_to_read_on_a_phone():
    for key in menu.SECTIONS:
        for row in menu.section_kb(key).inline_keyboard:
            assert len(row) <= 3


def test_conversational_actions_point_at_a_command():
    """Buttons must never silently arm an FSM state that changes what your next message means."""
    for action, text in menu.CONVERSATIONAL.items():
        assert "/" in text, f"{action} should name the command to send"


def test_sections_and_actions_are_uniquely_keyed():
    actions = [a for _, _, acts in menu.SECTIONS.values() for _, a in acts]
    assert len(actions) == len(set(actions)), "an action key appears in two sections"
