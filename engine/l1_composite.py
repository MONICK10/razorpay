"""L1 -- Composite key. VA already told us the customer (L0). Here we ask:
does a reference token in the narration exactly name one of their open
invoices? Single reference -> single-invoice match. Multiple references
found verbatim -> an explicit bulk match. No fuzzy logic here -- that is
L3's job; L1 only trusts exact string matches, deterministically."""
from __future__ import annotations

from engine import reference


def match_single_reference(narration: str, open_invoices: list[dict]) -> dict | None:
    """Return the single open invoice whose id appears verbatim in
    narration, or None if zero or more than one do."""
    ids = [inv["invoice_id"] for inv in open_invoices]
    found = reference.find_reference_candidates(narration, ids)
    if len(found) != 1:
        return None
    return next(inv for inv in open_invoices if inv["invoice_id"] == found[0])


def match_explicit_bulk(narration: str, open_invoices: list[dict],
                         amount_paise: int) -> list[dict] | None:
    """Return the set of open invoices explicitly referenced by id in
    narration, if there are 2+ of them and their balances sum exactly to
    amount_paise. Otherwise None."""
    ids = [inv["invoice_id"] for inv in open_invoices]
    found = reference.find_reference_candidates(narration, ids)
    if len(found) < 2:
        return None
    matched = [inv for inv in open_invoices if inv["invoice_id"] in found]
    if sum(inv["balance"] for inv in matched) == amount_paise:
        return matched
    return None


def extract_unresolved_tokens(narration: str, open_invoices: list[dict]) -> list[str]:
    """Tokens in narration that look like a reference but did not exactly
    match any open invoice -- candidates for L3 fuzzy repair."""
    known_ids = {inv["invoice_id"] for inv in open_invoices}
    tokens = reference.extract_tokens(narration)
    return [t for t in tokens if t not in known_ids]
