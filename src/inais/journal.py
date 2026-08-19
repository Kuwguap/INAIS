"""Voice journal and mood tracking.

Mood is stored as both a label and a number so it can be trended, but the number is a coarse
ordering, not a measurement — "stressed" scoring -1.0 says stressed sits below okay, nothing
more. Trends are reported over weeks because a single bad evening is not a pattern, and an
assistant that reacts to one entry would be both wrong and annoying.

This is not clinical anything. It surfaces what the user themselves said, and the nightly
reflection may turn repeated themes into facts — that is the whole ambition.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

from inais import db, llm
from inais.config import settings
from inais.timeutil import now_local

log = logging.getLogger(__name__)

# label -> score. Ordering only; see the module docstring.
MOODS: dict[str, float] = {
    "great": 2.0,
    "good": 1.0,
    "okay": 0.0,
    "tired": -0.5,
    "stressed": -1.0,
    "anxious": -1.0,
    "frustrated": -1.0,
    "low": -1.5,
    "sad": -1.5,
}
MOOD_ICONS = {
    "great": "😄", "good": "🙂", "okay": "😐", "tired": "😴", "stressed": "😰",
    "anxious": "😟", "frustrated": "😤", "low": "😔", "sad": "😢",
}
SPARK = "▁▂▃▄▅▆▇"

CLASSIFY_SYSTEM = (
    "Read a personal journal entry and label the speaker's mood. Return JSON "
    '{"mood": "great|good|okay|tired|stressed|anxious|frustrated|low|sad", '
    '"topics": ["at most 3 short topic words"]}. '
    "Judge the speaker's own state, not the mood of things they describe. "
    "Use \"okay\" when the entry is neutral or purely factual."
)


def score_for(mood: str) -> float:
    return MOODS.get(mood, 0.0)


def sparkline(scores: list[float]) -> str:
    """Render daily averages as a compact bar strip over the fixed mood range."""
    if not scores:
        return ""
    lo, hi = min(MOODS.values()), max(MOODS.values())
    span = hi - lo
    out = []
    for s in scores:
        position = 0.0 if span == 0 else (max(lo, min(hi, s)) - lo) / span
        out.append(SPARK[min(len(SPARK) - 1, int(position * (len(SPARK) - 1) + 0.5))])
    return "".join(out)


async def classify(transcript: str) -> tuple[str, list[str]]:
    """Cheap-model mood label. Falls back to neutral rather than guessing."""
    if not settings().openai_api_key or not transcript.strip():
        return "okay", []
    data = await llm.openai_json(
        model=settings().triage_model,
        system=CLASSIFY_SYSTEM,
        user=transcript[:4000],
        purpose="journal-mood",
        max_completion_tokens=120,
    )
    mood = str(data.get("mood", "")).lower()
    if mood not in MOODS:
        mood = "okay"
    topics = [str(t).strip()[:40] for t in (data.get("topics") or [])[:3] if str(t).strip()]
    return mood, topics


async def add_entry(transcript: str, source: str = "voice") -> dict | None:
    transcript = transcript.strip()
    p = db.pool()
    if p is None or not transcript:
        return None
    mood, topics = await classify(transcript)
    try:
        vec = llm.vec_literal(await llm.embed(transcript))
    except Exception:
        log.exception("journal embedding failed")
        vec = None
    row = await p.fetchrow(
        "insert into journal_entries (transcript, embedding, mood, mood_score, topics, source)"
        " values ($1, $2::vector, $3, $4, $5, $6) returning id, created_at",
        transcript, vec, mood, score_for(mood), topics,
        source if source in ("voice", "text") else "text",
    )
    # a fresh entry changes the mood signal — drop the 30-min affect cache so the persona
    # register reflects it on the very next turn, not up to half an hour later
    try:
        from inais.brain import affect
        affect.invalidate()
    except Exception:
        log.exception("affect invalidate after journal entry failed")
    return {"id": row["id"], "mood": mood, "topics": topics, "created_at": row["created_at"]}


def _direction(scores: list[float]) -> str:
    """'improving' | 'declining' | '' from a time-ordered list of mood scores.

    Compares the first half of the window to the second; needs ≥4 points and a real gap so a
    single off day doesn't read as a trend. This is an ORDERING signal, not a measurement."""
    if len(scores) < 4:
        return ""
    half = len(scores) // 2
    first, second = scores[:half], scores[half:]
    delta = sum(second) / len(second) - sum(first) / len(first)
    if abs(delta) < 0.4:
        return ""
    return "improving" if delta > 0 else "declining"


