"""The Story world (/story) is a scripted 13-payment walkthrough. Its whole
point is that every outcome is real -- produced by engine.pipeline, not
asserted by hand -- so this test IS the verification the brief demands:
play the script through SessionState exactly as the UI does and check the
reason code of every step, plus the three closing-panel end states.

Pinned to the deterministic L3 stub (no model), which is what
engine/l3_ai.py falls back to whenever no API credential resolves -- the
same conditions the rest of the suite runs under.
"""
from __future__ import annotations

import pytest

from engine.reason_codes import EXCEPTIONS
from simulator import story_seed


@pytest.fixture
def state(monkeypatch):
    monkeypatch.setattr("engine.l3_ai._get_client", lambda: None)
    from simulator.state import SessionState
    return SessionState()


EXC = {c.value for c in EXCEPTIONS}


def play_all(state) -> list[dict]:
    events = {}
    for _ in range(len(story_seed.PAYMENTS_RUPEES)):
        r = state.story_step()
        # snapshot events so far (later steps patch earlier ones on re-queue)
    snap = state.story_snapshot()
    for ev in snap["events"]:
        events[ev["n"]] = ev
    return snap, events


def test_every_scripted_outcome_matches_the_real_engine(state):
    snap, events = play_all(state)

    expected_first = {
        1: "EXACT", 2: "REF-FUZZY", 3: "TOL-FEE", 4: "BULK-N", 5: "DUP-ONACC",
        6: "AMBIG-N", 7: "AMBIG-N", 8: "EXACT",
        9: "PART-EXP", 10: "RESID-DED", 11: "NO-MATCH", 12: "NO-MATCH", 13: "SUSPENSE",
    }
    expected_final = {**expected_first, 6: "FIFO-TIE", 7: "EXACT"}

    for n in range(1, 14):
        assert events[n]["first_code"] == expected_first[n], (
            f"payment {n}: first code {events[n]['first_code']}, expected {expected_first[n]}")
        assert events[n]["final_code"] == expected_final[n], (
            f"payment {n}: final code {events[n]['final_code']}, expected {expected_final[n]}")

    # the two that only settle later must be flagged as such
    assert events[6]["resolved_on_requeue"] is True
    assert events[7]["resolved_on_requeue"] is True


def test_story_seed_expected_codes_are_kept_in_sync(state):
    """story_seed.PAYMENTS_RUPEES carries its own expected_code / first_code
    for the UI -- guard that it never drifts from what the engine does."""
    _, events = play_all(state)
    for p in story_seed.PAYMENTS_RUPEES:
        ev = events[p["n"]]
        assert ev["final_code"] == p["expected_code"], f"payment {p['n']} final"
        assert ev["first_code"] == p.get("first_code", p["expected_code"]), f"payment {p['n']} first"


def test_headline_counts(state):
    snap, _ = play_all(state)
    assert len(snap["events"]) == 13
    c = snap["counts"]
    assert c["sorted"] == 10
    assert c["could_not_sort"] == 3
    assert c["sorted_later"] == 2


def test_without_with_comparison(state):
    snap, _ = play_all(state)
    cmp = snap["comparison"]
    assert cmp["without"]["rows_to_read"] == 13
    assert cmp["without"]["decisions"] == 13
    # with the agent: fewer rows, fewer decisions, less time
    assert cmp["with"]["rows_to_read"] < 13
    assert cmp["with"]["decisions"] < 13
    assert cmp["with"]["minutes"] < cmp["without"]["minutes"]


def test_drafted_message_is_concrete_and_send_is_screen_only(state):
    snap, _ = play_all(state)
    doing = {a["action_id"]: a for a in snap["doing_about_it"]}

    ask = doing["query-STORY-DECCAN"]
    assert ask["draft"] is not None and ask["sent"] is False
    d = ask["draft"]
    # real name, real amounts, real total, real balance, real bill numbers
    for piece in ["To: Deccan Traders", "Rs 4,300", "Rs 2,800", "Rs 7,100 in total",
                  "outstanding balance is Rs 19,900",
                  "INV-09 (Rs 7,000 remaining)", "INV-10 (Rs 20,000)",
                  "KFG Snacks Store"]:
        assert piece in d, f"draft missing {piece!r}\n{d}"

    # the hold-cash and credit rows carry no draft
    assert doing["hold-PAY-13"]["draft"] is None
    assert doing["credit-STORY-KUMAR"]["draft"] is None

    # Send only flips the flag -- nothing is actually sent
    state.send_story_action("query-STORY-DECCAN")
    latest = state.story_snapshot()
    assert {a["action_id"]: a for a in latest["doing_about_it"]}["query-STORY-DECCAN"]["sent"] is True
    # unrelated state untouched
    assert latest["counts"] == snap["counts"]

    # reset clears the sent flag
    fresh = state.reset_story()
    play_all(state)
    again = {a["action_id"]: a for a in state.story_snapshot()["doing_about_it"]}
    assert again["query-STORY-DECCAN"]["sent"] is False


