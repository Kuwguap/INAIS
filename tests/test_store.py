"""ogoffcl store integration — pure render/format logic, callback-data budget, and the
security shape of the inbound push route. No network, no DB.
"""

from __future__ import annotations

import inspect

import pytest

from inais.bot import keyboards
from inais.integrations import ogoffcl

UUID = "123e4567-e89b-12d3-a456-426614174000"


# ---------- status model ----------

def test_status_model_is_complete():
    assert len(ogoffcl.STATUSES) == 8
    assert ogoffcl.STATUSES[0] == "pending" and ogoffcl.STATUSES[-1] == "refunded"
    for s in ogoffcl.STATUSES:
        assert s in ogoffcl.STATUS_LABELS and s in ogoffcl.STATUS_ICONS


# ---------- inbound push formatting ----------

def test_format_waitlist_event():
    text, order_id = ogoffcl.format_event({"event": "waitlist.joined", "email": "a@b.com", "count": 7})
    assert "a@b.com" in text and "7" in text
    assert order_id is None


def test_format_paid_order_event_returns_order_id_for_buttons():
    text, order_id = ogoffcl.format_event({
        "event": "order.paid", "order_number": "OG-9", "total_amount": 120,
        "customer_name": "Ama", "id": "u1", "item_count": 2})
    assert "OG-9" in text and "GH₵120" in text and "Ama" in text
    assert order_id == "u1"


def test_format_unknown_event_is_safe():
    text, order_id = ogoffcl.format_event({"event": "weird.thing"})
    assert "weird.thing" in text and order_id is None


def test_event_key_is_stable():
    assert ogoffcl.event_key({"event": "order.paid", "id": "u1"}) == "order.paid:u1"
    assert ogoffcl.event_key({"event": "waitlist.joined", "email": "a@b.com"}) == "waitlist.joined:a@b.com"


# ---------- render helpers ----------

def test_render_orders_empty_and_populated():
    assert "No orders" in ogoffcl.render_orders([])
    text = ogoffcl.render_orders([{
        "order_number": "OG-1", "total_amount": 52, "status": "confirmed",
        "payment_status": "paid", "customer_name": "Kwame"}])
    assert "OG-1" in text and "GH₵52" in text and "Kwame" in text


def test_render_order_detail_lists_items_and_total():
    text = ogoffcl.render_order({
        "order_number": "OG-2", "status": "shipped", "payment_status": "paid",
        "customer_name": "Ama", "total_amount": 90,
        "items": [{"product_name": "Hoodie", "size": "L", "quantity": 1, "price": 90}]})
    assert "OG-2" in text and "Hoodie" in text and "GH₵90" in text


def test_render_analytics_and_overview_and_discounts():
    assert "Views: 10" in ogoffcl.render_analytics({"days": 7, "views": 10, "visits": 4, "mobile_pct": 50})
    assert "Paid revenue" in ogoffcl.render_overview({"products": 3, "orders": 5, "unpaid": 1, "revenue": 300})
    assert "No discount" in ogoffcl.render_discounts([])
    assert "DROP20" in ogoffcl.render_discounts([{"code": "DROP20", "percentage": 20, "is_active": True}])


def test_render_waitlist_shows_total_and_emails():
    text = ogoffcl.render_waitlist({"count": 2, "rows": [{"email": "a@b.com", "source": "waitlist"}]})
    assert "2 total" in text and "a@b.com" in text


# ---------- callback-data budget (64-byte cap) ----------

def _all_callback_bytes(kb):
    return [len(b.callback_data.encode()) for row in kb.inline_keyboard for b in row]


@pytest.mark.parametrize("kb", [
    keyboards.store_order_kb(UUID),
    keyboards.order_status_kb(UUID, "shipped", paid=False),
    keyboards.orders_list_kb([{"id": UUID, "order_number": "OG-1", "status": "shipped", "payment_status": "paid"}]),
    keyboards.discounts_kb([{"id": UUID, "code": "DROP20", "is_active": True}]),
    keyboards.site_lock_kb(True),
    keyboards.products_list_kb([{"id": UUID, "name": "Hoodie", "stock": 5, "is_active": True}]),
    keyboards.product_kb({"id": UUID, "is_active": True}),
])
def test_store_callback_data_within_64_bytes(kb):
    assert all(n <= 64 for n in _all_callback_bytes(kb))


def test_order_status_button_encodes_a_parseable_index():
    kb = keyboards.order_status_kb(UUID, "shipped")
    statuses = {}
    for row in kb.inline_keyboard:
        for b in row:
            if b.callback_data.startswith("ordst:"):
                _, oid, idx = b.callback_data.split(":", 2)
                statuses[int(idx)] = oid
    # every pipeline+terminal status is offered, and the index maps back to a real status
    assert len(statuses) == len(ogoffcl.STATUSES)
    assert all(0 <= i < len(ogoffcl.STATUSES) for i in statuses)


# ---------- inbound route security shape ----------

def test_store_route_is_key_gated_and_pause_aware():
    from inais import main

    src = inspect.getsource(main.run_web)
    assert '"/store/events"' in src                 # route registered
    assert "compare_digest" in src                  # constant-time key check
    assert "is_paused" in src                        # /pause mutes store alerts
    assert "claim_event" in src                      # dedupe on re-delivery