async def trend_direction(days: int = 7) -> str | None:
    """'improving' | 'declining' | None over recent daily mood averages — for the proactive
    check-in, so a well-judged word can land after a hard stretch (never clinical language)."""
    p = db.pool()
    if p is None:
        return None
    since = now_local().date() - timedelta(days=days - 1)
    rows = await p.fetch(
        "select created_at::date as day, avg(mood_score) as score from journal_entries"
        " where created_at::date >= $1 group by day order by day", since)
    return _direction([float(r["score"]) for r in rows]) or None


async def trend(days: int = 14) -> str:
    p = db.pool()
    if p is None:
        return "No database configured — the journal needs one."
    since = now_local().date() - timedelta(days=days - 1)
    rows = await p.fetch(
        "select created_at::date as day, avg(mood_score) as score, count(*) as n"
        " from journal_entries where created_at::date >= $1"
        " group by day order by day", since)
    if not rows:
        return ("📓 No journal entries yet.\n"
                "Use /journal and send a voice note — I'll track how things are going.")

    by_day = {r["day"]: (float(r["score"]), r["n"]) for r in rows}
    days_list = [since + timedelta(days=i) for i in range(days)]
    present = [by_day[d][0] for d in days_list if d in by_day]
    strip = sparkline([by_day.get(d, (0.0, 0))[0] for d in days_list])

    common = await p.fetch(
        "select mood, count(*) as n from journal_entries"
        " where created_at::date >= $1 group by mood order by n desc limit 3", since)
    topics = await p.fetch(
        "select unnest(topics) as topic, count(*) as n from journal_entries"
        " where created_at::date >= $1 group by topic order by n desc limit 5", since)

    average = sum(present) / len(present)
    direction = {"improving": " — trending up",
                 "declining": " — trending down"}.get(_direction(present), "")

    entries = sum(v[1] for v in by_day.values())
    lines = [
        f"📓 Mood, last {days} days ({entries} entr{'y' if entries == 1 else 'ies'})",
        strip,
        f"Average: {average:+.1f}{direction}",
        "Most often: " + ", ".join(
            f"{MOOD_ICONS.get(c['mood'], '')} {c['mood']} ({c['n']})" for c in common),
    ]
    if topics:
        lines.append("Recurring: " + ", ".join(f"{t['topic']} ({t['n']})" for t in topics))
    return "\n".join(lines)


async def recent_for_reflection(limit: int = 20) -> list[dict]:
    """Unreflected entries, for the nightly pattern pass."""
    p = db.pool()
    if p is None:
        return []
    rows = await p.fetch(
        "select id, transcript, mood, topics, created_at from journal_entries"
        " where not reflected order by id desc limit $1", limit)
    return [dict(r) for r in rows]


async def mark_reflected(ids: list[int]) -> None:
    p = db.pool()
    if p is not None and ids:
        await p.execute(
            "update journal_entries set reflected = true where id = any($1::bigint[])", ids)


def render_for_reflection(entries: list[dict]) -> str:
    if not entries:
        return "(none)"
    return "\n".join(
        f"[{e['created_at']:%Y-%m-%d} · {e['mood']}] {e['transcript'][:400]}"
        for e in reversed(entries))


async def counts_since(start: date) -> tuple[int, float | None]:
    """(entries, average score) — used by the weekly review."""
    p = db.pool()
    if p is None:
        return 0, None
    row = await p.fetchrow(
        "select count(*) as n, avg(mood_score) as score from journal_entries"
        " where created_at::date >= $1", start)
    if not row or not row["n"]:
        return 0, None
    return int(row["n"]), float(row["score"])
