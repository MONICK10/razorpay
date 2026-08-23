"""The closed vocabulary of allocation outcomes. Every payment exits the
pipeline with exactly one of these codes -- no record is ever "unknown".

PROVISIONAL is deliberately NOT in this enum. It answers a different
question than the other eleven -- "how sure are we of this customer's
invoice split?" rather than "what happened to this payment?" -- so it
lives at the customer level instead: see CustomerStatus below and
engine/customer_status.py. A payment that contributes to a provisional
customer picture still exits with an ordinary code (typically NO-MATCH
or AMBIG-N); it's the aggregate across a customer's payments that may be
provisional, not any single payment.
"""
from __future__ import annotations

from enum import Enum


class ReasonCode(str, Enum):
    EXACT = "EXACT"
    TOL_FEE = "TOL-FEE"
    PART_EXP = "PART-EXP"
    RESID_DED = "RESID-DED"
    BULK_N = "BULK-N"
    REF_FUZZY = "REF-FUZZY"
    FIFO_TIE = "FIFO-TIE"
    DUP_ONACC = "DUP-ONACC"
    SUSPENSE = "SUSPENSE"
    AMBIG_N = "AMBIG-N"
    NO_MATCH = "NO-MATCH"


# Codes that represent a resolved allocation (as opposed to landing in the
# exception queue). Whether a resolved allocation counts as "auto-posted"
# for reporting purposes is a separate question, decided by the confidence
# dial against the computed confidence score -- see scoring/score.py.
RESOLVED = {
    ReasonCode.EXACT,
    ReasonCode.TOL_FEE,
    ReasonCode.PART_EXP,
    ReasonCode.RESID_DED,
    ReasonCode.BULK_N,
    ReasonCode.REF_FUZZY,
    ReasonCode.FIFO_TIE,
    ReasonCode.DUP_ONACC,
}

# Codes that land in the exception queue and require a human touch.
EXCEPTIONS = {
    ReasonCode.SUSPENSE,
    ReasonCode.AMBIG_N,
    ReasonCode.NO_MATCH,
}

# Codes whose cash arrived from a known customer but was never tied to a
# specific invoice -- these are the ones that make a customer's aggregate
# balance provisional (see engine/customer_status.py). SUSPENSE is excluded:
# the payer isn't even identified, so there's no customer to roll it up to.
UNMATCHED_CASH_CODES = {
    ReasonCode.AMBIG_N,
    ReasonCode.NO_MATCH,
}


class CustomerStatus(str, Enum):
    """The customer-level counterpart to ReasonCode. Answers: given
    everything we know about this customer's invoices and payments, how
    much of their picture is actually provable?"""
    CLEAN = "CLEAN"          # open ledger balance is zero
    PARTIAL = "PARTIAL"      # balance > 0, but fully explained by real matches
    PROVISIONAL = "PROVISIONAL"  # balance > 0 AND some received cash was
                                  # never tied to a specific invoice


class ActionType(str, Enum):
    """What the agent proposes to do about a payment (or, for
    SEND_STATEMENT, a customer) once L5 has produced a final disposition.
    A reason code says what happened; an action type says what happens
    next -- the difference between a classifier and an agent. See
    engine/l6_action.py."""
    QUERY_CUSTOMER = "QUERY_CUSTOMER"        # AMBIG-N / NO-MATCH: ask which invoice
    SEND_STATEMENT = "SEND_STATEMENT"        # customer-level PROVISIONAL: send a statement
    APPLY_CREDIT = "APPLY_CREDIT"            # DUP-ONACC: auto-apply to next open invoice
    HOLD_AND_ESCALATE = "HOLD_AND_ESCALATE"  # SUSPENSE: hold, escalate after 7 days
    NONE = "NONE"                            # already resolved cleanly, nothing to do


class EntryType(str, Enum):
    """Every allocation row is either real money or an accounting fiction
    that lets an invoice balance reach zero without matching cash. TOL-FEE
    and RESID-DED each post two rows per payment -- the cash actually
    received, and a write-off/deduction that covers the rest of the
    invoice -- and only the first is money that arrived. Tagging both
    lets SUM(allocations WHERE entry_type=CASH) be checked against
    SUM(payments) exactly: if that ever drifts, either money that never
    arrived is being counted as received, or real cash is being
    miscategorized as an adjustment."""
    CASH = "CASH"              # real money that arrived from the payer
    ADJUSTMENT = "ADJUSTMENT"  # write-off / deduction -- no cash moved
