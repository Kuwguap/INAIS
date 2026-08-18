import base64
from email import message_from_bytes, policy

from inais.integrations.gmail import build_mime, sender_domain


def _decode(raw: str):
    # policy.default decodes RFC 2047 headers back to unicode
    return message_from_bytes(base64.urlsafe_b64decode(raw), policy=policy.default)


def test_build_mime_headers_and_body():
    raw = build_mime("me@gmail.com", "you@example.com", "Hello", "Body text here")
    msg = _decode(raw)
    assert msg["From"] == "me@gmail.com"
    assert msg["To"] == "you@example.com"
    assert msg["Subject"] == "Hello"
    assert "Body text here" in msg.get_content()
    assert msg["In-Reply-To"] is None


def test_build_mime_reply_threading():
    raw = build_mime("me@gmail.com", "you@example.com", "Re: Hello", "ok",
                     in_reply_to="<abc123@mail.example.com>")
    msg = _decode(raw)
    assert msg["In-Reply-To"] == "<abc123@mail.example.com>"
    assert msg["References"] == "<abc123@mail.example.com>"


def test_build_mime_unicode_body():
    raw = build_mime("me@gmail.com", "you@example.com", "Résumé ✓", "héllo wörld 🚀")
    msg = _decode(raw)
    assert "Résumé" in str(msg["Subject"])
    assert "héllo wörld 🚀" in msg.get_content()


def test_sender_domain():
    assert sender_domain("Binance <no-reply@ses.binance.com>") == "ses.binance.com"
    assert sender_domain("plain@binance.com") == "binance.com"
    assert sender_domain("not an email") == ""
