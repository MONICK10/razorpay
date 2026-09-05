"""/app's processing flow reorders the real 64-payment submission dataset
(engine/generate_data.py) so 13 of its own payments run first -- see
simulator/batch_seed.py. This is presentation only: every payment, and
every one of its reason codes, must be exactly what the real engine
already produces. This test is the verification the brief demands: build
the real dataset, run it through SessionState exactly as /app does, and
check every one of the 64 outcomes against engine/generate_data.py's own
ground truth, plus the known 31/17/16 bucket split.
"""
from __future__ import annotations

import pytest

from engine import generate_data
from simulator import batch_seed
from simulator.state import SessionState


@pytest.fixture
def state(monkeypatch):
    monkeypatch.setattr("engine.l3_ai._get_client", lambda: None)
    return SessionState()


def _final_frame(state):
    return state.process_view()["history"][-1]


def test_batch_seed_has_exactly_13_narrated_slots():
    assert len(batch_seed.STORY_SLOT_PAYMENT_IDS) == 13
    assert len(set(batch_seed.STORY_SLOT_PAYMENT_IDS)) == 13
    assert set(batch_seed.NARRATIONS) == set(batch_seed.STORY_SLOT_PAYMENT_IDS)


def test_process_world_has_all_64_payments_exactly_once(state):
    final = _final_frame(state)
    ids = [ev["payment_id"] for ev in final["events"]]
    assert len(ids) == 64
    assert len(set(ids)) == 64


def test_first_13_are_the_story_slots_in_order_and_narrated(state):
    final = _final_frame(state)
    events = final["events"]
    assert [ev["payment_id"] for ev in events[:13]] == batch_seed.STORY_SLOT_PAYMENT_IDS
    for ev in events[:13]:
        assert ev["is_narrated"] is True
        assert ev["story_line"]
        assert ev["how_it_decided"]
        assert ev["what_changed"]
    for ev in events[13:]:
        assert ev["is_narrated"] is False


def test_reordering_does_not_change_any_reason_code_vs_ground_truth(state):
    """The whole point of the reorder: every one of the 64 real payments
    keeps the exact outcome engine/generate_data.py's own ground truth
    says it should have. Reordering only changes the order they are shown
    in, never what actually happened to any of them."""
    generate_data.RNG.seed(42)
    w = generate_data.build_world()
    expected = {row["payment_id"]: row["expected_reason_code"] for row in w.ground_truth}
    assert len(expected) == 64

    final = _final_frame(state)
    for ev in final["events"]:
        assert ev["final_code"] == expected[ev["payment_id"]], (
            f"{ev['payment_id']}: got {ev['final_code']}, ground truth says "
            f"{expected[ev['payment_id']]}")


def test_buckets_unchanged_by_the_reorder(state):
    final = _final_frame(state)
    assert final["buckets"] == {
        "total": 64, "auto_posted": 31, "needs_confirmation": 17,
        "exceptions": 16, "exceptions_grouped": 11,
    }
    assert final["counts"]["sorted"] + final["counts"]["could_not_sort"] == 64
    assert final["counts"]["sorted"] == 48
    assert final["counts"]["could_not_sort"] == 16


def test_summary_by_code_covers_every_payment_exactly_once(state):
    final = _final_frame(state)
    assert sum(r["count"] for r in final["summary_by_code"]) == 64


def test_threshold_curve_always_sums_to_64_and_matches_the_headline_boxes(state):
    """The dial chart must never disagree with the headline boxes: both
    come from the same bucket_payments() call over the same entries."""
    final = _final_frame(state)
    curve = final["threshold_curve"]
    assert [c["threshold"] for c in curve] == list(range(0, 101, 5))
    for c in curve:
        assert c["auto_matched"] + c["human_touches"] + c["could_not_sort"] == 64

    at_default = next(c for c in curve if c["threshold"] == final["threshold"])
    assert at_default["auto_matched"] == final["buckets"]["auto_posted"]
    assert at_default["human_touches"] == final["buckets"]["needs_confirmation"]
    assert at_default["could_not_sort"] == final["buckets"]["exceptions"]


def test_setup_reflects_live_balances_for_the_shops_panel(state):
    """'The shops - what they still owe' needs every customer's invoices
    with a LIVE balance at each point in the walkthrough, frozen per
    frame like everything else."""
    history = state.process_view()["history"]
    setup0 = history[0]["setup"]
    assert len(setup0) == 17  # the batch's known customers
    for cust in setup0:
        for inv in cust["invoices"]:
            assert inv["balance_paise"] == inv["amount_paise"]  # nothing paid yet

    final = _final_frame(state)
    windward = next(c for c in final["setup"] if c["name"] == "Windward Solutions")
    inv0008 = next(i for i in windward["invoices"] if i["invoice_id"] == "INV-0008")
    assert inv0008["balance_paise"] == 0  # PAY-0008 (slot 1) settles it

    bluepeak = next(c for c in final["setup"] if c["name"] == "Bluepeak Solutions")
    by_id = {i["invoice_id"]: i for i in bluepeak["invoices"]}
    assert by_id["INV-0039"]["balance_paise"] == 0        # resolved (FIFO-TIE)
    assert by_id["INV-0040"]["balance_paise"] == by_id["INV-0040"]["amount_paise"]  # untouched sibling


def test_arrival_numbers(state):
    arrival = state.process_view()["arrival"]
    assert arrival["payment_count"] == 64
    assert arrival["merchant_name"] == "KFG Snacks Store"
    assert arrival["date_from"] <= arrival["date_to"]
    assert arrival["total_value_paise"] > 0
    assert set(arrival["csv_columns"]) == {
        "amount_paise", "virtual_account", "payer_name", "narration", "method"}


def test_history_frames_are_frozen_at_that_point(state):
    """Stepping back must show the panels as they were, not the final
    state -- same guarantee /story gives (tests/test_story_world.py)."""
    history = state.process_view()["history"]
    assert len(history) == 65  # frame 0 (opening) + one per payment
    assert history[0]["events"] == []
    assert history[0]["done"] is False
    assert history[-1]["done"] is True


def test_reset_process_clears_sent_but_keeps_the_frames(state):
    final = _final_frame(state)
    action_id = final["doing_about_it"][0]["action_id"]
    state.send_process_action(action_id)
    assert action_id in state.process_view()["sent"]
    view = state.reset_process()
    assert view["sent"] == []
    assert len(view["history"]) == 65
