"""Harvesting real behavioural signals into neural-network training examples.

Every label here comes from something the user actually did — not from an LLM's opinion:
  • sent a message about a topic            → interest +
  • tapped 👍 / asked for more on a note    → interest +
  • tapped 👎 on a note                     → interest −
  • tapped "Draft reply" on an email        → email_importance +
  • tapped "Ignore" on an email             → email_importance −
Silence is not used as a label: absence of a tap is genuinely ambiguous.
"""

from __future__ import annotations

import logging

from inais import db
from inais.brain import nn
from inais.config import settings

log = logging.getLogger(__name__)

MIN_TEXT_CHARS = 12


async def record_interest(text: str, positive: bool, note: str = "", weight: float = 1.0) -> None:
    if not settings().nn_enabled or len(text.strip()) < MIN_TEXT_CHARS:
        return
    try:
        await nn.add_example("interest", text, 1.0 if positive else 0.0, note, weight)
    except Exception:
        log.exception("failed to record interest signal")


async def record_email_importance(subject: str, sender: str, snippet: str,
                                  important: bool) -> None:
    if not settings().nn_enabled:
        return
    text = f"From: {sender}\nSubject: {subject}\n{snippet}".strip()
    if len(text) < MIN_TEXT_CHARS:
        return
    try:
        await nn.add_example("email_importance", text, 1.0 if important else 0.0,
                             note=f"user {'acted on' if important else 'ignored'} mail")
    except Exception:
        log.exception("failed to record email importance signal")


async def record_email_signal_from_event(event_id: int, important: bool) -> None:
    """Look the email event up and label it from what the user just tapped."""
    p = db.pool()
    if p is None:
        return
    row = await p.fetchrow(
        "select from_ as sender, subject, snippet from email_events where id = $1", event_id)
    if row is None:
        return
    await record_email_importance(
        row["subject"] or "", row["sender"] or "", row["snippet"] or "", important)


async def record_knowledge_feedback(knowledge_id: int, positive: bool) -> str:
    """👍/👎 on something the assistant taught itself: trains interest, adjusts the note."""
    p = db.pool()
    if p is None:
        return "No database configured."
    row = await p.fetchrow(
        "update knowledge set engagement = engagement + $1 where id = $2"
        " returning topic, summary", 1 if positive else -1, knowledge_id)
    if row is None:
        return "That note is gone."
    await record_interest(
        f"{row['topic']}. {row['summary']}", positive,
        note="explicit feedback on self-learned note", weight=2.0,  # explicit beats implicit
    )
    return row["topic"]


async def user_message_signal(text: str) -> None:
    """Whatever the user brings up unprompted is, by definition, something they care about."""
    stripped = text.strip()
    if len(stripped) < 25 or stripped.startswith("/"):
        return  # too short to carry topical signal
    await record_interest(stripped, True, note="user raised this topic")
