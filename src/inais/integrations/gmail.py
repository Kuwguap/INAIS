"""Gmail REST API integration (scope: gmail.modify only — no delete).

googleapiclient is synchronous, so every public function here is an async wrapper
around asyncio.to_thread. Raises GmailAuthError on invalid_grant (e.g. the user
changed their Google password) so callers can mark the account needs_reauth.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from email.message import EmailMessage
from email.utils import parseaddr

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from inais import db
from inais.config import settings

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]
TOKEN_URI = "https://oauth2.googleapis.com/token"


class GmailAuthError(Exception):
    """Refresh token is dead — the account must be re-authorized."""


class HistoryGoneError(Exception):
    """startHistoryId too old (404) — caller should re-baseline the account."""


def _client_conf() -> tuple[str, str]:
    with open(settings().google_oauth_client_json, encoding="utf-8") as f:
        data = json.load(f)
    key = "installed" if "installed" in data else "web"
    return data[key]["client_id"], data[key]["client_secret"]


def _service(refresh_token: str):
    client_id, client_secret = _client_conf()
    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri=TOKEN_URI,
        client_id=client_id,
        client_secret=client_secret,
        scopes=SCOPES,
    )
    try:
        creds.refresh(Request())
    except RefreshError as e:
        raise GmailAuthError(str(e)) from e
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


# ---------- accounts (DB) ----------

async def list_accounts(only_active: bool = True) -> list[dict]:
    p = db.pool()
    if p is None:
        return []
    q = "select email, refresh_token, last_history_id, status from gmail_accounts"
    if only_active:
        q += " where status = 'active'"
    return [dict(r) for r in await p.fetch(q)]


async def mark_needs_reauth(email: str) -> None:
    p = db.pool()
    if p is not None:
        await p.execute("update gmail_accounts set status = 'needs_reauth' where email = $1", email)


async def set_history_id(email: str, history_id: str) -> None:
    p = db.pool()
    if p is not None:
        await p.execute("update gmail_accounts set last_history_id = $1 where email = $2",
                        history_id, email)


# ---------- read path ----------

def _sync_current_history_id(refresh_token: str) -> str:
    svc = _service(refresh_token)
    profile = svc.users().getProfile(userId="me").execute()
    return str(profile["historyId"])


async def current_history_id(refresh_token: str) -> str:
    return await asyncio.to_thread(_sync_current_history_id, refresh_token)


def _sync_fetch_history(refresh_token: str, start_history_id: str) -> tuple[list[str], str]:
    svc = _service(refresh_token)
    msg_ids: list[str] = []
    new_history_id = start_history_id
    page_token = None
    try:
        while True:
            req = svc.users().history().list(
                userId="me", startHistoryId=start_history_id,
                historyTypes=["messageAdded"], labelId="INBOX", pageToken=page_token,
            )
            resp = req.execute()
            new_history_id = str(resp.get("historyId", new_history_id))
            for h in resp.get("history", []):
                for added in h.get("messagesAdded", []):
                    msg_ids.append(added["message"]["id"])
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
    except HttpError as e:
        if e.resp.status == 404:
            raise HistoryGoneError(start_history_id) from e
        raise
    # dedupe, keep order
    return list(dict.fromkeys(msg_ids)), new_history_id


async def fetch_history(refresh_token: str, start_history_id: str) -> tuple[list[str], str]:
    """New INBOX message ids since start_history_id, plus the new history id."""
    return await asyncio.to_thread(_sync_fetch_history, refresh_token, start_history_id)


def _sync_get_meta(refresh_token: str, msg_id: str) -> dict:
    svc = _service(refresh_token)
    m = svc.users().messages().get(
        userId="me", id=msg_id, format="metadata",
        metadataHeaders=["From", "Subject", "Date", "Message-ID"],
    ).execute()
    headers = {h["name"].lower(): h["value"] for h in m.get("payload", {}).get("headers", [])}
    return {
        "id": m["id"],
        "thread_id": m.get("threadId", ""),
        "labels": m.get("labelIds", []),
        "snippet": m.get("snippet", ""),
        "from": headers.get("from", ""),
        "subject": headers.get("subject", "(no subject)"),
        "date": headers.get("date", ""),
        "message_id_header": headers.get("message-id", ""),
    }


async def get_meta(refresh_token: str, msg_id: str) -> dict:
    return await asyncio.to_thread(_sync_get_meta, refresh_token, msg_id)


def _walk_for_text(payload: dict) -> str:
    if payload.get("mimeType") == "text/plain" and payload.get("body", {}).get("data"):
        return base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", "replace")
    for part in payload.get("parts", []) or []:
        text = _walk_for_text(part)
        if text:
            return text
    # fall back to text/html stripped of tags-ish (rough but fine for LLM input)
    if payload.get("mimeType") == "text/html" and payload.get("body", {}).get("data"):
        import re
        html = base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", "replace")
        return re.sub(r"<[^>]+>", " ", html)
    return ""


def _sync_get_body(refresh_token: str, msg_id: str) -> str:
    svc = _service(refresh_token)
    m = svc.users().messages().get(userId="me", id=msg_id, format="full").execute()
    return _walk_for_text(m.get("payload", {}))[:12000]


async def get_body(refresh_token: str, msg_id: str) -> str:
    return await asyncio.to_thread(_sync_get_body, refresh_token, msg_id)


# ---------- draft path (send happens ONLY from the human approval handler) ----------

def build_mime(from_addr: str, to: str, subject: str, body: str,
               in_reply_to: str = "") -> str:
    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = to
    msg["Subject"] = subject
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
    msg.set_content(body)
    return base64.urlsafe_b64encode(msg.as_bytes()).decode()


def _sync_create_draft(refresh_token: str, raw: str, thread_id: str = "") -> str:
    svc = _service(refresh_token)
    message: dict = {"raw": raw}
    if thread_id:
        message["threadId"] = thread_id
    draft = svc.users().drafts().create(userId="me", body={"message": message}).execute()
    return draft["id"]


async def create_draft(refresh_token: str, raw: str, thread_id: str = "") -> str:
    return await asyncio.to_thread(_sync_create_draft, refresh_token, raw, thread_id)


def _sync_update_draft(refresh_token: str, draft_id: str, raw: str, thread_id: str = "") -> None:
    svc = _service(refresh_token)
    message: dict = {"raw": raw}
    if thread_id:
        message["threadId"] = thread_id
    svc.users().drafts().update(userId="me", id=draft_id, body={"message": message}).execute()


async def update_draft(refresh_token: str, draft_id: str, raw: str, thread_id: str = "") -> None:
    await asyncio.to_thread(_sync_update_draft, refresh_token, draft_id, raw, thread_id)


def _sync_send_draft(refresh_token: str, draft_id: str) -> None:
    svc = _service(refresh_token)
    svc.users().drafts().send(userId="me", body={"id": draft_id}).execute()


async def send_draft(refresh_token: str, draft_id: str) -> None:
    await asyncio.to_thread(_sync_send_draft, refresh_token, draft_id)


def sender_domain(from_header: str) -> str:
    addr = parseaddr(from_header)[1]
    return addr.rsplit("@", 1)[-1].lower() if "@" in addr else ""
