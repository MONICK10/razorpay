"""L4 -- Policy. Deterministic tie-breaks and structural decisions that
don't require judgment, only a rule: FIFO on neutral ties, duplicates to
on-account, and subset-sum search for unreferenced bulk payments.
`waterfall_split` also backs the customer-level PROVISIONAL view in
engine/customer_status.py -- see there for how it's used."""
from __future__ import annotations

from itertools import combinations

MAX_COMBO_SIZE = 4
MAX_CANDIDATE_INVOICES = 10  # bound the combinatorial search


def find_amount_matches(open_invoices: list[dict], amount_paise: int) -> list[dict]:
    """Open invoices whose balance alone equals amount_paise exactly."""
    return [inv for inv in open_invoices if inv["balance"] == amount_paise]


def find_subset_sum_matches(open_invoices: list[dict],
                             amount_paise: int) -> list[list[dict]]:
    """All combinations (size 2..MAX_COMBO_SIZE) of open invoices whose
    balances sum exactly to amount_paise. Bounded search: only meaningful
    for the small per-customer invoice counts this engine expects."""
    pool = open_invoices[:MAX_CANDIDATE_INVOICES]
    results = []
    for size in range(2, min(MAX_COMBO_SIZE, len(pool)) + 1):
        for combo in combinations(pool, size):
            if sum(inv["balance"] for inv in combo) == amount_paise:
                results.append(list(combo))
    return results


def fifo_pick(candidates: list[dict]) -> dict:
    return min(candidates, key=lambda inv: inv["issue_date"])


def all_neutral(candidates: list[dict]) -> bool:
    """A tie is 'neutral' -- safe for policy to break without a human --
    only when every candidate is undisputed and financially identical."""
    if any(inv["disputed"] for inv in candidates):
        return False
    amounts = {inv["balance"] for inv in candidates}
    return len(amounts) == 1


def waterfall_split(open_invoices: list[dict], amount_paise: int) -> list[dict]:
    """Apply an amount oldest-invoice-first across open invoices until
    exhausted. Used by engine/customer_status.py to produce the *suggested*
    (never posted) invoice-level split for a PROVISIONAL customer: the
    aggregate balance is certain, but which specific invoices it will
    eventually clear is not, so this is an assumption for display, not an
    allocation."""
    remaining = amount_paise
    ordered = sorted(open_invoices, key=lambda inv: inv["issue_date"])
    splits = []
    for inv in ordered:
        if remaining <= 0:
            break
        take = min(inv["balance"], remaining)
        if take > 0:
            splits.append({"invoice_id": inv["invoice_id"], "amount_paise": take})
            remaining -= take
    return splits
