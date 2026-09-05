"""L6 -- Action. The layer after L5: a reason code says what happened to a
payment, this layer says what happens next. Every exception exits with a
proposed action, not just a label.

Two backends, same contract as L3 (see l3_ai.py and ai_client.py): real
Claude calls when credentials are available, a deterministic template
fallback otherwise. Drafts are never sent from here -- this module only
produces text; simulator/state.py decides when to show it and whether a
human has clicked Send.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from engine.ai_client import get_client
from engine.reason_codes import ActionType, ReasonCode

logger = logging.getLogger(__name__)

HOLD_ESCALATION_DAYS = 7

_ACTION_BY_REASON_CODE = {
    ReasonCode.AMBIG_N: ActionType.QUERY_CUSTOMER,
    ReasonCode.NO_MATCH: ActionType.QUERY_CUSTOMER,
    ReasonCode.SUSPENSE: ActionType.HOLD_AND_ESCALATE,
    ReasonCode.DUP_ONACC: ActionType.APPLY_CREDIT,
}


def action_for_reason_code(reason_code) -> ActionType:
    return _ACTION_BY_REASON_CODE.get(reason_code, ActionType.NONE)


def escalate_at(created_at: str) -> str:
    """created_at as produced by simulator/state.py (ISO, UTC) plus the
    hold window -- purely informational; nothing in this demo advances a
    real clock far enough to actually flip a hold to escalated."""
    try:
        dt = datetime.fromisoformat(created_at)
    except ValueError:
        dt = datetime.now(timezone.utc)
    return (dt + timedelta(days=HOLD_ESCALATION_DAYS)).isoformat(timespec="seconds")


def rupees(paise: int) -> str:
    return f"Rs {paise / 100:,.2f}"


def draft_customer_query(payment: dict, customer_name: str, rationale: str) -> str:
    """AMBIG-N / NO-MATCH: ask the customer which invoice a payment we
    couldn't confidently match was meant to settle."""
    client = get_client()
    if client is None:
        return _stub_query(payment, customer_name, rationale)
    try:
        prompt = (
            f"Draft a short, polite email to a customer named {customer_name} "
            f"about a payment of {rupees(payment['amount_paise'])} we received "
            f"(narration: \"{payment.get('narration') or '(none given)'}\") that we "
            f"could not confidently match to one of their invoices. "
            f"Reason we could not match it: {rationale}. "
            "Ask them to confirm which invoice(s) this payment was intended to "
            "settle. Keep it under 120 words, no subject line, sign off as "
            "'Accounts Team'. Plain text only.")
        return client.complete(prompt, max_tokens=400)
    except Exception as exc:
        logger.warning("L6 action: draft_customer_query failed (%s), falling back", exc)
        return _stub_query(payment, customer_name, rationale)


def _stub_query(payment: dict, customer_name: str, rationale: str) -> str:
    date = (payment.get("created_at") or "")[:10]
    return (
        f"Hi {customer_name},\n\n"
        f"We received a payment of {rupees(payment['amount_paise'])}"
        f"{f' on {date}' if date else ''}, but we could not confidently match it "
        f"to one of your invoices ({rationale}).\n\n"
        "Could you let us know which invoice(s) this payment was intended to "
        "settle?\n\n"
        "Thanks,\nAccounts Team")


def draft_statement(customer_name: str, total_invoiced_paise: int,
                     open_balance_paise: int, unmatched_cash_paise: int,
                     certain_balance_paise: int) -> str:
    """Customer-level PROVISIONAL: the aggregate balance is certain even
    though we can't yet prove which invoice(s) some of their cash settles."""
    client = get_client()
    if client is None:
        return _stub_statement(customer_name, total_invoiced_paise,
                                open_balance_paise, unmatched_cash_paise,
                                certain_balance_paise)
    try:
        prompt = (
            f"Draft a short account statement email to a customer named "
            f"{customer_name}. Total invoiced: {rupees(total_invoiced_paise)}. "
            f"Open balance on our books: {rupees(open_balance_paise)}. Of that, "
            f"{rupees(unmatched_cash_paise)} is cash we've received from them "
            f"that we haven't yet matched to a specific invoice, so their "
            f"confirmed outstanding balance is {rupees(certain_balance_paise)}. "
            "Explain plainly that the total owed is certain even though the "
            "invoice-by-invoice split of their recent payments isn't yet "
            "confirmed, and ask them to flag it if this doesn't match their "
            "records. Under 120 words, no subject line, sign off as "
            "'Accounts Team'. Plain text only.")
        return client.complete(prompt, max_tokens=400)
    except Exception as exc:
        logger.warning("L6 action: draft_statement failed (%s), falling back", exc)
        return _stub_statement(customer_name, total_invoiced_paise,
                                open_balance_paise, unmatched_cash_paise,
                                certain_balance_paise)


def _stub_statement(customer_name: str, total_invoiced_paise: int,
                     open_balance_paise: int, unmatched_cash_paise: int,
                     certain_balance_paise: int) -> str:
    return (
        f"Hi {customer_name},\n\n"
        f"Statement summary: total invoiced {rupees(total_invoiced_paise)}, "
        f"open balance {rupees(open_balance_paise)}.\n\n"
        f"Of that, {rupees(unmatched_cash_paise)} is cash we've received from "
        "you that we haven't yet matched to a specific invoice. Your total "
        f"owed is still certain -- {rupees(certain_balance_paise)} -- it's "
        "only the invoice-by-invoice split of your recent payments that's "
        "still being confirmed.\n\n"
        "Please let us know if this doesn't match your records.\n\n"
        "Thanks,\nAccounts Team")
