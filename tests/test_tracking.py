"""Pure logic for the application and expense trackers (no network, no database).

The classifier reads other people's email templates, so these tests are mostly about what
happens when the model returns something wrong, partial or hostile.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from inais.agents.applications import STATUS_RANK, should_advance
from inais.agents.email_agent import looks_trackable, normalise_verdict
from inais.agents.expenses import EXPENSE_CATEGORIES, month_bounds, parse_amount, parse_currency
from inais.bot import keyboards


# ---------- amount parsing ----------

@pytest.mark.parametrize(("raw", "expected"), [
    (12.34, "12.34"),
    (12, "12.00"),
    ("12.34", "12.34"),
    ("$1,234.56", "1234.56"),
    ("USD 99.00", "99.00"),
    ("1 234,56", "1234.56"),      # European grouping
    ("1234,56", "1234.56"),
    ("1,234", "1234.00"),         # thousands separator, no decimals
    (Decimal("5.5"), "5.50"),
])
def test_parse_amount_accepts_real_world_formats(raw, expected):
    assert parse_amount(raw) == Decimal(expected)


@pytest.mark.parametrize("raw", [None, "", "free", "N/A", 0, -5, "-12.00", True, False, {}])
def test_parse_amount_rejects_non_amounts(raw):
    """A bad parse would silently distort the month's spending — reject instead."""
    assert parse_amount(raw) is None


def test_parse_amount_rejects_absurd_values():
    assert parse_amount("999999999") is None


def test_parse_currency_from_codes_symbols_and_fallback():
    assert parse_currency("usd") == "USD"
    assert parse_currency("$") == "USD"
    assert parse_currency("₵") == "GHS"
    assert parse_currency("€12.00") == "EUR"
    assert parse_currency(None, fallback="GHS") == "GHS"
    assert parse_currency("", fallback="GBP") == "GBP"


# ---------- verdict normalisation (one structured triage call) ----------

def test_verdict_defaults_are_safe_when_the_model_returns_junk():
    v = normalise_verdict({"importance": "URGENT!!", "category": "banana"})
    assert v["importance"] == "normal"
    assert v["category"] == "other"
    assert v["application"] is None and v["expense"] is None


def test_verdict_extracts_a_full_application():
    v = normalise_verdict({
        "importance": "high", "category": "application",
        "application": {"org": "Acme Corp", "role": "Backend Intern", "kind": "internship",
                        "status": "interview", "deadline_iso": "2026-09-01"},
    })
    assert v["category"] == "application"
    assert v["application"]["org"] == "Acme Corp"
    assert v["application"]["status"] == "interview"
    assert v["application"]["kind"] == "internship"


def test_application_without_an_org_is_dropped():
    """An application row with no employer is useless and would pollute the pipeline."""
    v = normalise_verdict({"category": "application",
                           "application": {"role": "Intern", "status": "applied"}})
    assert v["application"] is None
    assert v["category"] == "other"


def test_application_with_unknown_status_falls_back_to_applied():
    v = normalise_verdict({"category": "application",
                           "application": {"org": "Acme", "status": "ghosted"}})
    assert v["application"]["status"] == "applied"
    assert v["application"]["kind"] == "job"


def test_expense_requires_a_merchant_and_a_positive_amount():
    assert normalise_verdict({"category": "expense",
                              "expense": {"merchant": "Spotify"}})["expense"] is None
    assert normalise_verdict({"category": "expense",
                              "expense": {"amount": 9.99}})["expense"] is None
    assert normalise_verdict({"category": "expense",
                              "expense": {"merchant": "X", "amount": "free"}})["expense"] is None


def test_expense_is_extracted_and_categorised():
    v = normalise_verdict({"category": "expense", "expense": {
        "merchant": "Spotify", "amount": "$9.99", "currency": "$", "category": "subscription"}})
    assert v["expense"]["amount"] == Decimal("9.99")
    assert v["expense"]["currency"] == "USD"
    assert v["expense"]["category"] == "subscription"


def test_unknown_expense_category_falls_back():
    v = normalise_verdict({"category": "expense", "expense": {
        "merchant": "X", "amount": 5, "category": "vibes"}})
    assert v["expense"]["category"] == "other"
    assert v["expense"]["category"] in EXPENSE_CATEGORIES


def test_payload_without_the_matching_category_still_counts():
    """The model sometimes fills the object but mislabels the category."""
    v = normalise_verdict({"category": "other",
                           "expense": {"merchant": "Uber", "amount": 12}})
    assert v["category"] == "expense"


