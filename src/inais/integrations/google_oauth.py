"""Authorize a Google account from Telegram, with no laptop involved.

The old path was `python scripts/authorize_gmail.py` — fine for a developer at a desk, useless
for someone holding a phone. The deployed service already has a public HTTPS URL, so it can
be its own OAuth redirect target: the bot sends a link, the user signs in, Google redirects
back here with a code, and the refresh token lands in the database.

State is an HMAC of a timestamp rather than a stored nonce, so the callback stays verifiable
across restarts and redeploys without a table. `/oauth/callback` is a PUBLIC endpoint —
anything arriving there is untrusted until the signature checks out.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import time

from inais.config import settings

log = logging.getLogger(__name__)

GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.modify"
CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.events"
CALLBACK_PATH = "/oauth/callback"
STATE_TTL_SECONDS = 15 * 60


def scopes() -> list[str]:
    out = [GMAIL_SCOPE]
    if settings().calendar_enabled:
        out.append(CALENDAR_SCOPE)
    return out


def redirect_uri() -> str:
    return f"{settings().render_external_url.rstrip('/')}{CALLBACK_PATH}"


def _secret() -> bytes:
    return hashlib.sha256(f"oauth:{settings().telegram_bot_token}".encode()).digest()


def make_state() -> str:
    """`<issued-at>.<hmac>` — self-verifying, so no server-side nonce store is needed."""
    issued = str(int(time.time()))
    signature = hmac.new(_secret(), issued.encode(), hashlib.sha256).digest()
    return f"{issued}.{base64.urlsafe_b64encode(signature).decode().rstrip('=')}"


def verify_state(state: str) -> bool:
    try:
        issued, signature = (state or "").split(".", 1)
        expected = hmac.new(_secret(), issued.encode(), hashlib.sha256).digest()
        expected_b64 = base64.urlsafe_b64encode(expected).decode().rstrip("=")
        if not hmac.compare_digest(signature, expected_b64):
            return False
        return 0 <= (time.time() - int(issued)) <= STATE_TTL_SECONDS
    except (ValueError, TypeError):
        return False


def client_type() -> str | None:
    """'web', 'installed', or None when the client file is missing/unreadable."""
    try:
        with open(settings().google_oauth_client_json, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    for key in ("web", "installed"):
        if key in data:
            return key
    return None


def configured() -> tuple[bool, str]:
    """(ready, reason) — everything the in-chat flow needs before it can start."""
    cfg = settings()
    kind = client_type()
    if kind is None:
        return False, ("No Google OAuth client file. Add it as a Render Secret File named "
                       "google_oauth_client.json.")
    if not cfg.render_external_url:
        return False, "No public URL yet — this flow only works on the deployed service."
    if kind != "web":
        return False, ("The OAuth client is a Desktop app, which can't redirect back here. "
                       "Create a Web application client and add this redirect URI:\n"
                       f"{redirect_uri()}")
    return True, ""


def _build_flow():
    from google_auth_oauthlib.flow import Flow

    return Flow.from_client_secrets_file(
        settings().google_oauth_client_json, scopes=scopes(), redirect_uri=redirect_uri())


def auth_url() -> str:
    """The link the user taps. prompt=consent forces a refresh token to be issued."""
    flow = _build_flow()
    url, _ = flow.authorization_url(
        access_type="offline", prompt="consent", include_granted_scopes="false",
        state=make_state())
    return url


def _exchange_sync(code: str) -> tuple[str, str]:
    """(email, refresh_token). Blocking — the google libraries are sync."""
    from googleapiclient.discovery import build

    flow = _build_flow()
    flow.fetch_token(code=code)
    creds = flow.credentials
    if not creds.refresh_token:
        raise RuntimeError(
            "Google returned no refresh token. Remove INAIS at "
            "https://myaccount.google.com/permissions and try again.")
    service = build("gmail", "v1", credentials=creds, cache_discovery=False)
    email = service.users().getProfile(userId="me").execute()["emailAddress"].lower()
    return email, creds.refresh_token


async def exchange(code: str) -> tuple[str, str]:
    return await asyncio.to_thread(_exchange_sync, code)


async def store_account(email: str, refresh_token: str) -> None:
    from inais import db

    p = db.pool()
    if p is None:
        raise RuntimeError("No database configured — the token has nowhere to go.")
    await p.execute(
        "insert into gmail_accounts (email, refresh_token, status, last_history_id)"
        " values ($1, $2, 'active', null)"
        " on conflict (email) do update set refresh_token = excluded.refresh_token,"
        " status = 'active', last_history_id = null",
        email, refresh_token,
    )
    log.info("gmail account %s authorized via Telegram", email)
