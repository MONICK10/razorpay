"""Ask the ledger, for /app's floating chat widget (simulator/state.py's
ask_process()/build_customer_answer()/explain_why_open(), and
simulator/ask_ledger.py's compose_process_answer()). Strictly read-only:
these tests pin that guarantee, the customer-answer format (every bill
named individually, never a summary phrase), and the follow-up memory
("that one" resolving to the last customer discussed).
"""
from __future__ import annotations

import json

import pytest

from simulator.state import SessionState


@pytest.fixture
def state(monkeypatch):
    monkeypatch.setattr("engine.l3_ai._get_client", lambda: None)
    monkeypatch.setattr("engine.ai_client.get_client", lambda: None)
    return SessionState()


def test_asking_never_changes_state(state):
    before = json.dumps(state.process_view(), default=str, sort_keys=True)
    state.ask_process("tell me about Bluepeak Solutions", [])
    state.ask_process("who owes us the most?", [])
    state.ask_process("why did we refuse payment 11?", [])
    after = json.dumps(state.process_view(), default=str, sort_keys=True)
    assert before == after


def test_customer_answer_names_every_bill_in_three_groups(state):
    ca = state.build_customer_answer("Bluepeak Solutions", state.proc_history[-1])
    assert ca is not None
    assert ca["name"] == "Bluepeak Solutions"
    paid_ids = {i["invoice_id"] for i in ca["paid_in_full"]}
    open_ids = {i["invoice_id"] for i in ca["nothing_received"]}
    assert "INV-0039" in paid_ids   # resolved by the FIFO-TIE pick
    assert "INV-0040" in open_ids   # the untouched sibling
    assert ca["total_billed_paise"] == ca["total_received_paise"] + ca["still_owed_paise"]
    # the invoice that got paid names which payment did it
    inv0039 = next(i for i in ca["paid_in_full"] if i["invoice_id"] == "INV-0039")
    assert inv0039["paid_by_n"] == 6  # slot 6 in the reordered batch (batch_seed.py)


def test_composed_customer_answer_never_uses_a_summary_phrase(state):
    r = state.ask_process("tell me about Bluepeak Solutions", [])
    assert r["customer"] is not None
    answer = r["answer"]
    for banned in ("some bills", "a few bills", "still open" + " (some)"):
        assert banned not in answer.lower()
    assert "INV-0039" in answer and "INV-0040" in answer  # every bill named


def test_could_not_sort_customer_gets_a_send_email_action_with_a_real_draft(state):
    ca = state.build_customer_answer("Deccan Textiles", state.proc_history[-1])
    assert ca["could_not_sort"], "Deccan Textiles should have unresolved payments in this dataset"
    assert ca["action"] is not None
    draft = ca["action"]["draft"]
    assert draft is not None
    assert "Deccan Textiles" in draft
    for row in ca["could_not_sort"]:
        assert f"{row['amount_paise'] // 100:,}" in draft


def test_follow_up_resolves_that_one_from_history(state):
    first = state.ask_process("tell me about Bluepeak Solutions", [])
    history = [
        {"role": "user", "text": "tell me about Bluepeak Solutions"},
        {"role": "assistant", "text": first["answer"]},
    ]
    follow_up = state.ask_process("why is that one still open?", history)
    assert follow_up["customer"] is None  # targeted answer, not the full picture again
    assert "INV-0040" in follow_up["answer"]
    assert "INV-0039" in follow_up["answer"]  # names the sibling that WAS matched


def test_who_owes_the_most(state):
    r = state.ask_process("who owes us the most?", [])
    assert "owes the most" in r["answer"]


def test_how_much_is_stuck_and_why(state):
    r = state.ask_process("how much is stuck and why?", [])
    assert "SUSPENSE" in r["answer"] or "stuck" in r["answer"].lower()


def test_why_did_we_refuse_a_specific_payment(state):
    r = state.ask_process("why did we refuse payment 11?", [])
    assert "#11" in r["answer"]


def test_unresolvable_question_says_so_rather_than_guessing(state):
    r = state.ask_process("what is the meaning of life?", [])
    assert "can't answer" in r["answer"].lower()


def test_unknown_customer_name_resolves_to_no_customer(state):
    assert state.build_customer_answer("Nonexistent Corp", state.proc_history[-1]) is None


def test_at_step_0_nothing_has_been_received_for_any_customer(state):
    """The chatbot must answer from the SAME frozen per-step state the
    page's own panels use, not the fully-processed end state. At step 0
    (opening frame, before any payment) every bill is still fully open."""
    frame0 = state.proc_history[0]
    for cust in frame0["setup"]:
        ca = state.build_customer_answer(cust["name"], frame0)
        assert ca["total_received_paise"] == 0, cust["name"]
        assert ca["paid_in_full"] == []
        assert ca["partly_paid"] == []
        assert len(ca["nothing_received"]) == len(cust["invoices"])
        assert ca["still_owed_paise"] == ca["total_billed_paise"]

    # same guarantee through the full ask_process() -> compose_process_answer
    # path, driven by the step the frontend actually sends
    r = state.ask_process("tell me about Bluepeak Solutions", [], step=0)
    assert r["customer"]["total_received_paise"] == 0
    assert "INV-0039" in r["answer"]  # named in "nothing received yet", not "paid in full"
    assert "paid by payment" not in r["answer"]


def test_step_n_matches_what_the_page_shows_at_step_n(state):
    """At step 6 (payment 6, Bluepeak Solutions' FIFO-TIE pick), INV-0039
    is already paid -- exactly what the paylist/decision-trace show at
    that point, per tests/test_batch_reorder.py."""
    frame6 = state.proc_history[6]
    ca = state.build_customer_answer("Bluepeak Solutions", frame6)
    paid_ids = {i["invoice_id"] for i in ca["paid_in_full"]}
    assert paid_ids == {"INV-0039"}

    # one step earlier, payment 6 hasn't happened yet -- nothing paid
    frame5 = state.proc_history[5]
    ca5 = state.build_customer_answer("Bluepeak Solutions", frame5)
    assert ca5["paid_in_full"] == []
    assert ca5["total_received_paise"] == 0
