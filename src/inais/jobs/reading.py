"""Weekly reading digest — the unread links you saved, rounded up.

Saved links are summarised at save time (links.save), so the digest just lists what hasn't
been delivered yet. The scheduler marks them read only AFTER the send succeeds, so a failed
delivery keeps them in the queue for next time.
"""

from __future__ import annotations

import logging

from inais.study import links

log = logging.getLogger(__name__)

DIGEST_LIMIT = 12


async def unread_batch() -> list[dict]:
    """The links this digest would cover — the scheduler marks these read after sending."""
    return await links.unread(limit=DIGEST_LIMIT)


def build(items: list[dict]) -> str | None:
    """Render the digest, or None when there's nothing unread."""
    if not items:
        return None
    return "📚 Reading queue — what you saved and haven't caught up on\n\n" + links.render(items)
