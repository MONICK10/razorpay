"""Customer-level rollup: the two-tier report the PRD asks for, made
concrete. `rollup` is the one arithmetic rule, used identically whether the
inputs come from the generator's known construction (ground truth) or from
the engine's actual allocations (scored output) -- so the two can never
drift apart through duplicated logic.

  open_balance_ledger = what a naive system reports as still owed,
    looking only at invoices and what's actually been posted against them.
  unmatched_cash = money that provably arrived from this customer but was
    never tied to a specific invoice (AMBIG-N / NO-MATCH payments).
  balance = open_balance_ledger - unmatched_cash, floored at zero: the
    real amount still owed, once you account for cash you know arrived
    even though you can't yet say which invoice it settles.

CLEAN: balance is zero, nothing left to explain.
PARTIAL: balance > 0, but every rupee of it is accounted for by real
  invoice-level matches -- no unmatched cash muddying the picture.
PROVISIONAL: balance > 0 AND some of it is only explainable by unmatched
  cash. The number is still certain (it's arithmetic); which invoices it
  will eventually clear is not -- that's the "invoice-level split assumed"
  half of the PRD's two-tier report.
"""
from __future__ import annotations

from dataclasses import dataclass

from engine import l4_policy
from engine.reason_codes import CustomerStatus, UNMATCHED_CASH_CODES


@dataclass
class CustomerRollup:
    customer_id: str
    status: str
    balance_paise: int
    open_balance_ledger_paise: int
    unmatched_cash_paise: int
    total_invoiced_paise: int


def rollup(customer_id: str, total_invoiced_paise: int,
           open_balance_ledger_paise: int, unmatched_cash_paise: int) -> CustomerRollup:
    if open_balance_ledger_paise <= 0:
        status = CustomerStatus.CLEAN
    elif unmatched_cash_paise <= 0:
        status = CustomerStatus.PARTIAL
    else:
        status = CustomerStatus.PROVISIONAL
    balance = max(0, open_balance_ledger_paise - unmatched_cash_paise)
    return CustomerRollup(customer_id, status.value, balance,
                           max(0, open_balance_ledger_paise),
                           max(0, unmatched_cash_paise), total_invoiced_paise)


def compute_from_db(conn) -> dict[str, CustomerRollup]:
    """Roll up every customer from the engine's actual output: invoices,
    allocations, and payments as they landed in out/finance.db."""
    customers = [dict(r) for r in conn.execute("SELECT customer_id FROM customers")]
    invoices = [dict(r) for r in conn.execute(
        "SELECT invoice_id, customer_id, amount_paise FROM invoices")]
    applied = {row["invoice_id"]: row["applied"] for row in conn.execute(
        "SELECT invoice_id, COALESCE(SUM(amount_paise), 0) AS applied "
        "FROM allocations WHERE invoice_id IS NOT NULL GROUP BY invoice_id")}
    unmatched_by_customer: dict[str, int] = {}
    for row in conn.execute(
            "SELECT p.virtual_account AS va, a.amount_paise AS amt, "
            "a.reason_code AS code FROM allocations a "
            "JOIN payments p ON p.payment_id = a.payment_id "
            "WHERE a.invoice_id IS NULL"):
        if row["code"] not in (c.value for c in UNMATCHED_CASH_CODES):
            continue
        cust = conn.execute(
            "SELECT customer_id FROM customers WHERE virtual_account = ?",
            (row["va"],)).fetchone()
        if cust is None:
            continue  # SUSPENSE: no customer to attribute this to
        cid = cust["customer_id"]
        unmatched_by_customer[cid] = unmatched_by_customer.get(cid, 0) + row["amt"]

    invoices_by_customer: dict[str, list[dict]] = {}
    for inv in invoices:
        invoices_by_customer.setdefault(inv["customer_id"], []).append(inv)

    result = {}
    for cust in customers:
        cid = cust["customer_id"]
        custs_invoices = invoices_by_customer.get(cid, [])
        total_invoiced = sum(i["amount_paise"] for i in custs_invoices)
        open_ledger = sum(
            max(0, i["amount_paise"] - applied.get(i["invoice_id"], 0))
            for i in custs_invoices)
        unmatched = unmatched_by_customer.get(cid, 0)
        result[cid] = rollup(cid, total_invoiced, open_ledger, unmatched)
    return result


def suggested_split(conn, customer_id: str) -> list[dict]:
    """For a PROVISIONAL customer: an assumed (never posted) FIFO waterfall
    of their unmatched cash across currently-open invoices, for display
    only -- exactly the "invoice-level split assumed" the PRD names.
    Never written to the allocations ledger."""
    invoices = [dict(r) for r in conn.execute(
        "SELECT invoice_id, amount_paise, issue_date FROM invoices "
        "WHERE customer_id = ?", (customer_id,))]
    applied = {row["invoice_id"]: row["applied"] for row in conn.execute(
        "SELECT invoice_id, COALESCE(SUM(amount_paise), 0) AS applied "
        "FROM allocations WHERE invoice_id IS NOT NULL GROUP BY invoice_id")}
    open_invoices = []
    for inv in invoices:
        bal = inv["amount_paise"] - applied.get(inv["invoice_id"], 0)
        if bal > 0:
            open_invoices.append({**inv, "balance": bal})

    rollups = compute_from_db(conn)
    r = rollups.get(customer_id)
    if r is None or r.status != CustomerStatus.PROVISIONAL.value:
        return []
    return l4_policy.waterfall_split(open_invoices, r.unmatched_cash_paise)
