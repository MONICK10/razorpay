"""Proves the accounting-integrity invariant: every paisa tagged CASH is
real money that arrived in a payment, exactly once. TOL-FEE and RESID-DED
each post two rows per payment -- the cash actually received, and a
write-off/deduction that closes the rest of the invoice without any money
moving -- so SUM(allocations WHERE entry_type=CASH) must equal
SUM(payments.amount_paise) exactly. If it doesn't, either money that
never arrived is being counted as received, or real cash is being
miscategorized as an adjustment.
"""
from __future__ import annotations

from engine.pipeline import Context, run
from engine.reason_codes import EntryType, ReasonCode


def make_context():
    customer_id = "CUST-1"
    invoices = {
        "INV-A": {"customer_id": customer_id, "amount_paise": 100_000,
                   "issue_date": "2026-01-01", "due_date": "2026-01-15", "disputed": 0},
        "INV-B": {"customer_id": customer_id, "amount_paise": 100_000,
                   "issue_date": "2026-01-02", "due_date": "2026-01-16", "disputed": 0},
        "INV-C": {"customer_id": customer_id, "amount_paise": 100_000,
                   "issue_date": "2026-01-03", "due_date": "2026-01-17", "disputed": 0},
        "INV-D": {"customer_id": customer_id, "amount_paise": 100_000,
                   "issue_date": "2026-01-04", "due_date": "2026-01-18", "disputed": 0},
    }
    balance = {inv_id: inv["amount_paise"] for inv_id, inv in invoices.items()}
    ctx = Context(
        customers_by_va={"VA1": {"customer_id": customer_id, "name": "Acme Traders"}},
        invoices=invoices,
        balance=balance,
        invoice_ids_by_customer={customer_id: ["INV-A", "INV-B", "INV-C", "INV-D"]},
    )
    return ctx


def test_cash_allocations_sum_to_payments_across_tolerance_and_deduction():
    ctx = make_context()
    payments = [
        {  # EXACT -- clean single-row settlement
            "payment_id": "PAY-1", "amount_paise": 100_000, "method": "NEFT",
            "virtual_account": "VA1", "payer_name": "Acme Traders",
            "narration": "NEFT/Acme Traders/INV-A SETTLEMENT",
            "created_at": "2026-02-01 10:00:00",
        },
        {  # TOL-FEE -- shortfall within tolerance, written off to bank charges
            "payment_id": "PAY-2", "amount_paise": 95_000, "method": "NEFT",
            "virtual_account": "VA1", "payer_name": "Acme Traders",
            "narration": "NEFT/Acme Traders/INV-B SETTLEMENT",
            "created_at": "2026-02-02 10:00:00",
        },
        {  # RESID-DED -- shortfall outside tolerance, deduction keyword
            "payment_id": "PAY-3", "amount_paise": 80_000, "method": "NEFT",
            "virtual_account": "VA1", "payer_name": "Acme Traders",
            "narration": "NEFT/Acme Traders/INV-C NET OF TDS DEDUCTION",
            "created_at": "2026-02-03 10:00:00",
        },
        {  # overpayment -- excess parked on account, still real cash
            "payment_id": "PAY-4", "amount_paise": 120_000, "method": "NEFT",
            "virtual_account": "VA1", "payer_name": "Acme Traders",
            "narration": "NEFT/Acme Traders/INV-D SETTLEMENT",
            "created_at": "2026-02-04 10:00:00",
        },
    ]

    rows = run(payments, ctx)

    cash_total = sum(r["amount_paise"] for r in rows if r["entry_type"] == EntryType.CASH)
    payments_total = sum(p["amount_paise"] for p in payments)
    assert cash_total == payments_total, (
        f"CASH allocations ({cash_total}p) must equal payments ({payments_total}p); "
        "an ADJUSTMENT row is being counted as cash, or vice versa")

    adjustment_rows = [r for r in rows if r["entry_type"] == EntryType.ADJUSTMENT]
    assert {r["reason_code"] for r in adjustment_rows} == {ReasonCode.TOL_FEE, ReasonCode.RESID_DED}
    assert sum(r["amount_paise"] for r in adjustment_rows) == 5_000 + 20_000

    # Sanity: invoice balances still reach zero using BOTH cash and
    # adjustment rows -- entry_type only changes the bank-reconciliation
    # view, not how invoices get closed.
    assert ctx.balance["INV-A"] == 0
    assert ctx.balance["INV-B"] == 0
    assert ctx.balance["INV-C"] == 0
    assert ctx.balance["INV-D"] == 0


def test_no_match_and_suspense_are_cash_not_adjustment():
    """An unmatched or unidentified payment is still real money that
    arrived -- it must stay CASH, not be miscategorized as an adjustment
    just because it never landed on an invoice."""
    ctx = make_context()
    payments = [
        {  # SUSPENSE -- unmapped virtual account
            "payment_id": "PAY-5", "amount_paise": 50_000, "method": "NEFT",
            "virtual_account": "VA-UNKNOWN", "payer_name": "Nobody",
            "narration": "NEFT/UNMAPPED TRANSFER",
            "created_at": "2026-02-05 10:00:00",
        },
    ]
    rows = run(payments, ctx)
    assert len(rows) == 1
    assert rows[0]["reason_code"] == ReasonCode.SUSPENSE
    assert rows[0]["entry_type"] == EntryType.CASH
    assert rows[0]["amount_paise"] == 50_000
