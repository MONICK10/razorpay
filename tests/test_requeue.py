"""Proves the re-queue rule: an exception can become solvable once a later
payment changes invoice state, even though it was genuinely unresolvable
at the moment it first arrived.

The curated 60-payment submission dataset (engine/generate_data.py) builds
its AMBIG-N/NO-MATCH cases to be *permanently* unresolvable (a disputed
invoice, an amount that fits nothing) -- that's realistic, but it means the
dataset alone never exercises this rule. This test does, with a minimal
hand-built scenario:

  - Customer has three open invoices of identical value (A, B, C).
  - Payment X arrives first, for exactly two invoices' worth of value.
    Three different pairs from {A, B, C} could account for it -- genuinely
    ambiguous, so it must land in the exception queue, not get guessed.
  - Payment Y arrives later, referencing invoice C by name and settling it
    exactly.
  - After the forward pass, C is closed. Re-evaluating X against current
    invoice state leaves only {A, B} open -- now a unique combination.
    The re-queue loop must pick this up without anyone touching X again.
"""
from __future__ import annotations

from engine.pipeline import Context, run
from engine.reason_codes import ReasonCode


def make_context():
    customer_id = "CUST-1"
    invoices = {
        "INV-A": {"customer_id": customer_id, "amount_paise": 200_000,
                   "issue_date": "2026-01-01", "due_date": "2026-01-15",
                   "disputed": 0},
        "INV-B": {"customer_id": customer_id, "amount_paise": 200_000,
                   "issue_date": "2026-01-02", "due_date": "2026-01-16",
                   "disputed": 0},
        "INV-C": {"customer_id": customer_id, "amount_paise": 200_000,
                   "issue_date": "2026-01-03", "due_date": "2026-01-17",
                   "disputed": 0},
    }
    balance = {inv_id: inv["amount_paise"] for inv_id, inv in invoices.items()}
    ctx = Context(
        customers_by_va={"VA1": {"customer_id": customer_id, "name": "Acme Traders"}},
        invoices=invoices,
        balance=balance,
        invoice_ids_by_customer={customer_id: ["INV-A", "INV-B", "INV-C"]},
    )
    return ctx


def test_ambiguous_payment_resolves_after_later_payment_clears_a_candidate():
    ctx = make_context()

    payment_x = {
        "payment_id": "PAY-X", "amount_paise": 400_000, "method": "NEFT",
        "virtual_account": "VA1", "payer_name": "Acme Traders",
        "narration": "NEFT/Acme Traders PAYMENT RECEIVED",
        "created_at": "2026-02-01 10:00:00",
    }
    payment_y = {
        "payment_id": "PAY-Y", "amount_paise": 200_000, "method": "NEFT",
        "virtual_account": "VA1", "payer_name": "Acme Traders",
        "narration": "NEFT/Acme Traders/INV-C SETTLEMENT",
        "created_at": "2026-02-02 10:00:00",
    }

    rows = run([payment_x, payment_y], ctx)
    by_payment: dict[str, list[dict]] = {}
    for r in rows:
        by_payment.setdefault(r["payment_id"], []).append(r)

    # PAY-Y resolves in the forward pass: exact reference, exact amount.
    y_rows = by_payment["PAY-Y"]
    assert len(y_rows) == 1
    assert y_rows[0]["reason_code"] == ReasonCode.EXACT
    assert y_rows[0]["invoice_id"] == "INV-C"

    # PAY-X could NOT have resolved in the forward pass (three candidate
    # pairs at the time it was evaluated) -- it must come back resolved via
    # the requeue loop, against exactly the two invoices left open.
    x_rows = by_payment["PAY-X"]
    x_invoice_ids = {r["invoice_id"] for r in x_rows}
    assert x_invoice_ids == {"INV-A", "INV-B"}, (
        "expected requeue to resolve PAY-X to the two invoices left open "
        f"after PAY-C closed INV-C, got {x_invoice_ids}")
    assert all(r["reason_code"] == ReasonCode.BULK_N for r in x_rows)
    assert ctx.balance["INV-A"] == 0
    assert ctx.balance["INV-B"] == 0
    assert ctx.balance["INV-C"] == 0


def test_same_payment_is_a_genuine_exception_without_the_later_payment():
    """Sanity check on the ambiguity itself: without PAY-Y, PAY-X must stay
    an unresolved exception -- proves the first test isn't passing by
    accident (e.g. some other path silently resolving it)."""
    ctx = make_context()
    payment_x = {
        "payment_id": "PAY-X", "amount_paise": 400_000, "method": "NEFT",
        "virtual_account": "VA1", "payer_name": "Acme Traders",
        "narration": "NEFT/Acme Traders PAYMENT RECEIVED",
        "created_at": "2026-02-01 10:00:00",
    }
    rows = run([payment_x], ctx)
    assert len(rows) == 1
    assert rows[0]["reason_code"] == ReasonCode.AMBIG_N
    assert rows[0]["invoice_id"] is None
