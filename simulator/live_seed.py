"""The Live world's seed data: a small, fixed set of invoices covering
every one of the 12 reason codes, so a real Razorpay test-mode payment
can demonstrate any of them end-to-end. Sized and shaped to match
scripts/live_demo_links.py's 10-payment recipe.

Lives entirely in memory, independent of engine/generate_data.py and
simulator/seed.py's demo world -- see simulator/state.py's live_* attributes
and simulator/webhook.py.
"""
from __future__ import annotations

CUSTOMERS = [
    {"customer_id": "LIVE-CUST-1", "name": "Test Buyer One", "virtual_account": "LIVE-VA-01"},
    {"customer_id": "LIVE-CUST-2", "name": "Test Buyer Two", "virtual_account": "LIVE-VA-02"},
    {"customer_id": "LIVE-CUST-3", "name": "Test Buyer Three", "virtual_account": "LIVE-VA-03"},
]

INVOICES = [
    # Test Buyer One -- EXACT / REF-FUZZY / TOL-FEE / BULK-N / DUP-ONACC
    {"invoice_id": "LIVE-INV-01", "customer_id": "LIVE-CUST-1", "amount_paise": 500_000,
     "issue_date": "2026-08-01", "due_date": "2026-08-31", "disputed": 0},
    {"invoice_id": "LIVE-INV-02", "customer_id": "LIVE-CUST-1", "amount_paise": 900_000,
     "issue_date": "2026-08-01", "due_date": "2026-08-31", "disputed": 0},
    {"invoice_id": "LIVE-INV-03", "customer_id": "LIVE-CUST-1", "amount_paise": 200_000,
     "issue_date": "2026-08-01", "due_date": "2026-08-31", "disputed": 0},
    {"invoice_id": "LIVE-INV-04", "customer_id": "LIVE-CUST-1", "amount_paise": 350_000,
     "issue_date": "2026-08-01", "due_date": "2026-08-31", "disputed": 0},
    {"invoice_id": "LIVE-INV-05", "customer_id": "LIVE-CUST-1", "amount_paise": 450_000,
     "issue_date": "2026-08-01", "due_date": "2026-08-31", "disputed": 0},

    # Test Buyer Two -- AMBIG-N (three financially-identical bills, one
    # disputed) and its requeue resolution
    {"invoice_id": "LIVE-INV-06", "customer_id": "LIVE-CUST-2", "amount_paise": 600_000,
     "issue_date": "2026-08-01", "due_date": "2026-08-31", "disputed": 0},
    {"invoice_id": "LIVE-INV-07", "customer_id": "LIVE-CUST-2", "amount_paise": 600_000,
     "issue_date": "2026-08-01", "due_date": "2026-08-31", "disputed": 0},
    {"invoice_id": "LIVE-INV-08", "customer_id": "LIVE-CUST-2", "amount_paise": 600_000,
     "issue_date": "2026-08-01", "due_date": "2026-08-31", "disputed": 1},

    # Test Buyer Three -- PART-EXP / RESID-DED
    {"invoice_id": "LIVE-INV-09", "customer_id": "LIVE-CUST-3", "amount_paise": 1_500_000,
     "issue_date": "2026-08-01", "due_date": "2026-08-31", "disputed": 0},
    {"invoice_id": "LIVE-INV-10", "customer_id": "LIVE-CUST-3", "amount_paise": 1_000_000,
     "issue_date": "2026-08-01", "due_date": "2026-08-31", "disputed": 0},
]
