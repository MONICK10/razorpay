"""The Live world's /live page support in simulator/state.py: real-time
events, the shops/sorted_out/could_not_sort/doing_about_it panels the
page renders from, and the idempotency guarantee webhook retries depend
on. Same resolve_payment() as every other world -- these tests check the
bookkeeping around it, not the matching logic itself.
"""
from __future__ import annotations

import pytest

from simulator.state import SessionState


@pytest.fixture
def state(monkeypatch):
    monkeypatch.setattr("engine.l3_ai._get_client", lambda: None)
    monkeypatch.setattr("engine.ai_client.get_client", lambda: None)
    return SessionState()


def _payment(pid, amount_paise, va, narration="", payer_name=None):
    return {
        "payment_id": pid, "amount_paise": amount_paise, "method": "UPI",
        "virtual_account": va, "payer_name": payer_name, "narration": narration,
        "created_at": "2026-08-05T10:00:00+00:00",
    }


def test_exact_payment_resolves_and_appears_in_sorted_out(state):
    state.submit_live_payment(_payment("pay_A1", 500_000, "LIVE-VA-01",
                                        "Razorpay/pay_A1/LIVE-INV-01 PAYMENT LINK"))
    snap = state.live_snapshot()
    assert len(snap["events"]) == 1
    ev = snap["events"][0]
    assert ev["final_code"] == "EXACT"
    assert ev["bills"] == ["LIVE-INV-01"]
    sorted_out = snap["sorted_out"]
    assert len(sorted_out) == 1
    assert sorted_out[0]["code"] == "EXACT"

    setup = {c["customer_id"]: c for c in snap["setup"]}
    inv01 = next(i for i in setup["LIVE-CUST-1"]["invoices"] if i["invoice_id"] == "LIVE-INV-01")
    assert inv01["balance_paise"] == 0


def test_identity_only_payment_refuses_no_match(state):
    """No invoice_id in the narration/notes -- same shape
    scripts/create_payment_link.py's --notes identity produces."""
    state.submit_live_payment(_payment("pay_B1", 500_100, "LIVE-VA-01",
                                        "Razorpay/pay_B1 PAYMENT LINK"))
    snap = state.live_snapshot()
    ev = snap["events"][0]
    assert ev["final_code"] == "NO-MATCH"
    could_not_sort = snap["could_not_sort"]
    assert len(could_not_sort) == 1
    assert could_not_sort[0]["customer"] == "Test Buyer One"
    assert snap["doing_about_it"], "a query-customer action should be drafted"


def test_suspense_payment_from_unmapped_account(state):
    state.submit_live_payment(_payment("pay_C1", 12_300, "UNKNOWN-VA-99", "NEFT/UNMAPPED"))
    snap = state.live_snapshot()
    assert snap["events"][0]["final_code"] == "SUSPENSE"
    assert any(a["action_id"] == "hold-pay_C1" for a in snap["doing_about_it"])


def test_webhook_idempotency_does_not_double_count(state):
    payment = _payment("pay_D1", 500_000, "LIVE-VA-01", "Razorpay/pay_D1/LIVE-INV-01 PAYMENT LINK")
    r1 = state.submit_live_payment(payment)
    r2 = state.submit_live_payment(payment)
    assert r1["duplicate"] is False
    assert r2["duplicate"] is True
    snap = state.live_snapshot()
    assert len(snap["events"]) == 1
    assert len(snap["ledger"]) == 1


def test_send_live_action_marks_sent(state):
    state.submit_live_payment(_payment("pay_E1", 999_900, "LIVE-VA-01", "no reference"))
    snap = state.live_snapshot()
    action_id = snap["doing_about_it"][0]["action_id"]
    assert snap["doing_about_it"][0]["sent"] is False
    state.send_live_action(action_id)
    snap2 = state.live_snapshot()
    matching = next(a for a in snap2["doing_about_it"] if a["action_id"] == action_id)
    assert matching["sent"] is True