def test_learned_score_can_raise_but_not_invent_importance():
    assert normalise_verdict({"importance": "low"}, learned=0.95)["importance"] == "normal"
    assert normalise_verdict({"importance": "high"}, learned=0.1)["importance"] == "normal"
    assert "net 0.95" in normalise_verdict({"importance": "low"}, learned=0.95)["reason"]


def test_needs_reply_is_coerced_to_bool():
    assert normalise_verdict({"needs_reply": "yes"})["needs_reply"] is True
    assert normalise_verdict({})["needs_reply"] is False


# ---------- trackable prefilter ----------

@pytest.mark.parametrize("subject", [
    "Your receipt from Spotify",
    "Payment confirmation",
    "Thank you for applying to Acme",
    "Invitation to interview",
    "We regret to inform you",
    "Your subscription renewal",
])
def test_trackable_subjects_buy_a_triage_call(subject):
    """These rarely carry Gmail's IMPORTANT label, so the cheap path would skip them."""
    assert looks_trackable({"subject": subject, "snippet": ""})


@pytest.mark.parametrize("subject", ["Weekly newsletter", "Re: lunch tomorrow", "Standup notes"])
def test_ordinary_mail_is_not_trackable(subject):
    assert not looks_trackable({"subject": subject, "snippet": "nothing to see"})


# ---------- application pipeline ----------

def test_pipeline_moves_forward_only():
    assert should_advance("applied", "interview")
    assert should_advance("assessment", "offer")
    assert not should_advance("interview", "applied")   # a stray footer must not regress it
    assert not should_advance("offer", "assessment")


def test_terminal_states_always_win():
    assert should_advance("interview", "rejected")
    assert should_advance("applied", "withdrawn")


def test_terminal_states_are_not_reopened_by_stale_mail():
    assert not should_advance("rejected", "interview")
    assert not should_advance("withdrawn", "applied")


def test_same_status_is_not_an_advance():
    assert not should_advance("applied", "applied")


def test_unknown_status_is_ignored():
    assert not should_advance("applied", "ghosted")


def test_status_rank_covers_every_non_terminal_stage():
    from inais.agents.applications import APPLICATION_STATUSES, TERMINAL

    assert set(STATUS_RANK) | TERMINAL == set(APPLICATION_STATUSES)


# ---------- month bucketing ----------

def test_month_bounds_for_the_current_month():
    start, end, label = month_bounds(0, today=date(2026, 8, 18))
    assert (start, end) == (date(2026, 8, 1), date(2026, 9, 1))
    assert label == "August 2026"


def test_month_bounds_walks_back_across_a_year_boundary():
    start, end, label = month_bounds(8, today=date(2026, 8, 18))
    assert (start, end) == (date(2025, 12, 1), date(2026, 1, 1))
    assert label == "December 2025"


def test_month_bounds_handles_december_end():
    start, end, _ = month_bounds(0, today=date(2026, 12, 5))
    assert (start, end) == (date(2026, 12, 1), date(2027, 1, 1))


# ---------- keyboards ----------

def test_tracking_callback_data_fits_telegrams_limit():
    kbs = [
        keyboards.application_kb(999999, deadline=True),
        keyboards.application_status_kb(999999),
        keyboards.expense_kb(999999),
        keyboards.expense_category_kb(999999),
        keyboards.spend_kb(3),
        keyboards.apps_list_kb([{"id": 12, "org": "A very long organisation name here"}]),
    ]
    for kb in kbs:
        for row in kb.inline_keyboard:
            for button in row:
                assert len(button.callback_data.encode()) <= 64


def test_application_keyboard_only_offers_a_task_when_there_is_a_deadline():
    with_deadline = keyboards.application_kb(1, deadline=True)
    without = keyboards.application_kb(1, deadline=False)
    assert any("apptask" in b.callback_data for row in with_deadline.inline_keyboard for b in row)
    assert not any("apptask" in b.callback_data for row in without.inline_keyboard for b in row)


def test_status_keyboard_offers_every_stage():
    from inais.agents.applications import APPLICATION_STATUSES

    data = [b.callback_data for row in keyboards.application_status_kb(7).inline_keyboard
            for b in row]
    for status in APPLICATION_STATUSES:
        assert f"appst:7:{status}" in data


def test_spend_keyboard_hides_later_on_the_current_month():
    current = [b.callback_data for row in keyboards.spend_kb(0).inline_keyboard for b in row]
    older = [b.callback_data for row in keyboards.spend_kb(2).inline_keyboard for b in row]
    assert current == ["spend:1"]
    assert "spend:1" in older and "spend:3" in older
