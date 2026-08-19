"""Pure logic for read-it-later, the voice journal, and the weekly review."""

from __future__ import annotations

from datetime import date

import pytest

from inais.integrations.fetch import (
    MIN_PARAGRAPH_CHARS,
    extract_urls,
    html_to_text,
    is_link_message,
)
from inais.journal import MOOD_ICONS, MOODS, SPARK, score_for, sparkline
from inais.jobs.weekly import render_stats


# ---------- URL detection ----------

def test_extract_urls_finds_links_and_trims_punctuation():
    assert extract_urls("see https://example.com/a.") == ["https://example.com/a"]
    assert extract_urls("(https://x.dev/post)") == ["https://x.dev/post"]
    assert extract_urls("no links here") == []


def test_extract_urls_handles_multiple():
    assert len(extract_urls("https://a.com and https://b.com")) == 2


def test_a_bare_link_is_a_save_request():
    assert is_link_message("https://example.com/article")
    assert is_link_message("  https://example.com/article  ")
    assert is_link_message("https://example.com/article read later")


@pytest.mark.parametrize("text", [
    # long question
    "What do you think about this article, and does it change the approach "
    "we discussed for the scheduler? https://example.com/article",
    # SHORT question — the case that matters: filing this instead of answering is the
    # worse failure, and a naive length threshold gets it wrong
    "should I read this https://example.com/x before the exam on Friday",
    "is https://example.com/x any good?",
    "thoughts on https://example.com/x?",
])
def test_a_link_inside_a_sentence_stays_a_conversation(text):
    assert not is_link_message(text)


def test_a_question_mark_always_means_a_question():
    assert not is_link_message("https://example.com/x ?")


def test_two_links_are_not_a_single_save():
    assert not is_link_message("https://a.com https://b.com")


def test_plain_text_is_never_a_link_message():
    assert not is_link_message("remind me to read more")
    assert not is_link_message("")


# ---------- HTML extraction ----------

SAMPLE = """
<html><head><title>Understanding Vector Search</title></head>
<body>
  <nav>Home About Contact Subscribe</nav>
  <script>var tracking = 1;</script>
  <article>
    <h1>Understanding Vector Search</h1>
    <p>Vector search finds items by meaning rather than exact keywords, which is why it
       handles paraphrases that keyword search misses entirely.</p>
    <p>The trade-off is that pure vector search misses exact tokens such as names and error
       codes, so hybrid approaches combine it with full-text ranking.</p>
  </article>
  <footer>Copyright 2026</footer>
</body></html>
"""


def test_html_to_text_pulls_the_title():
    title, _ = html_to_text(SAMPLE)
    assert title == "Understanding Vector Search"


def test_html_to_text_keeps_article_paragraphs():
    _, text = html_to_text(SAMPLE)
    assert "finds items by meaning" in text
    assert "hybrid approaches" in text


def test_html_to_text_drops_scripts_and_navigation():
    _, text = html_to_text(SAMPLE)
    assert "tracking" not in text
    assert "Subscribe" not in text          # nav is furniture
    assert "Copyright" not in text          # so is the footer


def test_html_to_text_drops_short_fragments():
    _, text = html_to_text("<p>Menu</p><p>OK</p>")
    assert text == ""


def test_html_to_text_deduplicates_repeated_boilerplate():
    para = "<p>" + ("This exact sentence repeats across the page template. " * 2) + "</p>"
    _, text = html_to_text(para * 3)
    assert text.count("This exact sentence repeats") == 2   # one surviving block, phrase twice


def test_html_to_text_survives_broken_markup():
    title, text = html_to_text("<p>unclosed paragraph with enough words to be kept as content")
    assert title == ""
    assert "unclosed paragraph" in text


def test_paragraph_threshold_is_a_sane_size():
    assert 20 <= MIN_PARAGRAPH_CHARS <= 80


# ---------- mood ----------

def test_every_mood_has_a_score_and_an_icon():
    for mood in MOODS:
        assert isinstance(score_for(mood), float)
        assert mood in MOOD_ICONS


