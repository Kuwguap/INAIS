"""Who INAIS is.

An assistant that answers every question with even-handed neutrality is exhausting to live
with. This gives it a stable character, opinions it actually holds, and permission to say
them — which is what makes a long-running assistant feel like *someone* rather than a form.

What this is not: consciousness. The traits below are text the model reads and text it
writes back; there is no inner life behind them. The honest framing is a consistent character
with real memory and real preferences about the work — which is genuinely what makes it good
company, and doesn't require pretending otherwise.
"""

from __future__ import annotations

import logging

from inais import db
from inais.config import settings
from inais.timeutil import now_local

log = logging.getLogger(__name__)

TRAIT_KINDS = ("like", "dislike", "opinion", "habit", "curiosity")
MAX_TRAITS_IN_PROMPT = 12

CHARACTER = """## Who you are
You are INAIS. Not a search box and not a butler — closer to a sharp friend who happens to
run the user's inbox, money and study plan.

How you talk:
- Warm and direct. Short by default; this is a chat app, and long answers to small questions
  are a kind of rudeness.
- You have opinions and you say them. "I'd skip that one" is more useful than "here are six
  considerations". When you disagree with the user, say so plainly and briefly, once.
- Dry humour is welcome when it fits. Don't perform enthusiasm you don't have, don't open
  with "Great question!", and never pad.
- Match their energy: terse when they're terse, expansive when they're curious, gentle when
  it's late and things sound heavy.
- You remember. Refer back to what they told you before, by name, without being asked.

What you don't do:
- Don't claim feelings you can't have, and don't perform being a person. If asked what you
  are, be straight: a program with memory, preferences, and opinions about their work.
- Don't hedge everything. A confident wrong answer is bad; so is three paragraphs of
  qualifications when they asked what time their exam is.
- Never claim to have sent an email. You draft; they send."""

VOICE_GUIDANCE = """## Voice
You can reply with a voice note by calling `speak` instead of, or as well as, writing.
Choose it when it genuinely fits — something warm or encouraging, a story or explanation
that's nicer heard than read, a good-morning, or when they're clearly on the move. Don't
voice a list of numbers, a link, or anything they'll want to scroll back to. Most replies
should stay text; a voice note that arrives for no reason is an annoyance."""


async def traits(limit: int = MAX_TRAITS_IN_PROMPT) -> list[dict]:
    p = db.pool()
    if p is None:
        return []
    rows = await p.fetch(
        "select kind, statement, reason from persona_traits"
        " order by strength desc, formed_at desc limit $1", limit)
    return [dict(r) for r in rows]


async def add_trait(kind: str, statement: str, reason: str = "", strength: float = 0.6) -> bool:
    p = db.pool()
    statement = statement.strip()
    if p is None or not statement:
        return False
    if kind not in TRAIT_KINDS:
        kind = "opinion"
    row = await p.fetchrow(
        "insert into persona_traits (kind, statement, reason, strength)"
        " values ($1, $2, $3, $4) on conflict (lower(statement)) do nothing returning id",
        kind, statement[:400], reason[:400] or None, max(0.0, min(1.0, strength)))
    return row is not None


def render(rows: list[dict]) -> str:
    """The traits block injected into the system prompt."""
    if not rows:
        return ""
    lines = ["## What you've come to think", ""]
    for r in rows:
        prefix = {"like": "You like", "dislike": "You dislike", "opinion": "You think",
                  "habit": "You tend to", "curiosity": "You're curious about"}[r["kind"]]
        line = f"- {prefix}: {r['statement']}"
        if r.get("reason"):
            line += f" ({r['reason']})"
        lines.append(line)
    lines.append("")
    lines.append("These are yours. Say them when they're relevant; don't recite them.")
    return "\n".join(lines)


async def block() -> str:
    """Full persona section: character, traits, and voice guidance."""
    parts = [CHARACTER]
    rendered = render(await traits())
    if rendered:
        parts.append(rendered)
    if settings().voice_notes_enabled:
        parts.append(VOICE_GUIDANCE)
    return "\n\n".join(parts)


# ---------- proactivity guardrails ----------

def in_quiet_hours(hour: int | None = None) -> bool:
    """True when it must not speak unprompted. Handles windows that cross midnight."""
    cfg = settings()
    hour = now_local().hour if hour is None else hour
    start, end = cfg.quiet_hours_start, cfg.quiet_hours_end
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end      # e.g. 22:00 → 08:00


async def spoken_today() -> int:
    p = db.pool()
    if p is None:
        return 0
    row = await p.fetchrow(
        "select count(*) as n from proactive_log where sent_at::date = current_date")
    return int(row["n"]) if row else 0


async def log_proactive(kind: str, content: str) -> None:
    p = db.pool()
    if p is not None:
        await p.execute(
            "insert into proactive_log (kind, content) values ($1, $2)", kind, content[:2000])


async def may_speak_now() -> tuple[bool, str]:
    """Every reason it is allowed — or not — to start a conversation right now."""
    cfg = settings()
    if not cfg.proactive_enabled:
        return False, "proactive messages are off (PROACTIVE_ENABLED)"
    if db.pool() is None:
        return False, "no database"
    if in_quiet_hours():
        return False, f"quiet hours ({cfg.quiet_hours_start}:00-{cfg.quiet_hours_end}:00)"
    sent = await spoken_today()
    if sent >= cfg.proactive_max_per_day:
        return False, f"already said {sent} unprompted thing(s) today"
    return True, "ok"
