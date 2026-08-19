"""In-chat Google authorization: the state signature is the only thing guarding a public URL."""

from __future__ import annotations

import time

from inais.integrations import google_oauth


def test_state_round_trips():
    assert google_oauth.verify_state(google_oauth.make_state())


def test_tampered_state_is_rejected():
    """`/oauth/callback` is public — a forged state must not be able to attach an account."""
    state = google_oauth.make_state()
    issued, signature = state.split(".", 1)
    assert not google_oauth.verify_state(f"{int(issued) + 1}.{signature}")
    assert not google_oauth.verify_state(f"{issued}.{signature[:-2]}xy")


def test_malformed_state_is_rejected():
    for bad in ("", "nonsense", "no-dot", "...", "abc.def"):
        assert not google_oauth.verify_state(bad)


def test_expired_state_is_rejected():
    old = str(int(time.time()) - google_oauth.STATE_TTL_SECONDS - 60)
    import base64
    import hashlib
    import hmac

    secret = google_oauth._secret()
    sig = hmac.new(secret, old.encode(), hashlib.sha256).digest()
    forged = f"{old}.{base64.urlsafe_b64encode(sig).decode().rstrip('=')}"
    assert not google_oauth.verify_state(forged)


def test_future_state_is_rejected():
    """A clock-skewed or hand-crafted future timestamp shouldn't be honoured."""
    import base64
    import hashlib
    import hmac

    future = str(int(time.time()) + 3600)
    sig = hmac.new(google_oauth._secret(), future.encode(), hashlib.sha256).digest()
    assert not google_oauth.verify_state(
        f"{future}.{base64.urlsafe_b64encode(sig).decode().rstrip('=')}")


def test_scopes_follow_the_calendar_flag():
    assert google_oauth.GMAIL_SCOPE in google_oauth.scopes()


def test_configured_explains_what_is_missing():
    ready, reason = google_oauth.configured()
    assert ready is False
    assert reason  # never a silent failure
