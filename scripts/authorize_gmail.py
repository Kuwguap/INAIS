"""Authorize a Gmail account (run locally, once per account — opens a browser).

Usage: python scripts/authorize_gmail.py [expected@gmail.com]

Prerequisite (one-time, in Google Cloud Console):
  1. Create a project, enable the Gmail API.
  2. OAuth consent screen: External, then PUBLISH it ("In production") WITHOUT verification —
     otherwise refresh tokens expire after 7 days. The "unverified app" warning is expected;
     click Advanced → "Go to <app> (unsafe)". You are the only user.
  3. Create an OAuth client (type: Desktop app) and download the JSON to the path in
     GOOGLE_OAUTH_CLIENT_JSON (default: google_oauth_client.json in the repo root — gitignored).
"""

from __future__ import annotations

import asyncio
import pathlib
import sys

import asyncpg
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
from inais.config import settings  # noqa: E402

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]


async def store(email: str, refresh_token: str) -> None:
    conn = await asyncpg.connect(settings().supabase_db_url, statement_cache_size=0)
    try:
        await conn.execute(
            "insert into gmail_accounts (email, refresh_token, status, last_history_id)"
            " values ($1, $2, 'active', null)"
            " on conflict (email) do update set refresh_token = excluded.refresh_token,"
            " status = 'active', last_history_id = null",
            email, refresh_token,
        )
    finally:
        await conn.close()


def main() -> None:
    cfg = settings()
    if not cfg.supabase_db_url:
        sys.exit("SUPABASE_DB_URL is not set (put it in .env)")
    expected = sys.argv[1].lower() if len(sys.argv) > 1 else ""

    flow = InstalledAppFlow.from_client_secrets_file(cfg.google_oauth_client_json, SCOPES)
    # access_type=offline + prompt=consent force a refresh token to be issued
    creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")
    if not creds.refresh_token:
        sys.exit("No refresh token returned — remove the app's access at "
                 "https://myaccount.google.com/permissions and retry.")

    svc = build("gmail", "v1", credentials=creds, cache_discovery=False)
    email = svc.users().getProfile(userId="me").execute()["emailAddress"].lower()
    if expected and email != expected:
        sys.exit(f"You authorized {email}, but expected {expected}. Sign in with the right "
                 f"account in the browser window and retry.")

    asyncio.run(store(email, creds.refresh_token))
    print(f"✅ {email} connected. The bot will baseline it on the next Gmail poll.")


if __name__ == "__main__":
    main()
