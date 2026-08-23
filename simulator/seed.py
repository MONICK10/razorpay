"""The simulator's demo world: a small, hand-built set of customers and
invoices sized for a self-explaining walkthrough, not statistical
coverage. Lives entirely in memory -- independent of engine/generate_data.py
and out/finance.db -- so running the simulator never touches the batch
submission's artifacts.

SCENARIOS is the single source of truth for the scenario library, served
to the UI via GET /scenarios (simulator/app.py) so the frontend never
hand-duplicates these values. Each scenario is one or more payments
submitted back to back through the real resolve_payment() path -- no
special-cased demo logic. Scenario 6 (duplicate) submits two payments so
it produces the same DUP-ONACC outcome regardless of whether Acme's bill
was already settled by an earlier scenario run; every other scenario is a
single payment against a dedicated customer/invoice nothing else touches,
so scenarios can be run in any order (except 9, which needs 8's ambiguity
still open -- enforced client-side, see static/index.html).
"""
from __future__ import annotations

CUSTOMERS = [
    {"customer_id": "CUST-1", "name": "Acme Traders", "virtual_account": "VA-1001"},
    {"customer_id": "CUST-2", "name": "Bluepeak Textiles", "virtual_account": "VA-1002"},
    {"customer_id": "CUST-3", "name": "Coastal Logistics", "virtual_account": "VA-1003"},
    {"customer_id": "CUST-4", "name": "Deccan Foods", "virtual_account": "VA-1004"},
    {"customer_id": "CUST-5", "name": "Kumar Electricals", "virtual_account": "VA-1005"},
    {"customer_id": "CUST-6", "name": "Ganges Foods", "virtual_account": "VA-1006"},
    {"customer_id": "CUST-7", "name": "Horizon Exports", "virtual_account": "VA-1007"},
    {"customer_id": "CUST-8", "name": "Meridian Industries", "virtual_account": "VA-1008"},
]

INVOICES = [
    # Acme -- INV-1001 is scenario 1's clean-payment target; INV-1008 is
    # scenario 6's "next open bill" so the auto-applied duplicate credit
    # has somewhere to land, on screen.
    {"invoice_id": "INV-1001", "customer_id": "CUST-1", "amount_paise": 500_000,
     "issue_date": "2026-07-01", "due_date": "2026-07-20", "disputed": 0},
    {"invoice_id": "INV-1008", "customer_id": "CUST-1", "amount_paise": 120_000,
     "issue_date": "2026-07-05", "due_date": "2026-07-25", "disputed": 0},

    {"invoice_id": "INV-1002", "customer_id": "CUST-2", "amount_paise": 320_000,
     "issue_date": "2026-07-02", "due_date": "2026-07-21", "disputed": 0},

    {"invoice_id": "INV-1003", "customer_id": "CUST-3", "amount_paise": 750_000,
     "issue_date": "2026-07-03", "due_date": "2026-07-22", "disputed": 0},

    # Deccan -- stays open (no bootstrap settlement): scenario 4 needs a
    # real ₹2,000 bill to shortfall against.
    {"invoice_id": "INV-1004", "customer_id": "CUST-4", "amount_paise": 200_000,
     "issue_date": "2026-06-15", "due_date": "2026-07-05", "disputed": 0},

    {"invoice_id": "INV-1005", "customer_id": "CUST-5", "amount_paise": 600_000,
     "issue_date": "2026-06-20", "due_date": "2026-07-10", "disputed": 0},
    {"invoice_id": "INV-1006", "customer_id": "CUST-5", "amount_paise": 600_000,
     "issue_date": "2026-06-25", "due_date": "2026-07-15", "disputed": 0},
    {"invoice_id": "INV-1007", "customer_id": "CUST-5", "amount_paise": 600_000,
     "issue_date": "2026-06-30", "due_date": "2026-07-18", "disputed": 0},

    # Ganges -- scenario 5 (RESID-DED): shortfall of ₹150, outside the
    # ₹100 tolerance band, with a deduction-shaped narration.
    {"invoice_id": "INV-1009", "customer_id": "CUST-6", "amount_paise": 300_000,
     "issue_date": "2026-07-04", "due_date": "2026-07-24", "disputed": 0},

    # Horizon -- scenario 7 (BULK-N): three invoices whose only matching
    # combination is all three together.
    {"invoice_id": "INV-1010", "customer_id": "CUST-7", "amount_paise": 200_000,
     "issue_date": "2026-06-28", "due_date": "2026-07-18", "disputed": 0},
    {"invoice_id": "INV-1011", "customer_id": "CUST-7", "amount_paise": 350_000,
     "issue_date": "2026-06-29", "due_date": "2026-07-19", "disputed": 0},
    {"invoice_id": "INV-1012", "customer_id": "CUST-7", "amount_paise": 450_000,
     "issue_date": "2026-06-30", "due_date": "2026-07-20", "disputed": 0},

    # Meridian -- scenario 11 (NO-MATCH / PROVISIONAL): three invoices
    # totalling Rs 63,000; no single invoice or combination sums to the
    # Rs 34,000 payment, so it matches nothing while the customer's
    # certain balance (63,000 - 34,000 = 29,000) still nets out exactly.
    {"invoice_id": "INV-1013", "customer_id": "CUST-8", "amount_paise": 1_800_000,
     "issue_date": "2026-06-10", "due_date": "2026-06-30", "disputed": 0},
    {"invoice_id": "INV-1014", "customer_id": "CUST-8", "amount_paise": 2_200_000,
     "issue_date": "2026-06-12", "due_date": "2026-07-02", "disputed": 0},
    {"invoice_id": "INV-1015", "customer_id": "CUST-8", "amount_paise": 2_300_000,
     "issue_date": "2026-06-14", "due_date": "2026-07-04", "disputed": 0},
]