def test_mood_ordering_is_sane():
    assert score_for("great") > score_for("good") > score_for("okay")
    assert score_for("okay") > score_for("stressed") > score_for("low")


def test_unknown_mood_scores_neutral():
    assert score_for("ecstatic") == 0.0
    assert score_for("") == 0.0


def test_sparkline_maps_the_range_to_bars():
    line = sparkline([min(MOODS.values()), 0.0, max(MOODS.values())])
    assert len(line) == 3
    assert line[0] == SPARK[0]
    assert line[-1] == SPARK[-1]
    assert all(c in SPARK for c in line)


def test_sparkline_handles_empty_and_out_of_range():
    assert sparkline([]) == ""
    assert len(sparkline([-99.0, 99.0])) == 2   # clamped, not crashed


# ---------- weekly review ----------

def _stats(**over) -> dict:
    base = {
        "start": date(2026, 8, 12), "end": date(2026, 8, 18),
        "tasks": {"done": 7, "overdue": 2, "open": 5},
        "pomodoro": {"sessions": 9, "minutes": 225, "days": 4},
        "study_plan": {"total": 7, "done": 5},
        "spend": [{"model": "claude-sonnet-5", "cost": 3.2}], "spend_total": 3.2,
        "learned": [{"topic": "pgvector HNSW tuning", "summary": "..."}],
        "cards_reviewed": 12, "drills": 3,
        "journal_entries": 4, "mood_avg": 0.5,
    }
    base.update(over)
    return base


def test_weekly_review_reports_every_section():
    text = render_stats(_stats())
    for fragment in ("7 completed", "2 overdue", "9 sessions", "5/7 days",
                     "12 cards", "4 entries", "$3.20", "pgvector HNSW tuning"):
        assert fragment in text


def test_weekly_review_states_a_quiet_week_plainly():
    text = render_stats(_stats(pomodoro={"sessions": 0, "minutes": 0, "days": 0}))
    assert "no completed sessions" in text


def test_weekly_review_omits_sections_with_no_data():
    text = render_stats(_stats(study_plan={"total": 0, "done": 0}, journal_entries=0,
                               cards_reviewed=0, drills=0))
    assert "Study plan" not in text
    assert "Journal" not in text
    assert "Review:" not in text


def test_weekly_review_hides_overdue_when_there_are_none():
    assert "overdue" not in render_stats(_stats(tasks={"done": 3, "overdue": 0, "open": 1}))


def test_weekly_review_handles_empty_stats():
    assert render_stats({}) == ""


@pytest.mark.parametrize("pct_done,expected", [(0, "0%"), (7, "100%")])
def test_study_plan_adherence_percentage(pct_done, expected):
    assert expected in render_stats(_stats(study_plan={"total": 7, "done": pct_done}))


# ---------- SSRF guard ----------

@pytest.mark.parametrize("ip", [
    "127.0.0.1", "10.0.0.5", "172.16.3.1", "192.168.1.1",   # loopback + private
    "169.254.169.254",                                       # cloud metadata endpoint
    "0.0.0.0", "224.0.0.1", "::1", "fe80::1", "fd00::1",
])
def test_forbidden_addresses_are_blocked(ip):
    from inais.integrations.fetch import ip_is_forbidden

    assert ip_is_forbidden(ip)


@pytest.mark.parametrize("ip", ["93.184.216.34", "142.250.74.110", "2606:4700::6810:84e5"])
def test_public_addresses_pass(ip):
    from inais.integrations.fetch import ip_is_forbidden

    assert not ip_is_forbidden(ip)


def test_garbage_addresses_are_treated_as_forbidden():
    from inais.integrations.fetch import ip_is_forbidden

    assert ip_is_forbidden("not-an-ip")


def test_redirect_cap_exists():
    from inais.integrations.fetch import MAX_REDIRECTS

    assert 1 <= MAX_REDIRECTS <= 10
