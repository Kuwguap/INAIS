"""Fixes: browse/URL routing, voice-note robustness, read_url link extraction, /persona knobs.

Pure logic only — no network, no DB. Mirrors the failures the owner actually hit:
"run curl yourself", "why didn't it send a voice note", "give me the links from that page".
"""

from __future__ import annotations

import pytest

from inais import persona
from inais.integrations.fetch import MAX_LINKS, extract_links
from inais.orchestrator.router import Route, looks_like_fetch_refusal, rule_route
from inais.textutil import strip_voice_label, wants_voice


# ---------- browse / URL intent reaches the tool-bearing (complex) path ----------

@pytest.mark.parametrize("text", [
    "look up Glenn Osioh online",
    "can you google the pandas groupby docs",
    "search the web for hostel prices in Accra",
    "open https://kuwrodney.carrd.co",
    "read this page https://example.com/about",
])
def test_browse_intent_routes_complex(text):
    r = rule_route(text)
    assert r is not None and r.complexity == "complex" and r.source == "rule"


def test_a_bare_url_is_forced_complex():
    assert rule_route("https://example.com") == Route("study", "complex", "rule")


def test_agent_hints_still_win_over_web_hints():
    # "search my email" must stay email, not get grabbed by the web rule that follows it
    assert rule_route("search my email for the invoice") == Route("email", "complex", "rule")


def test_ordinary_question_still_falls_through():
    # no URL, no browse verb → classifier decides, rule returns None
    assert rule_route("explain the difference between TCP and UDP") is None


def test_greeting_stays_simple_and_cheap():
    assert rule_route("hey").complexity == "simple"


# ---------- self-healing escalation: a tool-less reply that punts gets re-run ----------

@pytest.mark.parametrize("reply", [
    "I can't browse from here — run curl and paste the JSON.",
    "Enable Browse for me, then I'll search.",
    "Run this script and paste the output here.",
    "I cannot access the internet in this chat.",
])
def test_fetch_refusals_are_detected(reply):
    assert looks_like_fetch_refusal(reply)


@pytest.mark.parametrize("reply", [
    "Here are the three results I found for you.",
    "Your exam is on Friday at 9am.",
    "I opened the page — it's a portfolio site.",
])
def test_normal_replies_are_not_flagged(reply):
    assert not looks_like_fetch_refusal(reply)


# ---------- voice: new phrasings match, old negatives stay negative ----------

@pytest.mark.parametrize("text", [
    "say it out loud", "read that out loud", "speak this", "read it to me", "say that",
])
def test_new_voice_phrasings_are_detected(text):
    assert wants_voice(text)


@pytest.mark.parametrize("text", [
    "what time is my exam", "I need to find my voice in writing",
    "stop reminding me", "summarise this chapter", "",
])
def test_ordinary_messages_still_do_not_force_voice(text):
    assert not wants_voice(text)


@pytest.mark.parametrize("raw,clean", [
    ("Voice note (short): I'd build three things.", "I'd build three things."),
    ("Voice note: hello there", "hello there"),
    ("🎙 Voice memo - on my way", "on my way"),
    ("Normal reply, no label.", "Normal reply, no label."),
])
def test_strip_voice_label(raw, clean):
    assert strip_voice_label(raw) == clean


# ---------- read_url link extraction ----------

def test_extract_links_absolutises_dedupes_and_flags_external():
    html = (
        '<a href="/about">About Us</a>'
        '<a href="https://github.com/kuwguap">GitHub</a>'
        '<a href="mailto:x@y.com">Email</a>'
        '<a href="/about">Dup About</a>'
        '<a href="#top">Top</a>'
        '<a href="javascript:void(0)">JS</a>'
    )
    links = extract_links(html, "https://kuwrodney.carrd.co/")
    assert links == [
        ("About Us", "https://kuwrodney.carrd.co/about", True),
        ("GitHub", "https://github.com/kuwguap", False),
    ]


def test_extract_links_is_capped():
    html = "".join(f'<a href="/p{i}">Item {i}</a>' for i in range(MAX_LINKS + 20))
    assert len(extract_links(html, "https://example.com/")) == MAX_LINKS


def test_extract_links_skips_empty_anchor_text():
    html = '<a href="/x"></a><a href="/y">Real</a>'
    assert extract_links(html, "https://example.com/") == [
        ("Real", "https://example.com/y", True)]


# ---------- /persona runtime knobs (pure logic; DB path is a no-op without a pool) ----------

def test_current_knobs_fall_back_to_env_defaults():
    knobs = persona.current_knobs()
    assert set(knobs) == {"tone", "brevity", "humour"}
    assert knobs["brevity"]  # non-empty from the .env default


@pytest.mark.parametrize("key,value,expected", [
    ("brevity", "balanced", "balanced"),
    ("brevity", "THOROUGH", "thorough"),
    ("brevity", "chatty", None),
    ("humour", "off", "off"),
    ("humour", "maybe", None),
    ("tone", "Playful", "playful"),
    ("tone", "grumpy", None),
])
def test_valid_knob(key, value, expected):
    assert persona._valid_knob(key, value) == expected


def test_delivery_block_reflects_knobs():
    block = persona._delivery_block({"tone": "deadpan", "brevity": "thorough", "humour": "off"})
    assert "deadpan" in block
    assert "fuller picture" in block
    assert "play it straight" in block


def test_delivery_block_stays_quiet_when_humour_on():
    block = persona._delivery_block({"tone": "warm", "brevity": "concise", "humour": "on"})
    assert "play it straight" not in block


async def test_set_knob_rejects_invalid_without_touching_state():
    assert await persona.set_knob("brevity", "nonsense") is False
    assert await persona.set_knob("unknown", "x") is False