def test_mid_run_draft_covers_only_what_is_held_so_far(state):
    # frame 11 = right after payment 11; only #11 (Rs 4,300) is held for
    # Deccan at that point -- #12 (Rs 2,800) has not arrived yet.
    frame11 = state.story_view()["history"][11]
    doing = {a["action_id"]: a for a in frame11["doing_about_it"]}
    d = doing["query-STORY-DECCAN"]["draft"]
    assert "one payment" in d and "Rs 4,300" in d
    assert "Rs 2,800" not in d


def test_closing_panel_end_states(state):
    snap, _ = play_all(state)
    by_name = {c["name"]: c for c in snap["customers"]}

    kumar = by_name["Kumar Stores"]
    assert kumar["status"] == "CLEAN"
    assert kumar["status_plain"] == "All settled"
    assert kumar["certain_balance_paise"] == 0
    assert kumar["credit_held_paise"] == 5000 * story_seed.RUPEE  # the repeat

    priya = by_name["Priya Mart"]
    assert priya["status"] == "CLEAN"
    assert priya["certain_balance_paise"] == 0

    deccan = by_name["Deccan Traders"]
    assert deccan["status"] == "PROVISIONAL"
    assert deccan["status_plain"] == "Amount known, bills unclear"
    # 45,000 owed; 8,000 toward INV-09, 10,000 cleared on INV-11 (9,000 in +
    # 1,000 tax write-off) -> 27,000 still on the books.
    # 4,300 + 2,800 = 7,100 arrived but unplaced -> exact balance 19,900.
    assert deccan["open_ledger_paise"] == 27000 * story_seed.RUPEE
    assert deccan["unmatched_cash_paise"] == 7100 * story_seed.RUPEE
    assert deccan["certain_balance_paise"] == 19900 * story_seed.RUPEE
    # BUG FIX (a): the best guess only keeps a slice that finishes a bill.
    # 7,000 finishes INV-09; the leftover 100 is shown as "not yet placed",
    # never as a Rs 100 guess against the Rs 20,000 INV-10.
    guess = {s["invoice_id"]: s["amount_paise"] for s in deccan["best_guess"]}
    assert guess == {"INV-09": 7000 * story_seed.RUPEE}
    assert deccan["not_yet_placed_paise"] == 100 * story_seed.RUPEE


def test_reset_restores_a_fresh_day(state):
    state.send_story_action("query-STORY-DECCAN")
    view = state.reset_story()
    assert view["sent"] == []               # the sent flag is cleared
    assert len(view["history"]) == 14       # the day is re-played on reset
    frame0 = view["history"][0]             # ... and frame 0 is a clean slate
    assert frame0["events"] == []
    assert frame0["counts"]["sorted"] == 0
    for c in frame0["setup"]:
        for inv in c["invoices"]:
            assert inv["balance_paise"] == inv["amount_paise"]


def test_history_frames_are_frozen_at_that_point(state):
    """Stepping back must show the panels AS THEY WERE, not the final state.
    Payments 6 and 7 are held (AMBIG-N) until payment 8 clears them -- so the
    frame right after payment 6 must still show it unresolved, even though by
    the end it is sorted."""
    for _ in range(13):
        state.story_step()
    hist = state.story_view()["history"]

    f6 = hist[6]          # right after payment 6
    ev6_at_6 = next(e for e in f6["events"] if e["n"] == 6)
    assert ev6_at_6["final_code"] == "AMBIG-N"
    assert ev6_at_6["resolved_on_requeue"] is False
    assert any(r["n"] == 6 for r in f6["could_not_sort"])
    assert not any(r["n"] == 6 for r in f6["sorted_out"])
    assert f6["counts"]["sorted_later"] == 0

    f8 = hist[8]          # after payment 8 -- now 6 and 7 have settled
    ev6_at_8 = next(e for e in f8["events"] if e["n"] == 6)
    assert ev6_at_8["final_code"] == "FIFO-TIE"
    assert ev6_at_8["resolved_on_requeue"] is True
    assert any(r["n"] == 6 for r in f8["sorted_out"])
    assert f8["counts"]["sorted_later"] == 2

    # the balances in the shops strip are point-in-time too
    def deccan_open(frame):
        return next(c for c in frame["setup"] if c["name"] == "Deccan Traders")
    inv09_at_8 = next(i for i in deccan_open(hist[8])["invoices"] if i["invoice_id"] == "INV-09")
    inv09_at_9 = next(i for i in deccan_open(hist[9])["invoices"] if i["invoice_id"] == "INV-09")
    assert inv09_at_8["balance_paise"] == 15000 * story_seed.RUPEE   # untouched
    assert inv09_at_9["balance_paise"] == 7000 * story_seed.RUPEE    # after the Rs 8,000 part payment