SCENARIOS = [
    {
        "id": "clean_payment",
        "title": "Clean payment",
        "story": "Acme pays ₹5,000 and writes the bill number correctly.",
        "watch": "It matches on all three — account, amount, reference.",
        "expected_reason_code": "EXACT",
        "payments": [
            {"virtual_account": "VA-1001", "payer_name": "Acme Traders",
             "narration": "NEFT/Acme Traders/INV-1001 SETTLEMENT",
             "amount_paise": 500_000, "method": "NEFT"},
        ],
    },
    {
        "id": "typo_in_reference",
        "title": "Typo in the reference",
        "story": "Bluepeak typed INV-10O2 with a letter O instead of a zero.",
        "watch": "The exact match fails, the AI repairs it, confidence drops.",
        "expected_reason_code": "REF-FUZZY",
        "payments": [
            {"virtual_account": "VA-1002", "payer_name": "Bluepeak Textiles",
             "narration": "IMPS/Bluepeak Textiles/INV-10O2",
             "amount_paise": 320_000, "method": "IMPS"},
        ],
    },
    {
        "id": "paid_part_of_it",
        "title": "Paid part of it",
        "story": "Coastal owed ₹7,500 and sent ₹4,000.",
        "watch": "The bill stays open with ₹3,500 remaining.",
        "expected_reason_code": "PART-EXP",
        "payments": [
            {"virtual_account": "VA-1003", "payer_name": "Coastal Logistics",
             "narration": "NEFT/Coastal Logistics/INV-1003 PART PAYMENT",
             "amount_paise": 400_000, "method": "NEFT"},
        ],
    },
    {
        "id": "bank_took_a_fee",
        "title": "Bank took a fee",
        "story": "Deccan owed ₹2,000, ₹1,960 arrived. The bank kept ₹40.",
        "watch": "The bill closes anyway, and the ledger splits into a CASH "
                 "row and an ADJUSTMENT row.",
        "expected_reason_code": "TOL-FEE",
        "payments": [
            {"virtual_account": "VA-1004", "payer_name": "Deccan Foods",
             "narration": "NEFT/Deccan Foods/INV-1004 SETTLEMENT",
             "amount_paise": 196_000, "method": "NEFT"},
        ],
    },
    {
        "id": "short_paid_on_purpose",
        "title": "Short-paid on purpose",
        "story": "Ganges deducted for damaged goods, not a bank fee.",
        "watch": "Same shortfall shape as “Bank took a fee,” different "
                 "treatment -- deduction, not tolerance.",
        "expected_reason_code": "RESID-DED",
        "payments": [
            {"virtual_account": "VA-1006", "payer_name": "Ganges Foods",
             "narration": "NEFT/Ganges Foods/INV-1009 NET OF DAMAGED GOODS DEDUCTION",
             "amount_paise": 285_000, "method": "NEFT"},
        ],
    },
    {
        "id": "paid_twice_by_mistake",
        "title": "Paid twice by mistake",
        "story": "Acme pays the same bill again.",
        "watch": "The app refuses to double-count, parks it as credit, then "
                 "applies that credit to their next open bill automatically.",
        "expected_reason_code": "DUP-ONACC",
        "payments": [
            {"virtual_account": "VA-1001", "payer_name": "Acme Traders",
             "narration": "NEFT/Acme Traders/INV-1001 SETTLEMENT",
             "amount_paise": 500_000, "method": "NEFT"},
            {"virtual_account": "VA-1001", "payer_name": "Acme Traders",
             "narration": "NEFT/Acme Traders/INV-1001 DUPLICATE RETRY",
             "amount_paise": 500_000, "method": "NEFT"},
        ],
    },
    {
        "id": "several_bills_one_payment",
        "title": "One payment, several bills",
        "story": "One transfer from Horizon covering three invoices at once.",
        "watch": "Subset-sum finds the only combination that fits.",
        "expected_reason_code": "BULK-N",
        "payments": [
            {"virtual_account": "VA-1007", "payer_name": "Horizon Exports",
             "narration": "NEFT/Horizon Exports BULK SETTLEMENT",
             "amount_paise": 1_000_000, "method": "NEFT"},
        ],
    },
    {
        "id": "cant_tell_which_bill",
        "title": "Can't tell which bill",
        "story": "Kumar has three identical ₹6,000 bills and sends "
                 "₹6,000 with no reference.",
        "watch": "Identity is certain, allocation is not. It refuses.",
        "expected_reason_code": "AMBIG-N",
        "payments": [
            {"virtual_account": "VA-1005", "payer_name": "Kumar Electricals",
             "narration": "NEFT/Kumar Electricals PAYMENT RECEIVED",
             "amount_paise": 1_200_000, "method": "NEFT"},
        ],
    },
    {
        "id": "it_solves_itself",
        "title": "It solves itself",
        "story": "More money arrives from Kumar.",
        "watch": "The app re-tries scenario 8's refusal. It now resolves.",
        "expected_reason_code": "EXACT",
        "requires": "cant_tell_which_bill",
        "payments": [
            {"virtual_account": "VA-1005", "payer_name": "Kumar Electricals",
             "narration": "NEFT/Kumar Electricals/INV-1007 SETTLEMENT",
             "amount_paise": 600_000, "method": "NEFT"},
        ],
    },
    {
        "id": "money_from_nowhere",
        "title": "Money from nowhere",
        "story": "A cash deposit with no account and no name.",
        "watch": "Identity fails immediately, so nothing else can run.",
        "expected_reason_code": "SUSPENSE",
        "payments": [
            {"virtual_account": "VA-UNKNOWN-9999", "payer_name": "Unknown Sender",
             "narration": "NEFT/UNMAPPED TRANSFER",
             "amount_paise": 150_000, "method": "NEFT"},
        ],
    },
    {
        "id": "balance_certain_split_unknown",
        "title": "Balance certain, split unknown",
        "story": "Meridian pays ₹34,000 of ₹63,000 in an amount that "
                 "matches no bill.",
        "watch": "No payment can be allocated, but the balance is exact.",
        "expected_reason_code": "NO-MATCH",
        "payments": [
            {"virtual_account": "VA-1008", "payer_name": "Meridian Industries",
             "narration": "NEFT/Meridian Industries PART PAYMENT",
             "amount_paise": 3_400_000, "method": "NEFT"},
        ],
    },
]
