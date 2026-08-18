from inais.bot.keyboards import draft_approval_kb, email_notification_kb


def _all_callback_data(kb):
    return [btn.callback_data for row in kb.inline_keyboard for btn in row]


def test_draft_keyboard_callback_data_under_64_bytes():
    kb = draft_approval_kb(999_999_999_999)  # even absurd ids stay tiny
    for data in _all_callback_data(kb):
        assert len(data.encode()) <= 64


def test_notification_keyboard_prefixes():
    kb = email_notification_kb(7)
    assert _all_callback_data(kb) == ["dra:7", "ign:7"]


def test_draft_keyboard_prefixes():
    kb = draft_approval_kb(12)
    assert _all_callback_data(kb) == ["apr:12", "edt:12", "rej:12"]
